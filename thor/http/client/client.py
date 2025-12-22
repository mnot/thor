from __future__ import annotations
from collections import defaultdict
import socket
from typing import Optional, Callable, List, Dict, Tuple, TYPE_CHECKING

import thor
from thor.loop import LoopBase
from thor.http.common import OriginType
from thor.tcp import TcpConnection

from .initiate import HttpConnectionInitiate

if TYPE_CHECKING:
    from .exchange import HttpClientExchange

class HttpClient:
    "An asynchronous HTTP client."

    def __init__(self, loop: Optional[LoopBase] = None) -> None:
        self.loop = loop or thor.loop._loop
        self.idle_timeout: int = 60  # seconds
        self.connect_attempts: int = 3
        self.connect_timeout: int = 3  # seconds
        self.read_timeout: Optional[int] = None  # seconds
        self.retry_limit: int = 2
        self.retry_delay: float = 0.5  # seconds
        self.max_server_conn: int = 6
        self.check_ip: Optional[Callable[[str], bool]] = None
        self.careful: bool = True
        self._idle_conns: Dict[OriginType, List[TcpConnection]] = defaultdict(list)
        self.conn_counts: Dict[OriginType, int] = defaultdict(int)
        self._req_q: Dict[OriginType, List[Tuple[Callable, Callable]]] = defaultdict(
            list
        )
        self.loop.once("stop", self._close_conns)

    def exchange(self) -> HttpClientExchange:
        from .exchange import HttpClientExchange  # pylint: disable=import-outside-toplevel
        return HttpClientExchange(self)

    def attach_conn(
        self,
        origin: OriginType,
        handle_connect: Callable,
        handle_connect_error: Callable,
    ) -> None:
        "Find an idle connection for origin, or create a new one."
        while True:
            try:
                tcp_conn = self._idle_conns[origin].pop()
            except IndexError:  # No idle conns available.
                if not self._idle_conns[origin]:
                    del self._idle_conns[origin]
                self._new_conn(origin, handle_connect, handle_connect_error)
                break
            if tcp_conn.tcp_connected:
                tcp_conn.remove_listeners("data", "pause", "close")
                tcp_conn.pause(True)
                if hasattr(tcp_conn, "idler"):
                    tcp_conn.idler.delete()
                handle_connect(tcp_conn)
                break

    def release_conn(self, exchange: HttpClientExchange) -> None:
        "Add an idle connection back to the pool."
        tcp_conn = exchange.tcp_conn
        if tcp_conn:
            tcp_conn.remove_listeners("data", "pause", "close")
            exchange.tcp_conn = None
            if tcp_conn.tcp_connected:
                origin = exchange.origin
                assert origin, "origin not found in release_conn"

                def idle_close() -> None:
                    "Remove the connection from the pool when it closes."
                    if hasattr(tcp_conn, "idler"):
                        tcp_conn.idler.delete()
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
                    tcp_conn.once("close", idle_close)
                    tcp_conn.idler = self.loop.schedule(  # type: ignore[attr-defined]
                        self.idle_timeout, idle_close
                    )
                    self._idle_conns[origin].append(tcp_conn)
                else:
                    self.dead_conn(exchange)

    def dead_conn(self, exchange: HttpClientExchange) -> None:
        "Notify the client that a connection is dead."
        origin = exchange.origin
        assert origin, "origin not found in dead_conn"
        if exchange.tcp_conn and exchange.tcp_conn.tcp_connected:
            exchange.tcp_conn.close()
        exchange.tcp_conn = None
        self.conn_counts[origin] -= 1
        if self.conn_counts[origin] == 0:
            del self.conn_counts[origin]
            if self._req_q[origin]:
                (handle_connect, handle_connect_error) = self._req_q[origin].pop(0)
                self._new_conn(origin, handle_connect, handle_connect_error)

    def _new_conn(
        self,
        origin: OriginType,
        handle_connect: Callable[[TcpConnection], None],
        handle_error: Callable[[str, int, str], None],
    ) -> None:
        "Create a new connection."
        if self.conn_counts[origin] >= self.max_server_conn:
            self._req_q[origin].append((handle_connect, handle_error))
            return
        HttpConnectionInitiate(self, origin, handle_connect, handle_error)

    def _close_conns(self) -> None:
        "Close all idle HTTP connections."
        for conn_list in list(self._idle_conns.values()):
            for conn in list(conn_list):
                try:
                    conn.close()
                except socket.error:
                    pass
        self._idle_conns.clear()
