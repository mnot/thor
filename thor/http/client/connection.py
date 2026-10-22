from __future__ import annotations

# pylint: disable=raise-missing-from

from typing import TYPE_CHECKING, List, Optional, Tuple

from thor.events import EventEmitter
from thor.http import error
from thor.http.common import (
    Delimiters,
    HttpMessageHandler,
    States,
    no_body_status,
)
from thor.http.error import (
    HttpVersionError,
    StartLineError,
)
from thor.loop import ScheduledEvent
from thor.tcp import TcpConnection
from thor.types import OriginType, RawHeaderListType

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
        self._counted_dead = False  # removed from client.conn_counts
        self.idler: Optional[ScheduledEvent] = None
        self._read_timeout_ev: Optional[ScheduledEvent] = None
        self._in_read = False
        self._response_ended = False  # complete, but the read isn't settled yet
        self._end_trailers: RawHeaderListType = []

        self.tcp_conn.on("data", self._handle_data)
        self.tcp_conn.once("close", self._conn_closed)
        self.tcp_conn.on("pause", self._conn_paused)
        self.tcp_conn.on("sent", self._restart_timeout)

    def attach(self, exchange: HttpClientExchange) -> None:
        self.active_exchange = exchange
        self.last_active_exchange = None
        self.careful = exchange.careful
        self.reusable = False
        self._input_state = self.default_state
        self._input_delimit = Delimiters.NONE
        self._input_body_left = 0
        self._input_chunk_term = None
        self._output_state = States.WAITING
        self._output_delimit = Delimiters.NONE
        # Reset per-message metrics
        self.input_header_length = 0
        self.input_transfer_length = 0
        self.tcp_conn.pause(False)
        self.set_timeout(self._read_timeout(), "read")

    def detach(self) -> None:
        # dead_conn can run more than once; keep the exchange from the first.
        if self.active_exchange:
            self.last_active_exchange = self.active_exchange
        self.active_exchange = None

    def close(self) -> None:
        self.tcp_conn.close()

    def kill(self) -> None:
        if self.tcp_connected:
            self.close()
        self.client.dead_conn(self)

    def set_timeout(self, timeout: Optional[float], kind: str) -> None:
        self.clear_timeout()
        if timeout is None:
            return
        self._read_timeout_ev = self.client.loop.schedule(
            timeout, self.input_error, error.ReadTimeoutError(kind)
        )

    def _read_timeout(self) -> Optional[float]:
        if self.client.read_timeout is not None:
            return self.client.read_timeout
        return self.client.connect_timeout

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

    def _handle_data(self, inbytes: bytes) -> None:
        self._in_read = True
        try:
            self.handle_input(inbytes)
        finally:
            self._in_read = False
        self._settle()

    def _settle(self) -> None:
        """
        Once a read has been fully parsed, finish a response that completed
        during it. Waiting until then, rather than finishing from inside
        input_end, means anything unexpected after the response is seen
        before the connection is handed on, so a queued request is never
        written onto a connection that's about to be found untrustworthy.
        """
        if not self._response_ended:
            return
        self._response_ended = False
        self._finish(self._end_trailers)

    def _finish(self, trailers: RawHeaderListType) -> None:
        "Hand the connection back (or drop it), then tell the exchange."
        exchange = self.active_exchange or self.last_active_exchange
        if self.reusable:
            self.client.release_conn(self)
        else:
            self.client.dead_conn(self)
        if exchange:
            exchange.input_end_notify(trailers)

    def _handle_input_step(self, inbytes: bytes) -> Optional[bytes]:
        if self.reusable and not self.active_exchange:
            # Idle in the pool: no response is outstanding, so nothing should arrive.
            if inbytes.strip():
                self._extra_data(inbytes)
            return None
        if inbytes:
            self._restart_timeout()
        return HttpMessageHandler._handle_input_step(self, inbytes)

    def _restart_timeout(self) -> None:
        "Inactivity timeout: restart on data received or sent, if one is running."
        if self._read_timeout_ev:
            self.set_timeout(self._read_timeout(), "read")

    def _extra_data(self, inbytes: bytes) -> None:
        "Data arrived on an idle connection; it can't be trusted."
        self.reusable = False
        self._input_state = States.ERROR
        self._input_buffer = []
        self.clear_timeout()
        self.client.dead_conn(self)

    def input_extra_data(self, inbytes: bytes) -> None:
        "Data followed a complete response; the response stands, the connection doesn't."
        if not self._response_ended:
            HttpMessageHandler.input_extra_data(self, inbytes)
            return
        self.reusable = False
        self._input_state = States.ERROR
        self._input_buffer = []

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
        if self.reusable and self._in_read:
            # Whatever else this read holds is not a response; _settle finishes
            # once the read has been looked at.
            self._input_state = States.QUIET
            self._response_ended = True
            self._end_trailers = trailers
            return
        # Not reusable, or a close-delimited body ending in _conn_closed (after
        # dead_conn has detached): nothing can follow, so finish now.
        self._finish(trailers)

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
