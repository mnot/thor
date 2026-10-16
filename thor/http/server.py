#!/usr/bin/env python

"""
Thor HTTP Server

This library allow implementation of an HTTP/1.1 server that is
"non-blocking," "asynchronous" and "event-driven" -- i.e., it achieves very
high performance and concurrency, so long as the application code does not
block (e.g., upon network, disk or database access). Blocking on one request
will block the entire server.

"""

# pylint: disable=raise-missing-from

import os
import sys
from typing import Any, List, Optional, Set, Tuple

from thor.events import EventEmitter, on
from thor.http.common import (
    Delimiters,
    HttpMessageHandler,
    States,
    header_names,
    hop_by_hop_hdrs,
    no_body_status,
    output_content_length,
)
from thor.http.error import (
    HostRequiredError,
    HttpError,
    HttpVersionError,
    RequestTransferEncodingError,
    TooManyMsgsError,
    TransferCodeError,
)
from thor.loop import LoopBase, ScheduledEvent
from thor.tcp import TcpConnection, TcpServer
from thor.types import RawHeaderListType


class HttpServer(EventEmitter):
    "An asynchronous HTTP server."

    tcp_server_class = TcpServer
    idle_timeout: Optional[float] = 60  # in seconds; None or 0 to disable
    request_timeout: Optional[float] = 60  # in seconds; None or 0 to disable
    response_timeout: Optional[float] = None  # in seconds; None or 0 to disable

    def __init__(self, host: bytes, port: int, loop: Optional[LoopBase] = None) -> None:
        EventEmitter.__init__(self)
        self.tcp_server = self.tcp_server_class(host, port, loop=loop)
        self.loop = self.tcp_server.loop
        self.tcp_server.on("connect", self.handle_conn)
        self.loop.schedule(0, self.emit, "start")
        self.connections: Set["HttpServerConnection"] = set()
        self.shutting_down = False

    def handle_conn(self, tcp_conn: TcpConnection) -> None:
        http_conn = HttpServerConnection(tcp_conn, self)
        self.connections.add(http_conn)
        tcp_conn.on("data", http_conn.handle_input)
        tcp_conn.on("disconnect", http_conn.conn_closed)
        tcp_conn.on("pause", http_conn.res_body_pause)
        tcp_conn.pause(False)

    def graceful_shutdown(self) -> None:
        """
        Stop the server gracefully.

        This stops accepting new connections and waits for all active
        connections to close before emitting "stop".
        """
        self.shutting_down = True
        self.tcp_server.on("stop", lambda: self.emit("stop"))
        self.tcp_server.graceful_shutdown()
        for conn in list(self.connections):
            if conn.is_idle:
                conn.close_conn()

    def shutdown(self) -> None:
        "Stop the server"
        self.tcp_server.shutdown()
        self.emit("stop")


