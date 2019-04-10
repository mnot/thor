#!/usr/bin/env python

"""
Thor HTTP Client

This library allow implementation of an HTTP/1.1 client that is
"non-blocking," "asynchronous" and "event-driven" -- i.e., it achieves very
high performance and concurrency, so long as the application code does not
block (e.g., upon network, disk or database access). Blocking on one response
will block the entire client.

"""

from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit
from string import ascii_letters, digits
from typing import Callable, List, Dict, Tuple, Union # pylint: disable=unused-import

import thor
from thor.events import EventEmitter, on
from thor.loop import LoopBase
from thor.loop import ScheduledEvent  # pylint: disable=unused-import
from thor.tcp import TcpClient, TcpConnection
from thor.tls import TlsClient

from thor.http.common import HttpMessageHandler, \
    States, Delimiters, \
    idempotent_methods, no_body_status, hop_by_hop_hdrs, \
    header_names, RawHeaderListType, OriginType
from thor.http.error import UrlError, ConnectError, \
    ReadTimeoutError, HttpVersionError, StartLineError
from thor.http.error import HttpError # pylint: disable=unused-import

req_rm_hdrs = hop_by_hop_hdrs + [b'host']

# TODO: next-hop version cache for Expect/Continue, etc.

class HttpClient:
    "An asynchronous HTTP client."

    tcp_client_class = TcpClient
    tls_client_class = TlsClient

    def __init__(self, loop: LoopBase = None) -> None:
        self.loop = loop or thor.loop._loop
        self.idle_timeout = 60       # type: int    # seconds
        self.connect_timeout = None  # type: int    # seconds
        self.read_timeout = None     # type: int    # seconds
        self.retry_limit = 2         # type: int
        self.retry_delay = 0.5       # type: float  # seconds
        self.max_server_conn = 6     # type: int
        self.careful = True          # type: bool
        self._idle_conns = defaultdict(list)     # type: Dict[OriginType, List[TcpConnection]]
        self._conn_counts = defaultdict(int)     # type: Dict[OriginType, int]
        self._req_q = defaultdict(list) # type: Dict[OriginType, List[Tuple[Callable, Callable, float]]]
        self.loop.once('stop', self._close_conns)

    def exchange(self) -> 'HttpClientExchange':
        return HttpClientExchange(self)

    def attach_conn(self, origin: OriginType, handle_connect: Callable,
                    handle_connect_error: Callable, connect_timeout: float) -> None:
        "Find an idle connection for origin, or create a new one."
        while True:
            try:
                tcp_conn = self._idle_conns[origin].pop()
            except IndexError: # No idle conns available.
                del self._idle_conns[origin]
                self._new_conn(origin, handle_connect, handle_connect_error, connect_timeout)
                break
            if tcp_conn.tcp_connected:
                tcp_conn.removeListeners('data', 'pause', 'close')
                tcp_conn.pause(True)
                if hasattr(tcp_conn, "_idler"):
                    tcp_conn._idler.delete() # type: ignore
                handle_connect(tcp_conn)
                break

    def release_conn(self, exchange: 'HttpClientExchange') -> None:
        "Add an idle connection back to the pool."
        tcp_conn = exchange.tcp_conn
        if tcp_conn:
            tcp_conn.removeListeners('data', 'pause', 'close')
            exchange.tcp_conn = None
            if tcp_conn.tcp_connected:
                origin = exchange.origin
                def idle_close() -> None:
                    "Remove the connection from the pool when it closes."
                    if hasattr(tcp_conn, "_idler"):
                        tcp_conn._idler.delete()  # type: ignore
                    self.dead_conn(exchange)
                    try:
                        self._idle_conns[origin].remove(tcp_conn)
                        if not self._idle_conns[origin]:
                            del self._idle_conns[origin]
                    except (KeyError, ValueError):
                        pass
                if self._req_q[origin]:
                    handle_connect = self._req_q[origin].pop(0)[0]
                    handle_connect(tcp_conn)
                elif self.idle_timeout > 0:
                    tcp_conn.once('close', idle_close)
                    tcp_conn._idler = self.loop.schedule(self.idle_timeout, idle_close) # type: ignore
                    self._idle_conns[origin].append(tcp_conn)
                else:
                    self.dead_conn(exchange)

    def dead_conn(self, exchange: 'HttpClientExchange') -> None:
        "Notify the client that a connection is dead."
        origin = exchange.origin
        if exchange.tcp_conn and exchange.tcp_conn.tcp_connected:
            exchange.tcp_conn.close()
        exchange.tcp_conn = None
        self._conn_counts[origin] -= 1
        if self._conn_counts[origin] == 0:
            del self._conn_counts[origin]
            if self._req_q[origin]:
                (handle_connect, handle_connect_error, connect_timeout) = self._req_q[origin].pop(0)
                self._new_conn(origin, handle_connect, handle_connect_error, connect_timeout)

    def _new_conn(self, origin: OriginType, handle_connect: Callable,
                  handle_error: Callable, timeout: float) -> None:
        "Create a new connection."
        if self._conn_counts[origin] >= self.max_server_conn:
            self._req_q[origin].append((handle_connect, handle_error, timeout))
            return
        (scheme, host, port) = origin
        tcp_client = None  # type: Union[TcpClient, TlsClient]
        if scheme == 'http':
            tcp_client = self.tcp_client_class(self.loop)
        elif scheme == 'https':
            tcp_client = self.tls_client_class(self.loop)
        else:
            raise ValueError(u'unknown scheme %s' % scheme)
        tcp_client.once('connect', handle_connect)
        tcp_client.once('connect_error', handle_error)
        self._conn_counts[origin] += 1
        tcp_client.connect(host.encode('ascii'), port, timeout)

    def _close_conns(self) -> None:
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
    default_state = States.QUIET

    def __init__(self, client: HttpClient) -> None:
        HttpMessageHandler.__init__(self)
        EventEmitter.__init__(self)
        self.client = client
        self.careful = client.careful
        self.method = None             # type: bytes
        self.uri = None                # type: bytes
        self.req_hdrs = None           # type: RawHeaderListType
        self.req_target = None         # type: bytes
        self.authority = None          # type: bytes
        self.res_version = None        # type: bytes
        self.tcp_conn = None           # type: TcpConnection
        self.origin = None             # type: OriginType
        self._conn_reusable = False
        self._req_body = False
        self._req_started = False
        self._retries = 0
        self._read_timeout_ev = None   # type: ScheduledEvent
        self._output_buffer = []       # type: List[bytes]

    def __repr__(self) -> str:
        status = [self.__class__.__module__ + "." + self.__class__.__name__]
        status.append('%s {%s}' % (self.method.decode('utf-8', 'replace') or "-",
                                   self.uri.decode('utf-8', 'replace') or "-"))
        if self.tcp_conn:
            status.append(self.tcp_conn.tcp_connected and 'connected' or 'disconnected')
        status.append(HttpMessageHandler.__repr__(self))
        return "<%s at %#x>" % (", ".join(status), id(self))

    def request_start(self, method: bytes, uri: bytes, req_hdrs: RawHeaderListType) -> None:
        """
        Start a request to uri using method, where req_hdrs is a list of (field_name, field_value)
        for the request headers.
        """
        self.method = method
        self.uri = uri
        self.req_hdrs = req_hdrs
        try:
            self.origin = self._parse_uri(self.uri)
        except (TypeError, ValueError):
            return
        self.client.attach_conn(self.origin, self._handle_connect,
                                self._handle_connect_error, self.client.connect_timeout)
    # TODO: if we sent Expect: 100-continue, don't wait forever
    # (i.e., schedule something)

    def _parse_uri(self, uri: bytes) -> OriginType:
        """
        Given a uri, parse out the host, port, authority and request target.
        Returns None if there is an error, otherwise the origin.
        """
        try:
            (schemeb, authority, path, query, fragment) = urlsplit(uri)
        except UnicodeDecodeError:
            self.input_error(UrlError("URL has non-ascii characters"), False)
            raise ValueError
        except ValueError as why:
            self.input_error(UrlError(why.args[0]), False)
            raise
        try:
            scheme = schemeb.decode('utf-8').lower()
        except UnicodeDecodeError:
            self.input_error(UrlError("URL scheme has non-ascii characters"), False)
            raise ValueError
        if scheme == 'http':
            default_port = 80
        elif scheme == 'https':
            default_port = 443
        else:
            self.input_error(UrlError("Unsupported URL scheme '%s'" % scheme), False)
            raise ValueError
        if b"@" in authority:
            authority = authority.split(b"@", 1)[1]
        if b":" in authority:
            hostb, portb = authority.rsplit(b":", 1)
            try:
                port = int(portb.decode('utf-8', 'replace'))
            except ValueError:
                self.input_error(UrlError("Non-integer port '%s' in URL" % \
                                          portb.decode('utf-8', 'replace')), False)
                raise
            if not 1 <= port <= 65535:
                self.input_error(UrlError("URL port %i out of range" % port), False)
                raise ValueError
        else:
            hostb, port = authority, default_port
        try:
            host = hostb.decode("ascii", "strict")
        except UnicodeDecodeError:
            self.input_error(UrlError("URL host has non-ascii characters"), False)
            raise ValueError
        if not all(c in ascii_letters + digits + '.-' for c in host):
            self.input_error(UrlError("URL hostname has disallowed character"), False)
            raise ValueError
        if len(host) > 255:
            self.input_error(UrlError("URL hostname greater than 255 characters"), False)
            raise ValueError
        labels = host.split(".")
        if any(len(l) == 0 for l in labels):
            self.input_error(UrlError("URL hostname has empty label"), False)
            raise ValueError
        if any(len(l) > 63 for l in labels):
            self.input_error(UrlError("URL hostname label greater than 63 characters"), False)
            raise ValueError
