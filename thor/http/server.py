#!/usr/bin/env python

"""
Thor HTTP Server

This library allow implementation of an HTTP/1.1 server that is
"non-blocking," "asynchronous" and "event-driven" -- i.e., it achieves very
high performance and concurrency, so long as the application code does not
block (e.g., upon network, disk or database access). Blocking on one request
will block the entire server.

"""

__author__ = "Mark Nottingham <mnot@mnot.net>"
__copyright__ = """\
Copyright (c) 2005-2011 Mark Nottingham

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import os
import sys

from thor.events import EventEmitter, on
from thor.tcp import TcpServer

from thor.http.common import HttpMessageHandler, \
    CLOSE, COUNTED, CHUNKED, \
    ERROR, \
    hop_by_hop_hdrs, \
    get_header, header_names
from thor.http.error import HttpVersionError, HostRequiredError, \
    TransferCodeError


class HttpServer(EventEmitter):
    "An asynchronous HTTP server."

    tcp_server_class = TcpServer
    idle_timeout = 60 # in seconds

    def __init__(self, host, port, loop=None):
        EventEmitter.__init__(self)
        self.tcp_server = self.tcp_server_class(host, port, loop=loop)
        self.tcp_server.on('connect', self.handle_conn)

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


class HttpServerConnection(HttpMessageHandler, EventEmitter):
    "A handler for an HTTP server connection."
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

    def input_start(self, top_line, hdr_tuples, conn_tokens,
        transfer_codes, content_length):
        """
        Take the top set of headers from the input stream, parse them
        and queue the request to be processed by the application.
        """
        try:
            method, _req_line = top_line.split(None, 1)
            uri, req_version = _req_line.rsplit(None, 1)
            req_version = req_version.rsplit('/', 1)[1]
        except (ValueError, IndexError):
            self.input_error(HttpVersionError(top_line))
            # TODO: more fine-grained
            raise ValueError
        if 'host' not in header_names(hdr_tuples):
            self.input_error(HostRequiredError())
            raise ValueError
        for code in transfer_codes:
            # we only support 'identity' and chunked' codes in requests
            if code not in ['identity', 'chunked']:
                self.input_error(TransferCodeError(code))
                raise ValueError
        exchange = HttpServerExchange(
            self, method, uri, hdr_tuples, req_version
        )
        self.ex_queue.append(exchange)
        self.server.emit('exchange', exchange)
        if not self.output_paused:
            # we only start new requests if we have some output buffer 
            # available. 
            exchange.request_start()
        allows_body = (content_length) or (transfer_codes != [])
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
        self._input_state = ERROR
        status_code, status_phrase = err.server_status or \
            (500, 'Internal Server Error')
        hdrs = [
            ('Content-Type', 'text/plain'),
        ]
        body = err.desc
        if err.detail:
            body += " (%s)" % err.detail
        ex = HttpServerExchange(self, "1.1") # FIXME
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
        res_hdrs = [i for i in res_hdrs \
                    if not i[0].lower() in hop_by_hop_hdrs ]

        try:
            body_len = int(get_header(res_hdrs, "content-length").pop(0))
        except (IndexError, ValueError):
            body_len = None
        if body_len is not None:
            delimit = COUNTED
            res_hdrs.append(("Connection", "keep-alive"))
        elif self.req_version == "1.1":
            delimit = CHUNKED
            res_hdrs.append(("Transfer-Encoding", "chunked"))
        else:
            delimit = CLOSE
            res_hdrs.append(("Connection", "close"))

        self.http_conn.output_start(
            "HTTP/1.1 %s %s" % (status_code, status_phrase),
            res_hdrs, delimit
        )

    def response_body(self, chunk):
        "Send part of the response body. May be called zero to many times."
        self.http_conn.output_body(chunk)

    def response_done(self, trailers):
        """
        Signal the end of the response, whether or not there was a body. MUST
        be called exactly once for each response.
        """
        self.http_conn.output_end(trailers)


def test_handler(x):
    @on(x, 'request_start')
    def go(*args):
        print "start: %s on %s" % (str(args[1]), id(x.http_conn))
        x.response_start(200, "OK", [])
        x.response_body('foo!')
        x.response_done([])

    @on(x, 'request_body')
    def body(chunk):
        print "body: %s" % chunk

    @on(x, 'request_done')
    def done(trailers):
        print "done: %s" % str(trailers)


if __name__ == "__main__":
    from thor.loop import run
    sys.stderr.write("PID: %s\n" % os.getpid())
    h, p = '127.0.0.1', int(sys.argv[1])
    demo_server = HttpServer(h, p)
    demo_server.on('exchange', test_handler)
    run()
