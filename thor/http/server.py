#!/usr/bin/env python

"""
Thor HTTP Server

This library allow implementation of an HTTP/1.1 server that is
"non-blocking," "asynchronous" and "event-driven" -- i.e., it achieves very
high performance and concurrency, so long as the application code does not
block (e.g., upon network, disk or database access). Blocking on one request
will block the entire server.

"""

from __future__ import absolute_import
from __future__ import print_function

import os
import sys

from thor import schedule
from thor.events import EventEmitter, on
from thor.tcp import TcpServer

from thor.http.common import HttpMessageHandler, \
    CLOSE, COUNTED, CHUNKED, \
    WAITING, ERROR, \
    hop_by_hop_hdrs, \
    get_header, header_names
from thor.http.error import HttpVersionError, HostRequiredError, TransferCodeError, StartLineError


class HttpServer(EventEmitter):
    "An asynchronous HTTP server."

    tcp_server_class = TcpServer
    idle_timeout = 60 # in seconds

    def __init__(self, host, port, loop=None):
        EventEmitter.__init__(self)
        self.tcp_server = self.tcp_server_class(host, port, loop=loop)
        self.tcp_server.on('connect', self.handle_conn)
        schedule(0, self.emit, 'start')

    def handle_conn(self, tcp_conn):
        http_conn = HttpServerConnection(tcp_conn, self)
        tcp_conn.on('data', http_conn.handle_input)
        tcp_conn.on('close', http_conn.conn_closed)
        tcp_conn.on('pause', http_conn.res_body_pause)
        tcp_conn.pause(False)

    def shutdown(self):
        "Stop the server"
        # TODO: Finish outstanding requests w/ timeout?
        self.tcp_server.shutdown()
        self.emit('stop')


class HttpServerConnection(HttpMessageHandler, EventEmitter):
    "A handler for an HTTP server connection."
    default_state = WAITING

    def __init__(self, tcp_conn, server):
        HttpMessageHandler.__init__(self)
        EventEmitter.__init__(self)
        self.tcp_conn = tcp_conn
        self.server = server
        self.ex_queue = [] # queue of exchanges
        self.output_paused = False

    def req_body_pause(self, paused):
        """
        Indicate that the server should pause (True) or unpause (False) the
        request.
        """
        self.tcp_conn.pause(paused)

    # Methods called by tcp

    def res_body_pause(self, paused):
        "Pause/unpause sending the response body."
        self.output_paused = paused
        self.emit('pause', paused)
        if not paused:
            self.drain_exchange_queue()

    def conn_closed(self):
        "The server connection has closed."
#        for exchange in self.ex_queue:
#            exchange.pause() # FIXME - maybe a connclosed err?
        self.ex_queue = []
        self.tcp_conn = None

    # Methods called by common.HttpRequestHandler

    def output(self, data):
        self.tcp_conn.write(data)

    def input_start(self, top_line, hdr_tuples, conn_tokens, transfer_codes, content_length):
        """
        Take the top set of headers from the input stream, parse them
        and queue the request to be processed by the application.
        """
        try:
            method, req_line = top_line.split(None, 1)
            uri, req_version = req_line.rsplit(None, 1)
            req_version = req_version.rsplit(b'/', 1)[1]
        except (ValueError, IndexError):
            self.input_error(HttpVersionError(top_line.decode('utf-8', 'replace')))
            # TODO: more fine-grained
            raise ValueError
        if b'host' not in header_names(hdr_tuples):
            self.input_error(HostRequiredError())
            raise ValueError
        for code in transfer_codes:
            # we only support 'identity' and chunked' codes in requests
            if code not in [b'identity', b'chunked']:
                self.input_error(TransferCodeError(code.decode('utf-8', 'replace')))
                raise ValueError
        exchange = HttpServerExchange(self, method, uri, hdr_tuples, req_version)
        self.ex_queue.append(exchange)
        self.server.emit('exchange', exchange)
        if not self.output_paused:
            # we only start new requests if we have some output buffer
            # available.
            exchange.request_start()
        allows_body = (content_length and content_length > 0) or (transfer_codes != [])
        return allows_body

    def input_body(self, chunk):
        "Process a request body chunk from the wire."
        self.ex_queue[-1].emit('request_body', chunk)

    def input_end(self, trailers):
        "Indicate that the request body is complete."
        self.ex_queue[-1].emit('request_done', trailers)

    def input_error(self, err):
        """
        Indicate a parsing problem with the request body (which
        hasn't been queued as an exchange yet).
        """
        if err.server_recoverable:
            self.emit('error', err)
        else:
            self._input_state = ERROR
            status_code, status_phrase = err.server_status or (b"500", b'Internal Server Error')
            hdrs = [(b'Content-Type', b'text/plain'),]
            body = err.desc.encode("utf-8")
            if err.detail:
                body += b" (%s)" % err.detail.encode("utf-8")
            ex = HttpServerExchange(self, b'', b'', [], b"1.1")
            ex.response_start(status_code, status_phrase, hdrs)
            ex.response_body(body)
            ex.response_done([])
            self.ex_queue.append(ex)

