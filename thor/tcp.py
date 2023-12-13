#!/usr/bin/env python

"""
push-based asynchronous TCP

This is a generic library for building event-based / asynchronous
TCP servers and clients.

It uses a push model; i.e., the network connection pushes data to
you (using a 'data' event), and you push data to the network connection
(using the write method).
"""

import errno
import os
import sys
import socket
from typing import Optional, List, Callable

from thor.dns import DnsResult, Address
from thor.loop import EventSource, LoopBase, schedule
from thor.loop import ScheduledEvent


class TcpConnection(EventSource):
    """
    An asynchronous TCP connection.

    Emits:
     - data (chunk): incoming data
     - close (): the other party has closed the connection
     - pause (bool): whether the connection has been paused

    It will emit the 'data' even every time incoming data is
    available;

    > def process(data):
    >   print "got some data:", data
    > tcp_conn.on('data', process)

    When you want to write to the connection, just write to it:

    > tcp_conn.write(data)

    If you want to close the connection from your side, just call close:

    > tcp_conn.close()

    Note that this will flush any data already written.

    If the other side closes the connection, The 'close' event will be
    emitted;

    > def handle_close():
    >   print "oops, they don't like us any more..."
    > tcp_conn.on('close', handle_close)

    If you write too much data to the connection and the buffers fill up,
    pause_cb will be emitted with True to tell you to stop sending data
    temporarily;

    > def handle_pause(paused):
    >   if paused:
    >       # stop sending data
    >   else:
    >       # it's OK to start again
    > tcp_conn.on('pause', handle_pause)

    Note that this is advisory; if you ignore it, the data will still be
    buffered, but the buffer will grow.

    Likewise, if you want to pause the connection because your buffers
    are full, call pause;

    > tcp_conn.pause(True)

    but don't forget to tell it when it's OK to send data again;

    > tcp_conn.pause(False)

    NOTE that connections are paused to start with; if you want to start
    getting data from them, you'll need to pause(False).
    """

    write_bufsize = 16
    read_bufsize = 1024 * 16

    block_errs = set([errno.EAGAIN, errno.EWOULDBLOCK, errno.ETIMEDOUT])
    close_errs = set(
        [
            errno.EBADF,
            errno.ECONNRESET,
            errno.ESHUTDOWN,
            errno.ECONNABORTED,
            errno.ECONNREFUSED,
            errno.ENOTCONN,
            errno.EPIPE,
        ]
    )

    def __init__(
        self, sock: socket.socket, address: Address, loop: Optional[LoopBase] = None
    ) -> None:
        EventSource.__init__(self, loop)
        self.socket = sock
        self.address = address
        self.tcp_connected = True  # we assume a connected socket
        self._input_paused = True  # we start with input paused
        self._output_paused = False
        self._closing = False
        self._write_buffer: List[bytes] = []

        self.register_fd(sock.fileno())
        self.on("fd_readable", self.handle_readable)
        self.on("fd_writable", self.handle_writable)
        self.once("fd_close", self._handle_close)

    def __repr__(self) -> str:
        status = [self.__class__.__module__ + "." + self.__class__.__name__]
        status.append(self.tcp_connected and "connected" or "disconnected")
        status.append(f"{self.address[0]}:{self.address[1]}")
        if self._input_paused:
            status.append("input paused")
        if self._output_paused:
            status.append("output paused")
        if self._closing:
            status.append("closing")
        if self._write_buffer:
            status.append(f"{len(self._write_buffer)} write buffered")
        return f"<{', '.join(status)} at {id(self):#x}>"

    def handle_readable(self) -> None:
        "The connection has data read for reading"
        try:
            data = self.socket.recv(self.read_bufsize)
        except (socket.error, OSError) as why:
            if why.args[0] in self.block_errs:
                return
            if why.args[0] in self.close_errs:
                self._handle_close()
                return
            raise
        if data == b"":
            self._handle_close()
        else:
            self.emit("data", data)

    def handle_writable(self) -> None:
        "The connection is ready for writing; write any buffered data."
        if self._write_buffer:
            data = b"".join(self._write_buffer)
            try:
                sent = self.socket.send(data)
            except (socket.error, OSError) as why:
                if why.args[0] in self.block_errs:
                    return
                if why.args[0] in self.close_errs:
                    self._handle_close()
                    return
                raise
            if sent < len(data):
                self._write_buffer = [data[sent:]]
            else:
                self._write_buffer = []
        if self._output_paused and len(self._write_buffer) < self.write_bufsize:
            self._output_paused = False
            self.emit("pause", False)
        if self._closing:
            self._close()
        if not self._write_buffer:
            self.event_del("fd_writable")

    def write(self, data: bytes) -> None:
        "Write data to the connection."
        self._write_buffer.append(data)
        if len(self._write_buffer) > self.write_bufsize:
            self._output_paused = True
            self.emit("pause", True)
        self.event_add("fd_writable")

    def pause(self, paused: bool) -> None:
        """
        Temporarily stop/start reading from the connection and pushing
        it to the app.
        """
        if paused:
            self.event_del("fd_readable")
        else:
            self.event_add("fd_readable")
        self._input_paused = paused

    def close(self) -> None:
        "Flush buffered data (if any) and close the connection."
        self.pause(True)
        if self._write_buffer:
            self._closing = True
        else:
            self._close()

    def _handle_close(self) -> None:
        "The connection has been closed by the other side."
        self._close()
        self.emit("close")

    def _close(self) -> None:
        self.tcp_connected = False
        self.remove_listeners("fd_readable", "fd_writable", "fd_close")
        self.unregister_fd()
        if self.socket:
            self.socket.close()


