from __future__ import annotations
from collections import defaultdict
import socket
from typing import Optional, Callable, List, Dict, Tuple

import thor
from thor.loop import LoopBase
from thor.http.common import OriginType

from .initiate import initiate_connection
from .exchange import HttpClientExchange
from .connection import HttpClientConnection


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
        self._idle_conns: Dict[OriginType, List[HttpClientConnection]] = defaultdict(
            list
        )
        self.conn_counts: Dict[OriginType, int] = defaultdict(int)
        self._req_q: Dict[OriginType, List[Tuple[Callable, Callable]]] = defaultdict(
            list
        )
        self.loop.once("stop", self._close_conns)

    def exchange(self) -> HttpClientExchange:
        return HttpClientExchange(self)

    def attach_conn(
        self,
        origin: OriginType,
        handle_connect: Callable[[HttpClientConnection], None],
        handle_connect_error: Callable,
    ) -> None:
        "Find an idle connection for origin, or create a new one."
        while True:
            try:
                conn = self._idle_conns[origin].pop()
                if not conn.tcp_connected:
                    self._conn_is_dead(conn)
                    continue
            except IndexError:  # No idle conns available.
                if origin in self._idle_conns and not self._idle_conns[origin]:
                    del self._idle_conns[origin]
                self._new_conn(origin, handle_connect, handle_connect_error)
                break
            if conn.tcp_connected:
                if conn.idler:
                    conn.idler.delete()
                handle_connect(conn)
                break

    def release_conn(self, conn: HttpClientConnection) -> None:
        "Add an idle connection back to the pool."
        conn.detach()
        if not conn.tcp_connected:
            self._conn_is_dead(conn)
            return

        origin = conn.origin
        assert origin, "origin not found in release_conn"

        def idle_close() -> None:
            "Remove the connection from the pool when it closes."
            if conn.idler:
                conn.idler.delete()
            self._conn_is_dead(conn)
            try:
                self._idle_conns[origin].remove(conn)
                if not self._idle_conns[origin]:
                    del self._idle_conns[origin]
            except (KeyError, ValueError):
                pass

        if self._req_q[origin]:
            handle_connect = self._req_q[origin].pop(0)[0]
            handle_connect(conn)
        elif self.idle_timeout > 0:
            conn.once("close", idle_close)
            conn.idler = self.loop.schedule(self.idle_timeout, idle_close)
            self._idle_conns[origin].append(conn)
        else:
            conn.close()
            self._conn_is_dead(conn)

    def dead_conn(self, conn: HttpClientConnection) -> None:
        "Notify the client that a connection is dead."
        conn.detach()
        if conn.tcp_connected:
            conn.close()
        self._conn_is_dead(conn)

    def _conn_is_dead(self, conn: HttpClientConnection) -> None:
        origin = conn.origin
        assert origin, "origin not found in _conn_is_dead"
        self.conn_counts[origin] -= 1
        if self.conn_counts[origin] <= 0:
            if origin in self.conn_counts:
                del self.conn_counts[origin]
            if self._req_q[origin]:
                (handle_connect, handle_connect_error) = self._req_q[origin].pop(0)
                self._new_conn(origin, handle_connect, handle_connect_error)

    def _new_conn(
        self,
        origin: OriginType,
        handle_connect: Callable[[HttpClientConnection], None],
        handle_error: Callable[[str, int, str], None],
    ) -> None:
        "Create a new connection."
        if self.conn_counts[origin] >= self.max_server_conn:
            self._req_q[origin].append((handle_connect, handle_error))
            return
        initiate_connection(self, origin, handle_connect, handle_error)

    def _close_conns(self) -> None:
        "Close all idle HTTP connections."
        for conn_list in list(self._idle_conns.values()):
            for conn in list(conn_list):
                try:
                    conn.close()
                except socket.error:
                    pass
        self._idle_conns.clear()
