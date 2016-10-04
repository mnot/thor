#!/usr/bin/env python

"""
Thor HTTP Client

This library allow implementation of an HTTP/1.1 client that is
"non-blocking," "asynchronous" and "event-driven" -- i.e., it achieves very
high performance and concurrency, so long as the application code does not
block (e.g., upon network, disk or database access). Blocking on one response
will block the entire client.

"""

from __future__ import absolute_import
from __future__ import print_function

from collections import defaultdict
try:
    from urlparse import urlsplit, urlunsplit
except ImportError:
    from urllib.parse import urlsplit, urlunsplit

import thor
from thor.events import EventEmitter, on
from thor.tcp import TcpClient
from thor.tls import TlsClient

from thor.http.common import HttpMessageHandler, \
    CLOSE, COUNTED, CHUNKED, NOBODY, \
    QUIET, WAITING, ERROR, \
    idempotent_methods, no_body_status, hop_by_hop_hdrs, \
    header_names
from thor.http.error import UrlError, ConnectError, \
    ReadTimeoutError, HttpVersionError, StartLineError

req_rm_hdrs = hop_by_hop_hdrs + [b'host']

# TODO: next-hop version cache for Expect/Continue, etc.

class HttpClient(object):
    "An asynchronous HTTP client."

    tcp_client_class = TcpClient
    tls_client_class = TlsClient

    def __init__(self, loop=None):
        self.loop = loop or thor.loop._loop
        self.idle_timeout = 60       # integer seconds
        self.connect_timeout = None  # integer seconds
        self.read_timeout = None     # integer seconds
        self.retry_limit = 2         # integer
        self.retry_delay = 0.5       # integer seconds
        self.max_server_conn = 4     # integer
        self.proxy_tls = False       # boolean
        self.proxy_host = None       # bytes
        self.proxy_port = None       # integer
        self.careful = True          # boolean
        self._idle_conns = defaultdict(list)
        self._conn_counts = defaultdict(int)
        self.loop.on('stop', self._close_conns)

    def exchange(self):
        return HttpClientExchange(self)

    def _attach_conn(self, origin, handle_connect, handle_connect_error, connect_timeout):
        "Find an idle connection for origin, or create a new one."
        if self.proxy_host and self.proxy_port:
            # TODO: full form of request-target
            host, port = self.proxy_host, self.proxy_port
            if self.proxy_tls:
                scheme = b'https'
            else:
                scheme = b'http'
            origin = (scheme, host, port)
        else:
            scheme, host, port = origin
        while True:
            try:
                tcp_conn = self._idle_conns[origin].pop()
            except IndexError:
                self._new_conn(origin, handle_connect, handle_connect_error, connect_timeout)
                break
            if tcp_conn.tcp_connected:
                tcp_conn.removeListeners('data', 'pause', 'close')
                tcp_conn.pause(True)
                if hasattr(tcp_conn, "_idler"):
                    tcp_conn._idler.delete()
                handle_connect(tcp_conn)
                break

    def _release_conn(self, tcp_conn, scheme):
        "Add an idle connection back to the pool."
        tcp_conn.removeListeners('close')
        origin = (scheme, tcp_conn.host, tcp_conn.port)
        if tcp_conn.tcp_connected:
            def idle_close():
                "Remove the connection from the pool when it closes."
                if hasattr(tcp_conn, "_idler"):
                    tcp_conn._idler.delete()
                self._dead_conn(origin)
                try:
                    self._idle_conns[origin].remove(tcp_conn)
                except (KeyError, ValueError):
                    pass
            tcp_conn.on('close', idle_close)
            if self.idle_timeout > 0:
                tcp_conn._idler = self.loop.schedule(self.idle_timeout, tcp_conn.close)
            else:
                tcp_conn.close()
                self._dead_conn(origin)
            self._idle_conns[origin].append(tcp_conn)
        else:
            self._dead_conn(origin)

    def _new_conn(self, origin, handle_connect, handle_error, timeout):
        "Create a new connection."
        (scheme, host, port) = origin
        if scheme == b'http':
            tcp_client = self.tcp_client_class(self.loop)
        elif scheme == b'https':
            tcp_client = self.tls_client_class(self.loop)
        else:
            raise ValueError(u'unknown scheme %s' % scheme.decode('utf-8', 'replace'))
        tcp_client.on('connect', handle_connect)
        tcp_client.on('connect_error', handle_error)
        self._conn_counts[origin] += 1
        tcp_client.connect(host, port, timeout) # FIXME: encoding?

    def _dead_conn(self, origin):
        "Notify the client that a connect to origin is dead."
        self._conn_counts[origin] -= 1

    def _close_conns(self):
        "Close all idle HTTP connections."
        for conn_list in self._idle_conns.values():
            for conn in conn_list:
                try:
                    conn.close()
                except:
                    pass
        self._idle_conns.clear()
        # TODO: probably need to close in-progress conns too.


