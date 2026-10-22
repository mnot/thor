from __future__ import annotations

import socket
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

from thor.loop import LoopBase, Timer, _loop
from thor.types import OriginType

from .connection import HttpClientConnection
from .exchange import HttpClientExchange
from .initiate import initiate_connection


class HttpClient:
    "An asynchronous HTTP client."

    def __init__(self, loop: Optional[LoopBase] = None) -> None:
        self.loop = loop or _loop
        self.idle_timeout: int = 60  # seconds
        self.connect_attempts: int = 3
        self.connect_timeout: Optional[int] = 3  # seconds
        self.read_timeout: Optional[int] = None  # seconds
        self.retry_limit: int = 2
        self.retry_delay: float = 0.5  # seconds
        self.retry_body_limit: int = 1024 * 1024  # bytes
        self.max_server_conn: int = 6
        self.check_ip: Optional[Callable[[str], bool]] = None
        self.careful: bool = True
        self._idle_conns: Dict[OriginType, List[HttpClientConnection]] = defaultdict(
            list
        )
        self.conn_counts: Dict[OriginType, int] = defaultdict(int)
        self._req_q: Dict[
            OriginType,
            List[
                Tuple[
                    Callable[[HttpClientConnection], None],
                    Callable[[str, int, str], None],
                ]
            ],
        ] = defaultdict(list)
        self.loop.once("stop", self._close_conns)

    def exchange(self) -> HttpClientExchange:
        return HttpClientExchange(self)

    def attach_conn(
        self,
        origin: OriginType,
        handle_connect: Callable[[HttpClientConnection], None],
        handle_connect_error: Callable[[str, int, str], None],
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
                    conn.idler.stop()
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

        def unpool() -> None:
            idle = self._idle_conns.get(origin)  # avoid defaultdict's auto-create
            if idle and conn in idle:
                idle.remove(conn)
                if not idle:
                    del self._idle_conns[origin]

        def idle_close() -> None:
            "Remove the connection from the pool when it closes."
            if conn.idler:
                conn.idler.stop()
            self._conn_is_dead(conn)
            unpool()

        def idle_expire() -> None:
            "Close the connection once it has been idle too long."
            unpool()
            # abort() emits no 'close', so nothing else will count this connection
            # as dead, and an unresponsive peer can't hold its slot.
            conn.tcp_conn.abort()
            conn.remove_listeners("close")  # drop the now-stale idle_close
            self._conn_is_dead(conn)

        if self._req_q[origin]:
            handle_connect = self._req_q[origin].pop(0)[0]
            handle_connect(conn)
        elif self.idle_timeout > 0:
            conn.once("close", idle_close)
            conn.idler = Timer(self.loop, idle_expire)
            conn.idler.start(self.idle_timeout)
            self._idle_conns[origin].append(conn)
        else:
            conn.close()
            self._conn_is_dead(conn)

    def dead_conn(self, conn: HttpClientConnection) -> None:
        "Notify the client that a connection is dead."
        conn.detach()
        idle = self._idle_conns.get(conn.origin)
        if idle and conn in idle:
            if conn.idler:
                conn.idler.stop()
                conn.idler = None
            idle.remove(conn)
            if not idle:
                del self._idle_conns[conn.origin]
        if conn.tcp_connected:
            conn.close()
        self._conn_is_dead(conn)

    def _conn_is_dead(self, conn: HttpClientConnection) -> None:
        # Several paths can report the same death.
        if getattr(conn, "_counted_dead", False):
            return
        setattr(conn, "_counted_dead", True)
        origin = conn.origin
        assert origin, "origin not found in _conn_is_dead"
        count = self.conn_counts[origin] - 1
        if count > 0:
            self.conn_counts[origin] = count
        else:
            self.conn_counts.pop(origin, None)
        if self._req_q[origin] and count < self.max_server_conn:
            handle_connect, handle_connect_error = self._req_q[origin].pop(0)
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
