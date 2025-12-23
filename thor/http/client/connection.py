from __future__ import annotations
from typing import Optional, List, Tuple, TYPE_CHECKING

from thor.events import EventEmitter
from thor.tcp import TcpConnection
from thor.http.common import (
    HttpMessageHandler,
    States,
    Delimiters,
    RawHeaderListType,
    OriginType,
    no_body_status,
)
from thor.http import error
from thor.http.error import (
    StartLineError,
    HttpVersionError,
)
from thor.loop import ScheduledEvent

if TYPE_CHECKING:
    from .client import HttpClient
    from .exchange import HttpClientExchange


class HttpClientConnection(HttpMessageHandler, EventEmitter):
    """
    A persistent connection for the HTTP client.
    """

    default_state = States.WAITING

    def __init__(
        self, client: HttpClient, origin: OriginType, tcp_conn: TcpConnection
    ) -> None:
        self.client = client
        self.origin = origin
        self.tcp_conn = tcp_conn
        HttpMessageHandler.__init__(self)
        EventEmitter.__init__(self)
        self.active_exchange: Optional[HttpClientExchange] = None
        self.last_active_exchange: Optional[HttpClientExchange] = None
        self.res_version: Optional[bytes] = None
        self.reusable = False
        self.idler: Optional[ScheduledEvent] = None
        self._read_timeout_ev: Optional[ScheduledEvent] = None

        self.tcp_conn.on("data", self.handle_input)
        self.tcp_conn.once("close", self._conn_closed)
        self.tcp_conn.on("pause", self._conn_paused)

    def attach(self, exchange: HttpClientExchange) -> None:
        self.active_exchange = exchange
        self.last_active_exchange = None
        self.careful = exchange.careful
        self.reusable = False
        self._input_state = self.default_state
        self._input_delimit = Delimiters.NONE
        self._input_body_left = 0
        self._output_state = States.WAITING
        self._output_delimit = Delimiters.NONE
        self.tcp_conn.pause(False)
        if self._input_buffer:
            self.handle_input(b"")
        self.set_timeout(self.client.connect_timeout, "connect")

    def detach(self) -> None:
        self.last_active_exchange = self.active_exchange
        self.active_exchange = None

    def close(self) -> None:
        self.tcp_conn.close()

    def kill(self) -> None:
        if self.tcp_connected:
            self.close()
        self.client.dead_conn(self)

    def set_timeout(self, timeout: float, kind: str) -> None:
        self.clear_timeout()
        self._read_timeout_ev = self.client.loop.schedule(
            timeout, self.input_error, error.ReadTimeoutError(kind)
        )

    def clear_timeout(self) -> None:
        if self._read_timeout_ev:
            self._read_timeout_ev.delete()
            self._read_timeout_ev = None

    @property
    def tcp_connected(self) -> bool:
        return self.tcp_conn.tcp_connected

    def _conn_closed(self) -> None:
        if self._input_buffer:
            self.handle_input(b"")
        self.clear_timeout()
        self.client.dead_conn(self)
        self.emit("close")
        if self.active_exchange:
            self.active_exchange.conn_closed(self._input_state, self._input_delimit)
        elif self.last_active_exchange:
            self.last_active_exchange.conn_closed(
                self._input_state, self._input_delimit
            )

    def _conn_paused(self, paused: bool) -> None:
        self.emit("pause", paused)
        if self.active_exchange:
            self.active_exchange.req_body_pause(paused)

    # HttpMessageHandler overrides

    def handle_input(self, inbytes: bytes) -> None:
        if self.active_exchange or not self.reusable:
            HttpMessageHandler.handle_input(self, inbytes)
        else:
            self._input_buffer.append(inbytes)

    # HttpMessageHandler implementation

    def input_start(
        self,
        top_line: bytes,
        hdr_tuples: RawHeaderListType,
        conn_tokens: List[bytes],
        transfer_codes: List[bytes],
        content_length: Optional[int],
    ) -> Tuple[bool, bool]:
        try:
            proto_version, status_txt = top_line.split(None, 1)
            proto, self.res_version = proto_version.rsplit(b"/", 1)
        except (ValueError, IndexError):
            self.input_error(StartLineError(top_line.decode("utf-8", "replace")))
            raise ValueError
        if proto != b"HTTP" or self.res_version not in [b"1.0", b"1.1"]:
            self.input_error(HttpVersionError(proto_version.decode("utf-8", "replace")))
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
                self.reusable = True

        if self.reusable:
            self.default_state = States.WAITING
        else:
            self.default_state = States.QUIET

        is_final = not res_code.startswith(b"1")
        allows_body = is_final and (res_code not in no_body_status)

        if self.active_exchange:
            if self.active_exchange.method == b"HEAD":
                allows_body = False

            if is_final:
                self.active_exchange.res_version = self.res_version
                self.active_exchange.emit(
                    "response_start", res_code, res_phrase, hdr_tuples
                )
            else:
                self.active_exchange.emit(
                    "response_nonfinal", res_code, res_phrase, hdr_tuples
                )

        return allows_body, is_final

    def input_body(self, chunk: bytes) -> None:
        if self.active_exchange:
            self.active_exchange.emit("response_body", chunk)

    def input_end(self, trailers: RawHeaderListType) -> None:
        self.clear_timeout()
        exchange = self.active_exchange
        if self.reusable:
            self.client.release_conn(self)
        else:
            self.client.dead_conn(self)
        if exchange:
            exchange.input_end_notify(trailers)

    def input_error(self, err: error.HttpError) -> None:
        self.clear_timeout()
        exchange = self.active_exchange or self.last_active_exchange
        self.client.dead_conn(self)
        if exchange:
            exchange.input_error(err)

    def output(self, data: bytes) -> None:
        self.tcp_conn.write(data)

    def output_done(self) -> None:
        pass