class HttpClientExchange(HttpMessageHandler, EventEmitter):
    default_state = QUIET

    def __init__(self, client):
        HttpMessageHandler.__init__(self)
        EventEmitter.__init__(self)
        self.client = client
        self.careful = client.careful
        self.method = None
        self.uri = None
        self.req_hdrs = None
        self.req_target = None
        self.scheme = None
        self.authority = None
        self.res_version = None
        self.tcp_conn = None
        self.origin = None
        self._conn_reusable = False
        self._req_body = False
        self._req_started = False
        self._retries = 0
        self._read_timeout_ev = None
        self._output_buffer = []

    def __repr__(self):
        status = [self.__class__.__module__ + "." + self.__class__.__name__]
        status.append('%s {%s}' % (self.method.encode('utf-8', 'replace') or "-", self.uri or "-"))
        if self.tcp_conn:
            status.append(self.tcp_conn.tcp_connected and 'connected' or 'disconnected')
        status.append(HttpMessageHandler.__repr__(self))
        return "<%s at %#x>" % (", ".join(status), id(self))

    def request_start(self, method, uri, req_hdrs):
        """
        Start a request to uri using method, where req_hdrs is a list of (field_name, field_value)
        for the request headers.

        All values are bytes.
        """
        self.method = method
        self.uri = uri
        self.req_hdrs = req_hdrs
        try:
            self.origin = self._parse_uri(self.uri)
        except (TypeError, ValueError):
            return
        self.client._attach_conn(self.origin, self._handle_connect,
                                 self._handle_connect_error, self.client.connect_timeout)
    # TODO: if we sent Expect: 100-continue, don't wait forever
    # (i.e., schedule something)

    def _parse_uri(self, uri):
        """
        Given a bytes, parse out the host, port, authority and request target.
        Returns None if there is an error, otherwise the origin.
        """
        (scheme, authority, path, query, fragment) = urlsplit(uri)
        scheme = scheme.lower()
        if scheme == b'http':
            default_port = 80
        elif scheme == b'https':
            default_port = 443
        else:
            self.input_error(UrlError("Unsupported URL scheme '%s'" % \
                                      scheme.decode('utf-8', 'replace')))
            raise ValueError
        if b"@" in authority:
            userinfo, authority = authority.split(b"@", 1)
        if b":" in authority:
            host, port = authority.rsplit(b":", 1)
            try:
                port = int(port)
            except ValueError:
                self.input_error(UrlError("Non-integer port '%s' in URL" % \
                                          port.decode('utf-8', 'replace')))
                raise
            if not 1 <= port <= 65535:
                self.input_error(UrlError("URL port %i out of range" % port))
                raise ValueError
        else:
            host, port = authority, default_port
        if path == b"":
            path = b"/"
        self.scheme = scheme
        self.authority = authority
        self.req_target = urlunsplit((b'', b'', path, query, b''))
        return scheme, host, port

    def _req_start(self):
        """
        Queue the request headers for sending.
        """
        self._req_started = True
        req_hdrs = [i for i in self.req_hdrs if not i[0].lower() in req_rm_hdrs]
        req_hdrs.append((b"Host", self.authority))
        if self.client.idle_timeout > 0:
            req_hdrs.append((b"Connection", b"keep-alive"))
        else:
            req_hdrs.append((b"Connection", b"close"))
        if b"content-length" in header_names(req_hdrs):
            delimit = COUNTED
        elif self._req_body:
            req_hdrs.append((b"Transfer-Encoding", b"chunked"))
            delimit = CHUNKED
        else:
            delimit = NOBODY
        self._input_state = WAITING
        self.output_start(b"%s %s HTTP/1.1" % (self.method, self.req_target), req_hdrs, delimit)

    def request_body(self, chunk):
        "Send part of the request body. May be called zero to many times."
        if self._input_state == ERROR:
            return
        if not self._req_started:
            self._req_body = True
            self._req_start()
        self.output_body(chunk)

    def request_done(self, trailers):
        """
        Signal the end of the request, whether or not there was a body. MUST
        be called exactly once for each request.
        """
        if self._input_state == ERROR:
            return
        if not self._req_started:
            self._req_start()
        self.output_end(trailers)

    def res_body_pause(self, paused):
        "Temporarily stop / restart sending the response body."
        if self.tcp_conn and self.tcp_conn.tcp_connected:
            self.tcp_conn.pause(paused)

    # Methods called by tcp

    def _handle_connect(self, tcp_conn):
        "The connection has succeeded."
        self.tcp_conn = tcp_conn
        self._set_read_timeout('connect')
        tcp_conn.on('data', self.handle_input)
        tcp_conn.on('close', self._conn_closed)
        tcp_conn.on('pause', self._req_body_pause)
        # FIXME: should this be done AFTER _req_start?
        self.output(b"") # kick the output buffer
        self.tcp_conn.pause(False)

    def _handle_connect_error(self, err_type, err_id, err_str):
        "The connection has failed."
        self.input_error(ConnectError(err_str))

    def _conn_closed(self):
        "The server closed the connection."
        self._clear_read_timeout()
        if self._input_buffer:
            self.handle_input(b"")
        if self._input_state == QUIET:
            pass # nothing to see here
        elif self._input_delimit == CLOSE:
            self.input_end([])
        elif self._input_state == WAITING:
            if self.method in idempotent_methods:
                if self._retries < self.client.retry_limit:
                    self.client.loop.schedule(self.client.retry_delay, self._retry)
                else:
                    self.input_error(
                        ConnectError("Tried to connect %s times." % (self._retries + 1)))
            else:
                self.input_error(ConnectError("Can't retry %s method" % \
                                              self.method.decode('utf-8', 'replace')))
        else:
            self.input_error(ConnectError(
                "Server dropped connection before the response was complete."))

    def _retry(self):
        "Retry the request."
        self._clear_read_timeout()
        self._retries += 1
        try:
            origin = self._parse_uri(self.uri)
        except (TypeError, ValueError):
            return
        self.client._attach_conn(origin, self._handle_connect,
                                 self._handle_connect_error, self.client.connect_timeout)

    def _req_body_pause(self, paused):
        "The client needs the application to pause/unpause the request body."
        self.emit('pause', paused)

    # Methods called by common.HttpMessageHandler

    def input_start(self, top_line, hdr_tuples, conn_tokens, transfer_codes, content_length):
        """
        Take the top set of headers from the input stream, parse them
        and queue the request to be processed by the application.
        """
        self._clear_read_timeout()
        try:
            proto_version, status_txt = top_line.split(None, 1)
            proto, self.res_version = proto_version.rsplit(b'/', 1)
        except (ValueError, IndexError):
            self.input_error(StartLineError(top_line.decode('utf-8', 'replace')))
            raise ValueError
        if proto != b"HTTP" or self.res_version not in [b"1.0", b"1.1"]:
            self.input_error(HttpVersionError(proto_version.decode('utf-8', 'replace')))
            raise ValueError
        try:
            res_code, res_phrase = status_txt.split(None, 1)
        except ValueError:
            res_code = status_txt.rstrip()
            res_phrase = b""
        if b'close' not in conn_tokens:
            if (self.res_version == b"1.0" and b'keep-alive' in conn_tokens) \
              or self.res_version in [b"1.1"]:
                self._conn_reusable = True
        self._set_read_timeout('start')
        self.emit('response_start', res_code, res_phrase, hdr_tuples)
        allows_body = (res_code not in no_body_status) and (self.method != b"HEAD")
        return allows_body

    def input_body(self, chunk):
        "Process a response body chunk from the wire."
        self._clear_read_timeout()
        self.emit('response_body', chunk)
        self._set_read_timeout('body')

    def input_end(self, trailers):
        "Indicate that the response body is complete."
        self._clear_read_timeout()
        if self.tcp_conn:
            if self.tcp_conn.tcp_connected and self._conn_reusable:
                self.client._release_conn(self.tcp_conn, self.scheme)
            else:
                self.tcp_conn.close()
        self._dead_conn()
        self.tcp_conn = None
        self.emit('response_done', trailers)

    def input_error(self, err):
        "Indicate an error state."
        if err.client_recoverable and not self.careful:
            # This error isn't absolutely fatal, and we want to see the rest.
            # Still, we probably don't want to reuse this connection later.
            self._conn_reusable = False
        else:
            # It really is a fatal error.
            self._input_state = ERROR
            self._clear_read_timeout()
            self._dead_conn()
            if self.tcp_conn and self.tcp_conn.tcp_connected:
                self.tcp_conn.close()
            self.tcp_conn = None
        self.emit('error', err)

    def _dead_conn(self):
        "Inform the client that the connection is dead."
        self.client._dead_conn(self.origin)

    def output(self, chunk):
        self._output_buffer.append(chunk)
        if self.tcp_conn and self.tcp_conn.tcp_connected:
            self.tcp_conn.write(b"".join(self._output_buffer))
            self._output_buffer = []

    # misc

    def _set_read_timeout(self, kind):
        "Set the read timeout."
        if self.client.read_timeout:
            self._read_timeout_ev = self.client.loop.schedule(
                self.client.read_timeout, self.input_error, ReadTimeoutError(kind))

    def _clear_read_timeout(self):
        "Clear the read timeout."
        if self._read_timeout_ev:
            self._read_timeout_ev.delete()


def test_client(request_uri, out, err):  # pragma: no coverage
    "A simple demonstration of a client."
    from thor.loop import stop, run

    c = HttpClient()
    c.connect_timeout = 5
    c.careful = False
    x = c.exchange()

    @on(x)
    def response_start(status, phrase, headers):
        "Print the response headers."
        out(b"HTTP/%s %s %s\n" % (x.res_version, status, phrase))
        out(b"\n".join([b"%s:%s" % header for header in headers]))
        print()
        print()

    @on(x)
    def error(err_msg):
        if err_msg:
            err("\033[1;31m*** ERROR:\033[0;39m %s (%s)\n" % (err_msg.desc, err_msg.detail))
        if not err_msg.client_recoverable:
            stop()

    x.on('response_body', out)

    @on(x)
    def response_done(trailers):
        thor.schedule(1, stop)

    x.request_start(b"GET", request_uri, [])
    x.request_done([])
    run()


if __name__ == "__main__":
    import sys
    test_client(sys.argv[1].encode('ascii'), sys.stdout.buffer.write, sys.stderr.write)
