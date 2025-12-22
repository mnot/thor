# pylint: disable=protected-access
from __future__ import annotations
from typing import Optional, List, Tuple, TYPE_CHECKING

from thor.events import EventEmitter
from thor.tcp import TcpConnection
from thor.loop import ScheduledEvent
from thor.http.uri import parse_uri
from thor.http.common import (
    HttpMessageHandler,
    States,
    Delimiters,
    idempotent_methods,
    no_body_status,
    header_names,
    RawHeaderListType,
    OriginType,
)
from thor.http.error import (
    UrlError,
    ConnectError,
    AccessError,
    ReadTimeoutError,
    StartLineError,
    HttpVersionError,
    HttpError,
    DnsError,
)

if TYPE_CHECKING:
    from .client import HttpClient

req_rm_hdrs = [
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailers",
    b"transfer-encoding",
    b"upgrade",
    b"host",
]

class HttpClientExchange(HttpMessageHandler, EventEmitter):
    default_state = States.QUIET

    def __init__(self, client: HttpClient) -> None:
        HttpMessageHandler.__init__(self)
        EventEmitter.__init__(self)
        self.client = client
        self.careful = client.careful
        self.method: Optional[bytes] = None
        self.uri: Optional[bytes] = None
        self.req_hdrs: RawHeaderListType = []
        self.req_target: Optional[bytes] = None
        self.authority: Optional[bytes] = None
        self.res_version: Optional[bytes] = None
        self.tcp_conn: Optional[TcpConnection] = None
        self.origin: Optional[OriginType] = None
        self._conn_reusable = False
        self._req_body = False
        self._req_started = False
        self._retries = 0
        self._read_timeout_ev: Optional[ScheduledEvent] = None
        self._output_buffer: List[bytes] = []

    def __repr__(self) -> str:
        status = [self.__class__.__module__ + "." + self.__class__.__name__]
        method = (self.method or b"-").decode("utf-8", "replace")
        uri = (self.uri or b"-").decode("utf-8", "replace")
        status.append(f"{method} <{uri}>")
        if self.tcp_conn:
            status.append(
                self.tcp_conn.tcp_connected and "connected" or "disconnected"
            )
        status.append(HttpMessageHandler.__repr__(self))
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
            (scheme, host, port, authority, req_target) = parse_uri(self.uri)
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
            self.origin, self._handle_connect, self._handle_connect_error
        )

    def _req_start(self) -> None:
        """
        Queue the request headers for sending.
        """
        if self._req_started:
            return
        self._req_started = True
        req_hdrs = [i for i in self.req_hdrs if not i[0].lower() in req_rm_hdrs]
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
        self._input_state = States.WAITING
        self.output_start(
            b"%s %s HTTP/1.1" % (self.method, self.req_target), req_hdrs, delimit
        )

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
        if close:
            self.client.dead_conn(self)

    def res_body_pause(self, paused: bool) -> None:
        "Temporarily stop / restart sending the response body."
        if self.tcp_conn and self.tcp_conn.tcp_connected:
            self.tcp_conn.pause(paused)

    # Methods called by tcp

    def _handle_connect(self, tcp_conn: TcpConnection) -> None:
        "The connection has succeeded."
        self.tcp_conn = tcp_conn
        self._set_read_timeout("connect")
        tcp_conn.on("data", self.handle_input)
        tcp_conn.once("close", self._conn_closed)
        tcp_conn.on("pause", self._req_body_pause)
        self.output(b"")  # kick the output buffer
        self.tcp_conn.pause(False)

    def _handle_connect_error(self, err_type: str, err_id: int, err_str: str) -> None:
        "The connection has failed."
        self._clear_read_timeout()
        self.client.dead_conn(self)
        if err_type == "gai":
            self.input_error(DnsError(err_str), False)
        elif err_type == "access":
            self.input_error(AccessError(err_str), False)
        elif err_type == "retry":
            self.input_error(ConnectError(err_str), False)
        elif self._retries < self.client.retry_limit:
            self.client.loop.schedule(self.client.retry_delay, self._retry)
        else:
            self.input_error(ConnectError(err_str), False)

    def _conn_closed(self) -> None:
        "The server closed the connection."
        self._clear_read_timeout()
        self.client.dead_conn(self)
        if self._input_buffer:
            self.handle_input(b"")
        if self._input_state in [States.QUIET, States.ERROR]:
            pass  # nothing to see here
        elif self._input_delimit == Delimiters.CLOSE:
            self.input_end([])
        elif self._input_state == States.WAITING:
            if self.method in idempotent_methods:
                if self._retries < self.client.retry_limit:
                    self.client.loop.schedule(self.client.retry_delay, self._retry)
                else:
                    self.input_error(
                        ConnectError(f"Tried to connect {self._retries + 1} times."),
                        False,
                    )
            else:
                assert self.method, "method not found in _conn_closed"
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

    def _retry(self) -> None:
        "Retry the request."
        self._retries += 1
        assert self.origin, "origin not found in _retry"
        self.client.attach_conn(
            self.origin, self._handle_connect, self._handle_connect_error
        )

    def _req_body_pause(self, paused: bool) -> None:
        "The client needs the application to pause/unpause the request body."
        self.emit("pause", paused)

    # Methods called by common.HttpMessageHandler

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
        self._clear_read_timeout()
        try:
            proto_version, status_txt = top_line.split(None, 1)
            proto, self.res_version = proto_version.rsplit(b"/", 1)
        except (ValueError, IndexError):
            self.input_error(StartLineError(top_line.decode("utf-8", "replace")), True)
            raise ValueError
        if proto != b"HTTP" or self.res_version not in [b"1.0", b"1.1"]:
            self.input_error(
                HttpVersionError(proto_version.decode("utf-8", "replace")), True
            )
            raise ValueError
        try:
            res_code, res_phrase = status_txt.split(None, 1)
        except ValueError:
            res_code = status_txt.rstrip()
            res_phrase = b""
        if b"close" not in conn_tokens:
            if (
                self.res_version == b"1.0" and b"keep-alive" in conn_tokens
            ) or self.res_version in [b"1.1"]:
                self._conn_reusable = True
        self._set_read_timeout("start")
        is_final = not res_code.startswith(b"1")
        allows_body = (
            is_final and (res_code not in no_body_status) and (self.method != b"HEAD")
        )
        if is_final:
            self.emit("response_start", res_code, res_phrase, hdr_tuples)
        else:
            self.emit("response_nonfinal", res_code, res_phrase, hdr_tuples)
        return allows_body, is_final

    def input_body(self, chunk: bytes) -> None:
        "Process a response body chunk from the wire."
        self._clear_read_timeout()
        self.emit("response_body", chunk)
        self._set_read_timeout("body")

    def input_end(self, trailers: RawHeaderListType) -> None:
        "Indicate that the response body is complete."
        self._clear_read_timeout()
        if self._conn_reusable:
            self.client.release_conn(self)
        else:
            self.client.dead_conn(self)
        self.emit("response_done", trailers)

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
        self.emit("error", err)

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
                self.client.read_timeout, self.input_error, ReadTimeoutError(kind)
            )

    def _clear_read_timeout(self) -> None:
        "Clear the read timeout."
        if self._read_timeout_ev:
            self._read_timeout_ev.delete()
