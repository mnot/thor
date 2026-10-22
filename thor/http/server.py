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
from typing import Any, Iterator, List, Optional, Set, Tuple

from thor.events import EventEmitter, on
from thor.http.common import (
    Delimiters,
    HttpMessageHandler,
    States,
    check_method,
    check_request_target,
    header_names,
    strip_hop_by_hop,
    no_body_status,
    output_content_length,
)
from thor.http.error import (
    ConnectError,
    HostRequiredError,
    HttpError,
    HttpVersionError,
    RequestLineError,
    RequestTransferEncodingError,
    TooManyMsgsError,
    TransferCodeError,
)
from thor.loop import LoopBase, Timer
from thor.tcp import TcpConnection, TcpServer
from thor.types import RawHeaderListType


def response_framing(
    method: bytes,
    status_code: bytes,
    req_version: bytes,
    persistent: bool,
    res_hdrs: RawHeaderListType,
) -> Tuple[RawHeaderListType, Delimiters]:
    """
    Decide how a final response to the given request is delimited, and
    return the headers to send (hop-by-hop ones dropped, framing and
    Connection added) along with the delimiter.
    """
    res_hdrs = strip_hop_by_hop(res_hdrs)
    body_len = output_content_length(res_hdrs)
    # A final response with a 1xx status is not an interim response; use
    # response_nonfinal for those.
    bodiless = (
        method == b"HEAD"
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
    elif req_version == b"1.1":
        delimit = Delimiters.CHUNKED
        res_hdrs.append((b"Transfer-Encoding", b"chunked"))
    else:
        delimit = Delimiters.CLOSE
    if delimit is Delimiters.CLOSE or not persistent:
        res_hdrs.append((b"Connection", b"close"))
    elif req_version == b"1.0":
        res_hdrs.append((b"Connection", b"keep-alive"))
    return res_hdrs, delimit


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
        tcp_conn.on("close", http_conn.peer_closed)
        tcp_conn.on("sent", http_conn.output_sent)
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


class ExchangeQueue:
    """
    A connection's exchanges, in request order.

    The last one is receiving its request, if that isn't complete yet; the
    first that hasn't been answered is the one whose response goes out next.
    An exchange leaves the queue once its request and response are both
    complete.
    """

    def __init__(self) -> None:
        self._exchanges: List["HttpServerExchange"] = []

    def __len__(self) -> int:
        return len(self._exchanges)

    def __iter__(self) -> Iterator["HttpServerExchange"]:
        return iter(self._exchanges)

    def add(self, exchange: "HttpServerExchange") -> None:
        self._exchanges.append(exchange)

    def retire(self, exchange: "HttpServerExchange") -> None:
        "Drop an exchange once its request and response are both complete."
        if (
            exchange.req_complete
            and exchange.res_complete
            and exchange in self._exchanges
        ):
            self._exchanges.remove(exchange)

    def clear(self) -> None:
        self._exchanges = []

    @property
    def last(self) -> "HttpServerExchange":
        "The most recently queued exchange; the one a request body belongs to."
        return self._exchanges[-1]

    @property
    def receiving(self) -> Optional["HttpServerExchange"]:
        "The exchange whose request is still being received, if any."
        if self._exchanges and not self._exchanges[-1].req_complete:
            return self._exchanges[-1]
        return None

    @property
    def next_to_answer(self) -> Optional["HttpServerExchange"]:
        "The oldest exchange that hasn't been answered, if any."
        for exchange in self._exchanges:
            if not exchange.res_complete:
                return exchange
        return None

    def answered_before(self, exchange: Optional["HttpServerExchange"]) -> bool:
        """
        Has every exchange ahead of this one been answered? With None, has
        every exchange been answered?
        """
        if exchange is None:
            ahead = self._exchanges
        else:
            ahead = self._exchanges[: self._exchanges.index(exchange)]
        return all(ex.res_complete for ex in ahead)

    def unstarted(self) -> Iterator["HttpServerExchange"]:
        """
        Exchanges the application hasn't been told about yet. Each is checked
        when reached, since starting one can answer (and so dequeue) others.
        """
        for exchange in list(self._exchanges):
            if exchange in self._exchanges and not exchange.started:
                yield exchange


class HttpServerConnection(HttpMessageHandler, EventEmitter):
    "A handler for an HTTP server connection."

    default_state = States.WAITING
    max_pipeline_requests = 100

    def __init__(self, tcp_conn: TcpConnection, server: HttpServer) -> None:
        HttpMessageHandler.__init__(self)
        EventEmitter.__init__(self)
        self.tcp_conn: Optional[TcpConnection] = tcp_conn
        self.server = server
        self.exchanges = ExchangeQueue()
        self.output_paused = False
        self._pending_error: Optional[HttpError] = None  # deferred error
        self._close_error: Optional[HttpError] = None  # why we're closing
        # Each closes the connection: when it has sat idle too long, when a
        # request body stops arriving, or when a response doesn't come.
        self._idler = Timer(self.server.loop, self.close_conn)
        self._req_timer = Timer(self.server.loop, self.close_conn)
        self._res_timer = Timer(self.server.loop, self.close_conn)
        self._req_in_progress = False  # request body incomplete
        self._req_paused = False  # the application asked for it
        self._input_held = False  # body is waiting for the application to be told
        self._res_timer_ex: Optional[HttpServerExchange] = None
        self._in_input = False
        self._peer_closed = False  # the client won't send any more
        self._linger_timer = Timer(self.server.loop, self.close_conn)
        self._arm_idle_timer()

    @property
    def is_idle(self) -> bool:
        return not self.exchanges

    def req_body_pause(self, paused: bool) -> None:
        """
        Indicate that the server should pause (True) or unpause (False) the
        request.
        """
        self._req_paused = paused
        self._update_input_pause()

    def update_input_hold(self) -> None:
        """
        Stop reading while any request body is being held back for an
        application that hasn't been told of the request yet; resume once it
        has, so a client can't make the server buffer without limit.
        """
        self._input_held = any(exchange.holding for exchange in self.exchanges)
        self._update_input_pause()

    def _update_input_pause(self) -> None:
        paused = self._req_paused or self._input_held
        if self.tcp_conn:
            self.tcp_conn.pause(paused)
        # a paused request isn't the client's fault
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
        self._req_timer.stop()

    def _start_req_timer(self) -> None:
        "(Re)start the inactivity timer for the request being received."
        if self.tcp_conn and not (self._req_paused or self._input_held):
            self._req_timer.start(self.server.request_timeout)
        else:
            self._req_timer.stop()

    def handle_input(self, inbytes: bytes) -> None:
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
        self._res_timer.stop()
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
            ex = self.exchanges.next_to_answer
            if ex and ex.req_complete and ex.started:
                target = ex
        if target is self._res_timer_ex:
            return
        self._stop_res_timer()
        if target and timeout:
            self._res_timer_ex = target
            self._res_timer.start(timeout)

    def _stop_linger_timer(self) -> None:
        self._linger_timer.stop()

    def _stop_idle_timer(self) -> None:
        self._idler.stop()

    def _arm_idle_timer(self) -> None:
        self._idler.start(self.server.idle_timeout)

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
        if self.server.shutting_down or self._peer_closed:
            self.close_conn()
        elif restart or not self._idler.armed:
            self._arm_idle_timer()

    def close_conn(self) -> None:
        "Close the connection."
        self._stop_idle_timer()
        self._stop_req_timer()
        self._stop_res_timer()
        self._stop_linger_timer()
        if self.tcp_conn:
            if self.tcp_conn.tcp_connected:
                self.tcp_conn.close()
            self.tcp_conn = None  # even if it was already gone
        self._abandon_exchanges()

    def _abandon_exchanges(self) -> None:
        """
        Tell the application about each response that can no longer be sent.
        A handler that raises doesn't stop the others being told; the first
        exception is raised once they all have been.
        """
        failure: Optional[Exception] = None
        for exchange in list(self.exchanges):
            try:
                exchange.connection_lost(self._close_error)
            except Exception as why:  # pylint: disable=broad-exception-caught
                failure = failure or why
        if failure:
            raise failure

    def exchange_done(self, exchange: "HttpServerExchange") -> None:
        exchange.res_complete = True
        self.exchanges.retire(exchange)
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
        # An error in a request body belongs to the exchange receiving it; an
        # error in a header block has no exchange, so everything queued is ahead.
        in_flight = self.exchanges.receiving
        if not self.exchanges.answered_before(in_flight):
            return
        self._pending_error = None
        self._close_error = err
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

    def peer_closed(self) -> None:
        """
        The client has closed its side. It may still be reading (a half-close),
        so what it has sent gets answered and the connection closes once that
        is done; but a client that has gone away looks the same, so the
        connection is dropped if nothing has been sent to it for
        request_timeout (a stall bound, not a total), and at once during
        shutdown.
        """
        self._peer_closed = True
        self._update_idle_timer()
        if not self.tcp_conn or self.is_idle:
            return
        if self.server.shutting_down:
            self.close_conn()
            return
        self._arm_linger()

    def _arm_linger(self) -> None:
        self._linger_timer.start(self.server.request_timeout)

    def output_sent(self) -> None:
        "Bytes went out: a half-closed client is still reading, so give it longer."
        if self._linger_timer.armed:
            self._arm_linger()

    def conn_closed(self) -> None:
        "The server connection has closed."
        self.server.connections.discard(self)
        self._stop_idle_timer()
        self._stop_req_timer()
        self._stop_res_timer()
        self._stop_linger_timer()
        self.tcp_conn = None
        try:
            self._abandon_exchanges()
        finally:
            self.exchanges.clear()
            self._pending_error = None

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
        problem = check_method(method) or check_request_target(uri)
        if problem:
            # Reported to whoever listens on this connection, then accepted as
            # is: the application decides what to make of it.
            self.input_error(RequestLineError(problem))
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
        if len(self.exchanges) >= self.max_pipeline_requests:
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
        self.exchanges.add(exchange)
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
        self.exchanges.last.deliver("request_body", chunk)

    def input_end(self, trailers: RawHeaderListType) -> None:
        "Indicate that the request body is complete."
        self._req_in_progress = False
        self._stop_req_timer()
        ex = self.exchanges.last
        ex.req_complete = True
        ex.deliver("request_done", trailers)
        self.exchanges.retire(ex)
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
        body = err.desc.encode("utf-8")
        if err.detail:
            body += b" (%s)" % err.detail.encode("utf-8")
        hdrs = [
            (b"Content-Type", b"text/plain"),
            (b"Content-Length", b"%d" % len(body)),
        ]
        # Framed like any other response to a request that doesn't persist:
        # counted, with Connection: close, whatever the request's version.
        ex = HttpServerExchange(self, b"", b"", [], b"1.1", persistent=False)
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
        for exchange in self.exchanges.unstarted():
            if self.output_paused or not self.tcp_conn:
                break
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
        self._lost = False  # the application has been told the connection went
        # request events that arrived before the application was told of it
        self._deferred: List[Tuple[str, Tuple[Any, ...]]] = []

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
        deferred, self._deferred = self._deferred, []
        for event, args in deferred:
            self.deliver(event, *args)
        if deferred:
            self.http_conn.update_input_hold()

    @property
    def holding(self) -> bool:
        "Is request body waiting for request_start?"
        return any(event == "request_body" for event, _ in self._deferred)

    def deliver(self, event: str, *args: Any) -> None:
        """
        Pass a request body event to the application, holding it until
        request_start has gone out first. Dropped if the response has
        already finished.
        """
        if self.res_complete:
            return
        if self.started:
            self.emit(event, *args)
        else:
            self._deferred.append((event, args))
            self.http_conn.update_input_hold()

    def connection_lost(self, cause: Optional[HttpError] = None) -> None:
        """
        The connection closed; if a response was owed, say so, once. The cause
        is the error that made the server close it, if there was one.
        """
        if self._lost or self.res_complete or not self.started:
            return
        self._lost = True
        self.emit(
            "error",
            cause
            or ConnectError("Connection closed before the response was sent."),
        )

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
        res_hdrs, delimit = response_framing(
            self.method, status_code, self.req_version, self.persistent, res_hdrs
        )
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
        self.res_complete = True  # closing now doesn't leave it unanswered
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
