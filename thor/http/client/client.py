from __future__ import annotations

import socket
from typing import Callable, Dict, List, Optional, Set, Tuple

from thor.loop import LoopBase, _loop
from thor.tcp import TcpConnection
from thor.types import OriginType

from .connection import ConnectionState, HttpClientConnection
from .exchange import HttpClientExchange
from .initiate import initiate_connection

ConnectHandler = Callable[[HttpClientConnection], None]
ConnectErrorHandler = Callable[[str, int, str], None]


class HttpClient:
    """
    An asynchronous HTTP client.

    It is also the connection pool: per origin, it knows the live
    connections (to cap them), the idle ones (to reuse them) and the
    requests waiting for one. A connection moves between those through
    attach_conn, release_conn and dead_conn only.
    """

    def __init__(self, loop: Optional[LoopBase] = None) -> None:
        self.loop = loop or _loop
        self.idle_timeout: Optional[int] = 60  # seconds; None or 0 to disable
        self.connect_attempts: int = 3
        self.connect_timeout: Optional[int] = 3  # seconds
        self.read_timeout: Optional[int] = None  # seconds
        self.retry_limit: int = 2
        self.retry_delay: float = 0.5  # seconds
        self.retry_body_limit: int = 1024 * 1024  # bytes
        self.max_server_conn: int = 6
        self.check_ip: Optional[Callable[[str], bool]] = None
        self.careful: bool = True
        self._conns: Dict[OriginType, Set[HttpClientConnection]] = {}
        self._idle_conns: Dict[OriginType, List[HttpClientConnection]] = {}
        self._waiting: Dict[
            OriginType, List[Tuple[ConnectHandler, ConnectErrorHandler]]
        ] = {}
        self.loop.once("stop", self._close_conns)

    def exchange(self) -> HttpClientExchange:
        return HttpClientExchange(self)

    @property
    def conn_counts(self) -> Dict[OriginType, int]:
        "Live connections per origin."
        return {origin: len(conns) for origin, conns in self._conns.items() if conns}

    def attach_conn(
        self,
        origin: OriginType,
        handle_connect: ConnectHandler,
        handle_connect_error: ConnectErrorHandler,
    ) -> None:
        "Find an idle connection for origin, or create a new one."
        idle = self._idle_conns.get(origin, [])
        while idle:
            conn = idle.pop()
            if not idle:
                del self._idle_conns[origin]
            if conn.tcp_connected and conn.state is ConnectionState.IDLE:
                conn.stop_idle()
                handle_connect(conn)
                return
            self._forget(conn)
        if len(self._conns.get(origin, ())) >= self.max_server_conn:
            self._waiting.setdefault(origin, []).append(
                (handle_connect, handle_connect_error)
            )
            return
        initiate_connection(self, origin, handle_connect, handle_connect_error)

    def new_conn(
        self, origin: OriginType, tcp_conn: TcpConnection
    ) -> HttpClientConnection:
        "Wrap a newly connected socket and count it against its origin."
        conn = HttpClientConnection(self, origin, tcp_conn)
        self._conns.setdefault(origin, set()).add(conn)
        return conn

    def release_conn(self, conn: HttpClientConnection) -> None:
        "The exchange is done with the connection: hand it on, pool it, or close it."
        if conn.state is not ConnectionState.ACTIVE:
            return  # already released, or closed
        conn.detach()
        if not conn.tcp_connected:
            self._forget(conn)
            return
        waiting = self._waiting.get(conn.origin)
        if waiting:
            handle_connect = waiting.pop(0)[0]
            if not waiting:
                del self._waiting[conn.origin]
            handle_connect(conn)
        elif self.idle_timeout:
            conn.idle(self.idle_timeout)
            self._idle_conns.setdefault(conn.origin, []).append(conn)
        else:
            conn.close()
            self._forget(conn)

    def dead_conn(self, conn: HttpClientConnection) -> None:
        "Take a connection out of service, whatever it was doing."
        conn.detach()
        idle = self._idle_conns.get(conn.origin)
        if idle and conn in idle:
            idle.remove(conn)
            if not idle:
                del self._idle_conns[conn.origin]
        if conn.tcp_connected:
            conn.close()
        self._forget(conn)

    def _forget(self, conn: HttpClientConnection) -> None:
        """
        Stop counting a connection; a request waiting for its origin can now
        have a new one. Several paths can report the same death.
        """
        conn.stop_idle()
        if conn.state is ConnectionState.CLOSED:
            return
        conn.state = ConnectionState.CLOSED
        origin = conn.origin
        conns = self._conns.get(origin)
        if conns:
            conns.discard(conn)
            if not conns:
                del self._conns[origin]
        waiting = self._waiting.get(origin)
        if waiting and len(self._conns.get(origin, ())) < self.max_server_conn:
            handle_connect, handle_connect_error = waiting.pop(0)
            if not waiting:
                del self._waiting[origin]
            initiate_connection(self, origin, handle_connect, handle_connect_error)

    def _close_conns(self) -> None:
        "Close all idle HTTP connections."
        for conn_list in list(self._idle_conns.values()):
            for conn in list(conn_list):
                try:
                    conn.close()
                except socket.error:
                    pass
        self._idle_conns.clear()
