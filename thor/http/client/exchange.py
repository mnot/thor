from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional, Tuple

from thor.events import EventEmitter
from thor.http.common import (
    Delimiters,
    States,
    check_method,
    check_request_target,
    header_names,
    idempotent_methods,
    strip_hop_by_hop,
)
from thor.http.error import (
    AccessError,
    ConnectError,
    DnsError,
    HttpError,
    OutputError,
    OutputSyntaxError,
    UrlError,
)
from thor.http.uri import parse_uri
from thor.types import OriginType, RawHeaderListType

if TYPE_CHECKING:
    from thor.loop import ScheduledEvent
    from thor.tcp import TcpConnection

    from .client import HttpClient
    from .connection import HttpClientConnection


class HttpClientExchange(EventEmitter):
    # Refuse a method that isn't a token or a request-target with whitespace
    # or control characters, which would change the request-line's shape.
    # False sends them as given, for probing servers.
    strict_request_line = True

    def __init__(self, client: HttpClient) -> None:
        EventEmitter.__init__(self)
        self.client = client
        self.careful = client.careful
        self.method: Optional[bytes] = None
        self.uri: Optional[bytes] = None
        self.req_hdrs: RawHeaderListType = []
        self.req_target: Optional[bytes] = None
        self.authority: Optional[bytes] = None
        self.res_version: Optional[bytes] = None
        self.conn: Optional[HttpClientConnection] = None
        self.origin: Optional[OriginType] = None
        self._req_body = False
        self._req_started = False
        self._req_done_trailers: Optional[RawHeaderListType] = None
        self._req_body_chunks: List[bytes] = []  # kept for replay on retry
        self._req_body_len = 0
        self._req_body_overflow = False
        self._error_sent = False
        self._retries = 0
        self._retry_pending = False
        self._output_q: List[Tuple[Any, ...]] = []
        self._response_complete = False

    def __repr__(self) -> str:
        status = [self.__class__.__module__ + "." + self.__class__.__name__]
        method = (self.method or b"-").decode("utf-8", "replace")
        uri = (self.uri or b"-").decode("utf-8", "replace")
        status.append(f"{method} <{uri}>")
        if self.conn:
            status.append(self.conn.tcp_connected and "connected" or "disconnected")
        return f"<{', '.join(status)} at {id(self):#x}>"

    def request_start(
        self, method: bytes, uri: bytes, req_hdrs: RawHeaderListType
    ) -> None:
        """
        Start a request to uri using method, where req_hdrs is a list of (field_name, field_value)
        for the request headers.
        """
        self.method = method
        self.uri = uri
        self.req_hdrs = req_hdrs
        try:
            scheme, host, port, authority, req_target = parse_uri(self.uri)
            self.origin = (scheme, host, port)
            self.authority = authority
            self.req_target = req_target
        except UrlError as why:
            self.input_error(why, False)
            return
        except (TypeError, ValueError):
            self.input_error(UrlError("Invalid URL"), False)
            return
        self.client.attach_conn(
            self.origin, self.handle_connect, self.handle_connect_error
        )

    def _req_start(self) -> None:
        """
        Queue the request headers for sending.
        """
        if self._req_started or self._error_sent:
            return
        self._req_started = True
        if self.strict_request_line:
            assert self.method is not None and self.req_target is not None
            problem = check_method(self.method) or check_request_target(self.req_target)
            if problem:
                self.input_error(OutputSyntaxError(problem), False)
                return
        req_hdrs = strip_hop_by_hop(self.req_hdrs, also=[b"host"])  # Host is ours
        assert self.authority, "authority not found in _req_start"
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

        top_line = b"%s %s HTTP/1.1" % (self.method, self.req_target)
        if self.conn:
            try:
                self.conn.output_start(top_line, req_hdrs, delimit)
            except OutputError as why:
                self.client.dead_conn(self.conn)
                self.input_error(why)
        else:
            self._output_q.append(("start", top_line, req_hdrs, delimit))

    def request_body(self, chunk: bytes) -> None:
        "Send part of the request body. May be called zero to many times."
        self._req_body = True
        self._req_start()
        if self.conn and self._error_sent:
            return
        if self.method in idempotent_methods and not self._req_body_overflow:
            self._req_body_len += len(chunk)
            if self._req_body_len > self.client.retry_body_limit:
                self._req_body_overflow = True
                self._req_body_chunks = []
            else:
                self._req_body_chunks.append(chunk)
        if self.conn:
            try:
                self.conn.output_body(chunk)
            except OutputError as why:
                self.client.dead_conn(self.conn)
                self.input_error(why)
        else:
            self._output_q.append(("body", chunk))

    def request_done(self, trailers: RawHeaderListType) -> None:
        """
        Signal the end of the request, whether or not there was a body. MUST
        be called exactly once for each request.
        """
        self._req_done_trailers = trailers
        self._req_start()
        if self.conn and self._error_sent:
            return
        if self.conn:
            try:
                self._output_end(trailers)
            except OutputError as why:
                self.client.dead_conn(self.conn)
                self.input_error(why)
        else:
            self._output_q.append(("end", trailers))

    def _output_end(self, trailers: RawHeaderListType) -> None:
        "Finish the request; a close here means the body was shorter than declared."
        assert self.conn, "conn not found in _output_end"
        if self.conn.output_end(trailers):
            self.client.dead_conn(self.conn)
            self.input_error(OutputError("Request body shorter than Content-Length"))

    def res_body_pause(self, paused: bool) -> None:
        "Temporarily stop / restart sending the response body."
        if self.conn and self.conn.tcp_connected:
            self.conn.tcp_conn.pause(paused)

    @property
    def input_transfer_length(self) -> int:
        """Total bytes received from the network for this response (per-message metric)."""
        if self.conn:
            return self.conn.input_transfer_length
        return 0

    @property
    def input_header_length(self) -> int:
        """Bytes received for response headers (per-message metric)."""
        if self.conn:
            return self.conn.input_header_length
        return 0

    def handle_connect(self, conn: HttpClientConnection) -> None:
        "The connection has succeeded."
        self.conn = conn
        self.conn.attach(self)
        queued, self._output_q = self._output_q, []
        try:
            for item in queued:
                if item[0] == "start":
                    self.conn.output_start(*item[1:])
                elif item[0] == "body":
                    self.conn.output_body(*item[1:])
                elif item[0] == "end":
                    self._output_end(*item[1:])
        except OutputError as why:
            self.client.dead_conn(self.conn)
            self.input_error(why)

    def handle_connect_error(self, err_type: str, err_id: int, err_str: str) -> None:
        "The connection has failed."

        if self._retry_pending:
            return
        if err_type == "gai":
            self.input_error(DnsError(err_str), False)
        elif err_type == "access":
            self.input_error(AccessError(err_str), False)
        elif err_type == "retry":
            self.input_error(ConnectError(err_str), False)
        elif self._retries < self.client.retry_limit:
            self._schedule_retry()
        else:
            self.input_error(ConnectError(err_str), False)

    def conn_closed(self, state: States, delimit: Delimiters) -> None:
        "The server closed the connection."

        if self._response_complete or self._error_sent or self._retry_pending:
            return

        if state in [States.QUIET, States.ERROR]:
            pass
        elif delimit == Delimiters.CLOSE:
            if self.conn:
                self.conn.input_end([])
        elif state == States.WAITING:
            if self.method in idempotent_methods:
                if self._retries < self.client.retry_limit:
                    self._schedule_retry()
                else:
                    self.input_error(
                        ConnectError(f"Tried to connect {self._retries + 1} times."),
                        False,
                    )
            else:
                assert self.method, "method not found in conn_closed"
                self.input_error(
                    ConnectError(
                        f"Can't retry {self.method.decode('utf-8', 'replace')} method"
                    ),
                    False,
                )
        else:
            self.input_error(
                ConnectError(
                    "Server dropped connection before the response was complete."
                ),
                False,
            )

    def _schedule_retry(self) -> None:
        self.conn = None
        self._retry_pending = True
        self.client.loop.schedule(self.client.retry_delay, self._retry)

    def _retry(self) -> None:
        "Retry the request."
        self._retry_pending = False
        self._retries += 1
        assert self.origin, "origin not found in _retry"
        if self._req_body_overflow:
            self.input_error(
                ConnectError("Request body too large to retry"),
                False,
            )
            return
        # Non-idempotent requests are only retried before anything is sent,
        # when the queue is still intact.
        if self._req_done_trailers is not None and self.method in idempotent_methods:
            self.conn = None
            self._req_started = False
            self._output_q = []
            self._req_start()
            for chunk in self._req_body_chunks:
                self._output_q.append(("body", chunk))
            self._output_q.append(("end", self._req_done_trailers))
        self.client.attach_conn(
            self.origin, self.handle_connect, self.handle_connect_error
        )

    def req_body_pause(self, paused: bool) -> None:
        "The client needs the application to pause/unpause the request body."
        self.emit("pause", paused)

    # Methods called by HttpClientConnection

    def input_end_notify(self, trailers: RawHeaderListType) -> None:
        "Indicate that the response body is complete."
        if self._response_complete or self._error_sent:
            return
        self._response_complete = True
        self.emit("response_done", trailers)

    def input_error(self, err: HttpError, close: bool = True) -> None:
        "Indicate an error state."
        if self._error_sent:
            return
        self._error_sent = True
        self.emit("error", err)