class HttpServerConnection(HttpMessageHandler, EventEmitter):
    "A handler for an HTTP server connection."

    default_state = States.WAITING
    max_pipeline_requests = 100

    def __init__(self, tcp_conn: TcpConnection, server: HttpServer) -> None:
        HttpMessageHandler.__init__(self)
        EventEmitter.__init__(self)
        self.tcp_conn: Optional[TcpConnection] = tcp_conn
        self.server = server
        self.ex_queue: List[HttpServerExchange] = []  # queue of exchanges
        self.output_paused = False
        self._pending_error: Optional[HttpError] = None  # deferred error
        self._idler: Optional[ScheduledEvent] = None
        self._req_timer: Optional[ScheduledEvent] = None
        self._req_in_progress = False  # request body incomplete
        self._req_paused = False
        self._res_timer: Optional[ScheduledEvent] = None
        self._res_timer_ex: Optional[HttpServerExchange] = None
        self._in_input = False
        self._arm_idle_timer()

    @property
    def is_idle(self) -> bool:
        return len(self.ex_queue) == 0

    def req_body_pause(self, paused: bool) -> None:
        """
        Indicate that the server should pause (True) or unpause (False) the
        request.
        """
        if self.tcp_conn:
            self.tcp_conn.pause(paused)
        # a paused request isn't the client's fault
        self._req_paused = paused
        if paused:
            self._stop_req_timer()
        elif self._receiving:
            self._start_req_timer()

    @property
    def _receiving(self) -> bool:
        "Is part of a request body or header block waiting on more data?"
        return self._req_in_progress or any(
            chunk.strip() for chunk in self._input_buffer
        )

    def _stop_req_timer(self) -> None:
        if self._req_timer:
            self._req_timer.delete()
            self._req_timer = None

    def _start_req_timer(self) -> None:
        "(Re)start the inactivity timer for the request being received."
        self._stop_req_timer()
        timeout = self.server.request_timeout
        if timeout and timeout > 0 and self.tcp_conn and not self._req_paused:
            self._req_timer = self.server.loop.schedule(timeout, self.close_conn)

    def handle_input(self, inbytes: bytes) -> None:
        if self._in_input:  # the parser calls itself for pipelined data
            HttpMessageHandler.handle_input(self, inbytes)
            return
        self._in_input = True
        try:
            HttpMessageHandler.handle_input(self, inbytes)
        finally:
            self._in_input = False
        if self._receiving:
            self._start_req_timer()
        else:
            self._stop_req_timer()  # e.g., a deferred header error
        self._update_idle_timer(restart=False)

    def _stop_res_timer(self) -> None:
        if self._res_timer:
            self._res_timer.delete()
            self._res_timer = None
        self._res_timer_ex = None

    def _update_res_timer(self) -> None:
        """
        Time the oldest unanswered exchange, once its request is complete
        and the application has been told about it.
        A pipelined exchange isn't timed until those before it are answered.
        """
        target = None
        timeout = self.server.response_timeout
        if timeout and timeout > 0 and self.tcp_conn:
            for ex in self.ex_queue:
                if not ex.res_complete:
                    target = ex if ex.req_complete and ex.started else None
                    break
        if target is self._res_timer_ex:
            return
        self._stop_res_timer()
        if target and timeout:
            self._res_timer_ex = target
            self._res_timer = self.server.loop.schedule(timeout, self.close_conn)

    def _stop_idle_timer(self) -> None:
        if self._idler:
            self._idler.delete()
            self._idler = None

    def _arm_idle_timer(self) -> None:
        self._stop_idle_timer()
        timeout = self.server.idle_timeout
        if timeout and timeout > 0:
            self._idler = self.server.loop.schedule(timeout, self.close_conn)

    def _update_idle_timer(self, restart: bool = True) -> None:
        """
        Once nothing is in flight, close a connection that's shutting down or
        else make sure the idle timer is running (restarting it if asked).
        It stays running while the next request's header block arrives, as a
        total deadline for it; input_start stops it. Waits until the parser
        has finished with a read, so pipelined requests aren't cut off.
        """
        if not self.tcp_conn or not self.is_idle or self._in_input:
            return
        if self.server.shutting_down:
            self.close_conn()
        elif restart or not self._idler:
            self._arm_idle_timer()

    def close_conn(self) -> None:
        "Close the connection."
        self._stop_idle_timer()
        self._stop_req_timer()
        self._stop_res_timer()
        if self.tcp_conn and self.tcp_conn.tcp_connected:
            self.tcp_conn.close()
            self.tcp_conn = None

    def exchange_done(self, exchange: "HttpServerExchange") -> None:
        exchange.res_complete = True
        if exchange.req_complete and exchange in self.ex_queue:
            self.ex_queue.remove(exchange)
        self._handle_pending_error()
        self._update_res_timer()
        self._update_idle_timer()

    def _handle_pending_error(self) -> None:
        """
        Act on a deferred input error once every earlier exchange has been
        answered, so responses stay in request order.
        """
        err = self._pending_error
        if err is None:
            return
        if not self.tcp_conn:
            self._pending_error = None
            return
        in_flight = self.ex_queue[-1] if self.ex_queue else None
        if in_flight and in_flight.req_complete:
            in_flight = None  # the bad request hasn't been queued as an exchange
        earlier = self.ex_queue[:-1] if in_flight else self.ex_queue
        if not all(ex.res_complete for ex in earlier):
            return
        self._pending_error = None
        if in_flight and (
            in_flight.res_complete or self._output_state != States.WAITING
        ):
            # its response is under way or sent; a second can't follow it
            self.close_conn()
        else:
            self._send_error(err)

    # Methods called by tcp

    def res_body_pause(self, paused: bool) -> None:
        "Pause/unpause sending the response body."
        self.output_paused = paused
        self.emit("pause", paused)
        if not paused:
            self.drain_exchange_queue()

    def conn_closed(self) -> None:
        "The server connection has closed."
        self.server.connections.discard(self)
        self._stop_idle_timer()
        self._stop_req_timer()
        self._stop_res_timer()
        self.ex_queue = []
        self._pending_error = None
        self.tcp_conn = None

    # Methods called by common.HttpRequestHandler

    def output(self, data: bytes) -> None:
        if self.tcp_conn and self.tcp_conn.tcp_connected:
            self.tcp_conn.write(data)

    def output_done(self) -> None:
        pass

    def input_start(
        self,
        top_line: bytes,
        hdr_tuples: RawHeaderListType,
        conn_tokens: List[bytes],
        transfer_codes: List[bytes],
        content_length: Optional[int],
    ) -> Tuple[bool, bool]:
        """
        Take the top set of headers from the input stream, parse them
        and queue the request to be processed by the application.
        """
        self._stop_idle_timer()
        try:
            method, req_line = top_line.split(None, 1)
            uri, req_version_txt = req_line.rsplit(None, 1)
            proto, req_version = req_version_txt.rsplit(b"/", 1)
        except (ValueError, IndexError):
            self.input_error(HttpVersionError(top_line.decode("utf-8", "replace")))
            raise ValueError
        if proto != b"HTTP" or req_version not in [b"1.0", b"1.1"]:
            self.input_error(
                HttpVersionError(req_version_txt.decode("utf-8", "replace"))
            )
            raise ValueError
        if b"host" not in header_names(hdr_tuples):
            self.input_error(HostRequiredError())
            raise ValueError
        for code in transfer_codes:
            # only chunked is supported; 'identity' is tolerated before it
            if code not in [b"identity", b"chunked"]:
                self.input_error(TransferCodeError(code.decode("utf-8", "replace")))
                raise ValueError
        if transfer_codes and transfer_codes[-1] != b"chunked":
            # RFC 9112 6.3: the body length can't be determined reliably
            self.input_error(
                RequestTransferEncodingError(
                    transfer_codes[-1].decode("utf-8", "replace")
                )
            )
            raise ValueError
        if len(self.ex_queue) >= self.max_pipeline_requests:
            self.input_error(TooManyMsgsError())
            raise ValueError
        if req_version == b"1.1":
            persistent = b"close" not in conn_tokens
        else:
            persistent = b"keep-alive" in conn_tokens and b"close" not in conn_tokens
        exchange = HttpServerExchange(
            self, method, uri, hdr_tuples, req_version, persistent
        )
        if not persistent:
            # Silently ignore anything pipelined after a request that closes.
            # This shadows the class attribute: every later reset to
            # default_state in common.py lands in ERROR, which drops input.
            self.default_state = States.ERROR
        self.ex_queue.append(exchange)
        self.server.emit("exchange", exchange)
        if not self.output_paused:
            # we only start new requests if we have some output buffer
            # available.
            exchange.request_start()
        allows_body = bool(content_length and content_length > 0) or (
            transfer_codes != []
        )
        self._req_in_progress = allows_body  # timer is started by handle_input
        return allows_body, True

    def input_body(self, chunk: bytes) -> None:
        "Process a request body chunk from the wire."
        ex = self.ex_queue[-1]
        if ex.res_complete:
            return
        ex.emit("request_body", chunk)

    def input_end(self, trailers: RawHeaderListType) -> None:
        "Indicate that the request body is complete."
        self._req_in_progress = False
        self._stop_req_timer()
        ex = self.ex_queue[-1]
        ex.req_complete = True
        if not ex.res_complete:
            ex.emit("request_done", trailers)
        if ex.res_complete and ex in self.ex_queue:
            self.ex_queue.remove(ex)
        self._update_res_timer()

    def input_error(self, err: HttpError) -> None:
        """
        Indicate a parsing problem with the request body (which
        hasn't been queued as an exchange yet).
        """
        if err.server_recoverable:
            self.emit("error", err)
        else:
            self._input_state = States.ERROR
            if self._pending_error is None:
                self._pending_error = err
                self._handle_pending_error()

    def _send_error(self, err: HttpError) -> None:
        "Send an error response for an unrecoverable error, then close."
        status_code, status_phrase = err.server_status
        hdrs = [(b"Content-Type", b"text/plain")]
        body = err.desc.encode("utf-8")
        if err.detail:
            body += b" (%s)" % err.detail.encode("utf-8")
        ex = HttpServerExchange(self, b"", b"", [], b"1.1")
        self.ex_queue.append(ex)  # so completing it doesn't arm the idle timer
        try:
            ex.response_start(status_code, status_phrase, hdrs)
            ex.response_body(body)
            ex.response_done([])
        except (BufferError, OSError):
            pass  # can't deliver it; just make sure the connection goes away
        self.close_conn()

    def drain_exchange_queue(self) -> None:
        """
        Walk through the exchange queue and kick off unstarted requests
        until we run out of output buffer.
        """
        for exchange in self.ex_queue:
            if not exchange.started:
                exchange.request_start()