#        if any(l[0].isdigit() for l in labels):
#            self.input_error(UrlError("URL hostname label starts with digit"), False)
#            raise ValueError
        if path == b"":
            path = b"/"
        self.authority = authority
        self.req_target = urlunsplit((b'', b'', path, query, b''))
        return scheme, host, port

    def _req_start(self) -> None:
        """
        Queue the request headers for sending.
        """
        if self._req_started:
            return
        self._req_started = True
        req_hdrs = [i for i in self.req_hdrs if not i[0].lower() in req_rm_hdrs]
        req_hdrs.append((b"Host", self.authority))
        if self.client.idle_timeout == 0:
            req_hdrs.append((b"Connection", b"close"))
        if b"content-length" in header_names(req_hdrs):
            delimit = Delimiters.COUNTED
        elif self._req_body:
            req_hdrs.append((b"Transfer-Encoding", b"chunked"))
            delimit = Delimiters.CHUNKED
        else:
            delimit = Delimiters.NOBODY
        self._input_state = States.WAITING
        self.output_start(b"%s %s HTTP/1.1" % (self.method, self.req_target), req_hdrs, delimit)

    def request_body(self, chunk: bytes) -> None:
        "Send part of the request body. May be called zero to many times."
        if self._input_state == States.ERROR:
            return
        self._req_body = True
        self._req_start()
        self.output_body(chunk)

    def request_done(self, trailers: RawHeaderListType) -> None:
        """
        Signal the end of the request, whether or not there was a body. MUST
        be called exactly once for each request.
        """
        if self._input_state == States.ERROR:
            return
        self._req_start()
        close = self.output_end(trailers)
        if close: # if there was a problem and we didn't ever assign a request delimiter.
            self.client.dead_conn(self)

    def res_body_pause(self, paused: bool) -> None:
        "Temporarily stop / restart sending the response body."
        if self.tcp_conn and self.tcp_conn.tcp_connected:
            self.tcp_conn.pause(paused)

    # Methods called by tcp

    def _handle_connect(self, tcp_conn: TcpConnection) -> None:
        "The connection has succeeded."
        self.tcp_conn = tcp_conn
        self._set_read_timeout('connect')
        tcp_conn.on('data', self.handle_input)
        tcp_conn.once('close', self._conn_closed)
        tcp_conn.on('pause', self._req_body_pause)
        # FIXME: should this be done AFTER _req_start?
        self.output(b"") # kick the output buffer
        self.tcp_conn.pause(False)

    def _handle_connect_error(self, err_type: str, err_id: int, err_str: str) -> None:
        "The connection has failed."
        self._clear_read_timeout()
        self.client.dead_conn(self)
        if self._retries < self.client.retry_limit:
            self.client.loop.schedule(self.client.retry_delay, self._retry)
        else:
            self.input_error(ConnectError(err_str), False)

    def _conn_closed(self) -> None:
        "The server closed the connection."
        self._clear_read_timeout()
        self.client.dead_conn(self)
        if self._input_buffer:
            self.handle_input(b"")
        if self._input_state == States.QUIET:
            pass # nothing to see here
        elif self._input_delimit == Delimiters.CLOSE:
            self.input_end([])
        elif self._input_state == States.WAITING:
            if self.method in idempotent_methods:
                if self._retries < self.client.retry_limit:
                    self.client.loop.schedule(self.client.retry_delay, self._retry)
                else:
                    self.input_error(
                        ConnectError("Tried to connect %s times." % (self._retries + 1)), False)
            else:
                self.input_error(ConnectError("Can't retry %s method" % \
                                              self.method.decode('utf-8', 'replace')), False)
        else:
            self.input_error(ConnectError(
                "Server dropped connection before the response was complete."), False)

    def _retry(self) -> None:
        "Retry the request."
        self._retries += 1
        self.client.attach_conn(self.origin, self._handle_connect,
                                self._handle_connect_error, self.client.connect_timeout)

    def _req_body_pause(self, paused: bool) -> None:
        "The client needs the application to pause/unpause the request body."
        self.emit('pause', paused)

    # Methods called by common.HttpMessageHandler

    def input_start(self, top_line: bytes, hdr_tuples: RawHeaderListType,
                    conn_tokens: List[bytes], transfer_codes: List[bytes],
                    content_length: int) -> Tuple[bool, bool]:
        """
        Take the top set of headers from the input stream, parse them
        and queue the request to be processed by the application.
        """
        self._clear_read_timeout()
        try:
            proto_version, status_txt = top_line.split(None, 1)
            proto, self.res_version = proto_version.rsplit(b'/', 1)
        except (ValueError, IndexError):
            self.input_error(StartLineError(top_line.decode('utf-8', 'replace')), True)
            raise ValueError
        if proto != b"HTTP" or self.res_version not in [b"1.0", b"1.1"]:
            self.input_error(HttpVersionError(proto_version.decode('utf-8', 'replace')), True)
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
        is_final = not res_code.startswith(b"1")
        allows_body = is_final and (res_code not in no_body_status) and (self.method != b"HEAD")
        if is_final:
            self.emit('response_start', res_code, res_phrase, hdr_tuples)
        else:
            self.emit('response_nonfinal', res_code, res_phrase, hdr_tuples)
        return allows_body, is_final

    def input_body(self, chunk: bytes) -> None:
        "Process a response body chunk from the wire."
        self._clear_read_timeout()
        self.emit('response_body', chunk)
        self._set_read_timeout('body')

    def input_end(self, trailers: RawHeaderListType) -> None:
        "Indicate that the response body is complete."
        self._clear_read_timeout()
        if self._conn_reusable:
            self.client.release_conn(self)
        else:
            self.client.dead_conn(self)
        self.emit('response_done', trailers)

    def input_error(self, err: HttpError, close: bool = True) -> None:
        "Indicate an error state."
        if err.client_recoverable and not self.careful:
            # This error isn't absolutely fatal, and we want to see the rest.
            # Still, we probably don't want to reuse this connection later.
            self._conn_reusable = False
        else:
            # It really is a fatal error.
            self._input_state = States.ERROR
            self._clear_read_timeout()
            if close:
                self.client.dead_conn(self)
        self.emit('error', err)

    def output(self, data: bytes) -> None:
        self._output_buffer.append(data)
        if self.tcp_conn and self.tcp_conn.tcp_connected:
            self.tcp_conn.write(b"".join(self._output_buffer))
            self._output_buffer = []

    def output_done(self) -> None:
        pass

    # misc

    def _set_read_timeout(self, kind: str) -> None:
        "Set the read timeout."
        if self.client.read_timeout:
            self._read_timeout_ev = self.client.loop.schedule(
                self.client.read_timeout, self.input_error, ReadTimeoutError(kind))

    def _clear_read_timeout(self) -> None:
        "Clear the read timeout."
        if self._read_timeout_ev:
            self._read_timeout_ev.delete()


def test_client(request_uri: bytes, out: Callable, err: Callable) -> None:  # pragma: no coverage
    "A simple demonstration of a client."
    from thor.loop import stop, run, schedule

    c = HttpClient()
    c.connect_timeout = 5
    c.careful = False
    x = c.exchange()

    @on(x)
    def response_start(status: bytes, phrase: bytes, headers: RawHeaderListType) -> None:
        "Print the response headers."
        out(b"HTTP/%s %s %s\n" % (x.res_version, status, phrase))
        out(b"\n".join([b"%s:%s" % header for header in headers]))
        print()
        print()

    @on(x)
    def error(err_msg: HttpError) -> None:
        if err_msg:
            err("\033[1;31m*** ERROR:\033[0;39m %s (%s)\n" % (err_msg.desc, err_msg.detail))
        if not err_msg.client_recoverable:
            stop()

    x.on('response_body', out)

    @on(x)
    def response_done(trailers: RawHeaderListType) -> None:
        schedule(1, stop)

    x.request_start(b"GET", request_uri, [])
    x.request_done([])
    run()


if __name__ == "__main__":
    import sys
    test_client(sys.argv[1].encode('ascii'), sys.stdout.buffer.write, sys.stderr.write)