# FIXME: connection?

#        if self.tcp_conn and not err.server_recoverable:
#            self.tcp_conn.close()
#            self.tcp_conn = None

# TODO: if in mid-request, we need to send an error event and clean up.
#        self.ex_queue[-1].emit('error', err)

    def drain_exchange_queue(self):
        """
        Walk through the exchange queue and kick off unstarted requests
        until we run out of output buffer.
        """
        # TODO: probably have a separate metric for outstanding requests,
        # rather than just the write queue size.
        for exchange in self.ex_queue:
            if not exchange.started:
                exchange.request_start()


class HttpServerExchange(EventEmitter):
    """
    A request/response interaction on an HTTP server.
    """

    def __init__(self, http_conn, method, uri, req_hdrs, req_version):
        EventEmitter.__init__(self)
        self.http_conn = http_conn
        self.method = method
        self.uri = uri
        self.req_hdrs = req_hdrs
        self.req_version = req_version
        self.started = False

    def __repr__(self):
        status = [self.__class__.__module__ + "." + self.__class__.__name__]
        status.append('%s {%s}' % (self.method or "-", self.uri or "-"))
        return "<%s at %#x>" % (", ".join(status), id(self))

    def request_start(self):
        self.started = True
        self.emit('request_start', self.method, self.uri, self.req_hdrs)

    def response_start(self, status_code, status_phrase, res_hdrs):
        "Start a response. Must only be called once per response."
        res_hdrs = [i for i in res_hdrs if not i[0].lower() in hop_by_hop_hdrs]
        try:
            body_len = int(get_header(res_hdrs, b"content-length").pop(0))
        except (IndexError, ValueError):
            body_len = None
        if body_len is not None:
            delimit = COUNTED
            res_hdrs.append((b"Connection", b"keep-alive"))
        elif self.req_version == b"1.1":
            delimit = CHUNKED
            res_hdrs.append((b"Transfer-Encoding", b"chunked"))
        else:
            delimit = CLOSE
            res_hdrs.append((b"Connection", b"close"))

        self.http_conn.output_start(
            b"HTTP/1.1 %s %s" % (status_code, status_phrase), res_hdrs, delimit)

    def response_body(self, chunk):
        "Send part of the response body. May be called zero to many times."
        self.http_conn.output_body(chunk)

    def response_done(self, trailers):
        """
        Signal the end of the response, whether or not there was a body. MUST
        be called exactly once for each response.
        """
        self.http_conn.output_end(trailers)


def test_handler(x): # pragma: no cover
    @on(x, 'request_start')
    def go(*args):
        print("start: %s on %s" % (str(args[1]), id(x.http_conn)))
        x.response_start(b'200', b"OK", [])
        x.response_body(b'foo!')
        x.response_done([])

    @on(x, 'request_body')
    def body(chunk):
        print("body: %s" % chunk)

    @on(x, 'request_done')
    def done(trailers):
        print("done: %s" % str(trailers))


if __name__ == "__main__":
    from thor.loop import run
    sys.stderr.write("PID: %s\n" % os.getpid())
    h, p = '127.0.0.1', int(sys.argv[1])
    demo_server = HttpServer(h, p)
    demo_server.on('exchange', test_handler)
    run()