class HttpServerExchange(EventEmitter):
    """
    A request/response interaction on an HTTP server.
    """

    def __init__(
        self,
        http_conn: HttpServerConnection,
        method: bytes,
        uri: bytes,
        req_hdrs: RawHeaderListType,
        req_version: bytes,
        persistent: bool = True,
    ) -> None:
        EventEmitter.__init__(self)
        self.http_conn = http_conn
        self.method = method
        self.uri = uri
        self.req_hdrs = req_hdrs
        self.req_version = req_version
        self.persistent = persistent
        self.started = False
        self.req_complete = False
        self.res_complete = False

    def __repr__(self) -> str:
        status = [self.__class__.__module__ + "." + self.__class__.__name__]
        method = self.method.decode("ascii") or "-"
        uri = self.uri.decode("utf-8", "replace") or "-"
        status.append(f"{method} <{uri}>")
        return f'<{", ".join(status)} at {id(self):#x}>'

    def request_start(self) -> None:
        self.started = True
        self.http_conn._update_res_timer()  # pylint: disable=protected-access
        self.emit("request_start", self.method, self.uri, self.req_hdrs)

    def response_nonfinal(
        self, status_code: bytes, status_phrase: bytes, res_hdrs: RawHeaderListType
    ) -> None:
        "Send a non-final (1xx) response. Can be called zero to many times."
        self.http_conn.output_start(
            b"HTTP/1.1 %s %s" % (status_code, status_phrase),
            res_hdrs,
            Delimiters.NONE,
            is_final=False,
        )

    def response_start(
        self, status_code: bytes, status_phrase: bytes, res_hdrs: RawHeaderListType
    ) -> None:
        "Start a response. Must only be called once per response."
        res_hdrs = [i for i in res_hdrs if not i[0].lower() in hop_by_hop_hdrs]
        body_len = output_content_length(res_hdrs)
        # A final response with a 1xx status is not an interim response; use
        # response_nonfinal for those.
        bodiless = (
            self.method == b"HEAD"
            or status_code in no_body_status
            or status_code.startswith(b"1")
        )
        if bodiless:
            # No body framing. Content-Length is kept for HEAD and 304 (it
            # describes the unsent representation) but must not be sent
            # with 204 or 1xx (RFC 9110 section 8.6).
            delimit = Delimiters.NOBODY
            if status_code == b"204" or status_code.startswith(b"1"):
                res_hdrs = [i for i in res_hdrs if i[0].lower() != b"content-length"]
        elif body_len is not None:
            delimit = Delimiters.COUNTED
        elif self.req_version == b"1.1":
            delimit = Delimiters.CHUNKED
            res_hdrs.append((b"Transfer-Encoding", b"chunked"))
        else:
            delimit = Delimiters.CLOSE
        if delimit is Delimiters.CLOSE or not self.persistent:
            res_hdrs.append((b"Connection", b"close"))
        elif self.req_version == b"1.0":
            res_hdrs.append((b"Connection", b"keep-alive"))

        self.http_conn.output_start(
            b"HTTP/1.1 %s %s" % (status_code, status_phrase),
            res_hdrs,
            delimit,
            is_final=True,
        )

    def response_body(self, chunk: bytes) -> None:
        "Send part of the response body. May be called zero to many times."
        self.http_conn.output_body(chunk)

    def response_done(self, trailers: RawHeaderListType, close: bool = False) -> None:
        """
        Signal the end of the response, whether or not there was a body. MUST
        be called exactly once for each response. If close is True, the
        underlying TCP connection is closed after the response is flushed,
        regardless of response framing.
        """
        should_close = self.http_conn.output_end(trailers)
        if (should_close or close or not self.persistent) and self.http_conn.tcp_conn:
            self.http_conn.close_conn()
        self.http_conn.exchange_done(self)


def test_handler(ex: HttpServerExchange) -> None:  # pragma: no cover
    @on(ex, "request_start")
    def go(*args: Any) -> None:
        print(f"start: {str(args[1])} on {id(ex.http_conn)}")
        ex.response_start(b"200", b"OK", [])
        ex.response_body(b"foo!")
        ex.response_done([])

    @on(ex, "request_body")
    def body(chunk: bytes) -> None:
        print(f"body: {chunk.decode('utf-8', 'replace')}")

    @on(ex, "request_done")
    def done(trailers: RawHeaderListType) -> None:
        print(f"done: {str(trailers)}")


if __name__ == "__main__":
    from thor.loop import run

    sys.stderr.write(f"PID: {os.getpid()}\n")
    h, p = b"127.0.0.1", int(sys.argv[1])
    demo_server = HttpServer(h, p)
    demo_server.on("exchange", test_handler)
    run()