class TcpServer(EventSource):
    """
    An asynchronous TCP server.

    Emits:
      - connect (tcp_conn): upon connection

    To start listening:

    > s = TcpServer(host, port)
    > s.on('connect', conn_handler)

    conn_handler is called every time a new client connects.
    """

    def __init__(
        self,
        host: bytes,
        port: int,
        sock: Optional[socket.socket] = None,
        loop: Optional[LoopBase] = None,
    ) -> None:
        EventSource.__init__(self, loop)
        self.host = host
        self.port = port
        self.sock = sock or server_listen(host, port)
        self.on("fd_readable", self.handle_accept)
        self.register_fd(self.sock.fileno(), "fd_readable")
        schedule(0, self.emit, "start")

    def handle_accept(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except (TypeError, IndexError):
            # sometimes accept() returns None if we have
            # multiple processes listening
            return
        conn.setblocking(False)
        tcp_conn = TcpConnection(
            conn, (self.host.decode("idna"), self.port), self._loop
        )
        self.emit("connect", tcp_conn)

    def shutdown(self) -> None:
        "Stop accepting requests and close the listening socket."
        self.remove_listeners("fd_readable")
        if self.sock:
            self.sock.close()
        self.emit("stop")


def server_listen(
    host: bytes, port: int, backlog: Optional[int] = None
) -> socket.socket:
    "Return a socket listening to host:port."
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(backlog or socket.SOMAXCONN)
    return sock


class TcpClient(EventSource):
    """
    An asynchronous TCP client.

    Emits:
      - connect (tcp_conn): upon connection
      - connect_error (err_type, err_id, err_str): if there's a problem
        before getting a connection. err_type is 'socket' or
        'gai'; err_id is the specific error encountered, and
        err_str is its textual description.

    To connect to a server:

    > c = TcpClient()
    > c.on('connect', conn_handler)
    > c.on('connect_error', error_handler)
    > c.connect(address)

    conn_handler will be called with the tcp_conn as the argument
    when the connection is made.
    """

    def __init__(self, loop: Optional[LoopBase] = None) -> None:
        EventSource.__init__(self, loop)
        self.hostname: Optional[bytes] = None
        self.address: Optional[Address] = None
        self.sock: Optional[socket.socket] = None
        self.check_ip: Optional[Callable[[str], bool]] = None
        self._timeout_ev: Optional[ScheduledEvent] = None
        self._error_sent = False

    def connect(
        self, host: bytes, port: int, connect_timeout: Optional[float] = None
    ) -> None:
        """
        Connect to an IPv4 host/port. Does not work with IPv6; see connect_dns().
        """
        dns_result = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            (host.decode("idna"), port),
        )
        self.connect_dns(host, dns_result, connect_timeout)

    def connect_dns(
        self,
        hostname: bytes,
        dns_result: DnsResult,
        connect_timeout: Optional[float] = None,
    ) -> None:
        """
        Connect to DnsResult (with an optional connect timeout)
        and emit 'connect' when connected, or 'connect_error' in
        the case of an error.
        """
        self.hostname = hostname
        family = dns_result[0]
        self.address = dns_result[4]
        if connect_timeout:
            self._timeout_ev = self._loop.schedule(
                connect_timeout,
                self.handle_socket_error,
                socket.error(errno.ETIMEDOUT, os.strerror(errno.ETIMEDOUT)),
            )

        if callable(self.check_ip):
            if not self.check_ip(self.address[0]):
                self.handle_conn_error("access", 0, "IP Check failed")
                return

        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.setblocking(False)
        self.once("fd_error", self.handle_fd_error)
        self.register_fd(self.sock.fileno(), "fd_writable")
        self.event_add("fd_error")
        self.once("fd_writable", self.handle_connect)
        try:
            err = self.sock.connect_ex(self.address)
        except socket.error as why:
            self.handle_socket_error(why)
            return
        if err != errno.EINPROGRESS:
            self.handle_socket_error(socket.error(err, os.strerror(err)))
            return

    def handle_connect(self) -> None:
        self.unregister_fd()
        if self._timeout_ev:
            self._timeout_ev.delete()
        if self._error_sent:
            return
        assert self.sock, "Socket not found in handle_connect"
        err = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err:
            self.handle_socket_error(socket.error(err, os.strerror(err)))
        else:
            assert self.address, "address not found in handle_connect"
            tcp_conn = TcpConnection(self.sock, self.address, self._loop)
            self.emit("connect", tcp_conn)

    def handle_fd_error(self) -> None:
        assert self.sock, "Socket not found in handle_fd_error"
        try:
            err_id = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        except OSError:
            err_id = 54
        err_str = os.strerror(err_id)
        self.handle_conn_error("socket", err_id, err_str)

    def handle_socket_error(self, why: Exception, err_type: str = "socket") -> None:
        err_id = why.args[0]
        err_str = why.args[1]
        self.handle_conn_error(err_type, err_id, err_str)

    def handle_conn_error(
        self, err_type: str, err_id: int, err_str: str, close: bool = True
    ) -> None:
        """
        Handle a connect error.
        """
        if self._timeout_ev:
            self._timeout_ev.delete()
        if self._error_sent:
            return
        self._error_sent = True
        self.unregister_fd()
        self.emit("connect_error", err_type, err_id, err_str)
        if close and self.sock:
            self.sock.close()


if __name__ == "__main__":
    # quick demo server
    from thor.loop import run, stop

    server = TcpServer(b"localhost", int(sys.argv[-1]))

    def handle_conn(conn: TcpConnection) -> None:
        conn.pause(False)

        def echo(chunk: bytes) -> None:
            if chunk.strip().lower() in ["quit", "stop"]:
                stop()
            else:
                conn.write(b"-> %s" % chunk)

        conn.on("data", echo)

    server.on("connect", handle_conn)
    run()
