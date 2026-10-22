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
import socket
import ssl
import sys
from collections import deque
from enum import Enum
from itertools import islice
from typing import Callable, Deque, Iterator, Optional, Set, Union

from thor.loop import EventSource, LoopBase, ScheduledEvent
from thor.types import Address, DnsResult


class WriteBuffer:
    """
    Bytes waiting to be sent, in the chunks they were written as. Sending
    takes from the front without copying what's left behind: a partly sent
    chunk is remembered by its offset, so a slow reader costs one send per
    writable event rather than a copy of everything still queued.
    """

    __slots__ = ("_chunks", "_offset", "size")

    def __init__(self) -> None:
        self._chunks: Deque[bytes] = deque()
        self._offset = 0  # into the first chunk
        self.size = 0  # bytes left to send

    def __len__(self) -> int:
        "Chunks left to send."
        return len(self._chunks)

    def __iter__(self) -> Iterator[bytes]:
        "What's left to send, chunk by chunk."
        for i, chunk in enumerate(self._chunks):
            yield chunk[self._offset :] if i == 0 else chunk

    def add(self, data: bytes) -> None:
        self._chunks.append(data)
        self.size += len(data)

    def clear(self) -> None:
        self._chunks.clear()
        self._offset = 0
        self.size = 0

    def peek(self, limit: int) -> Union[bytes, memoryview]:
        """
        The next bytes to send, as one piece. Small chunks are joined up to
        limit bytes so they go in one send; a large chunk isn't copied.
        """
        head = memoryview(self._chunks[0])[self._offset :]
        if len(self._chunks) == 1 or len(head) >= limit:
            return head
        pieces = [head]
        total = len(head)
        for chunk in islice(self._chunks, 1, None):
            if total + len(chunk) > limit:
                break
            pieces.append(memoryview(chunk))
            total += len(chunk)
        return b"".join(pieces)

    def consume(self, sent: int) -> None:
        "The first sent bytes of what peek returned have gone."
        self.size -= sent
        while sent:
            left = len(self._chunks[0]) - self._offset
            if sent >= left:
                self._chunks.popleft()
                self._offset = 0
                sent -= left
            else:
                self._offset += sent
                sent = 0


class IoFailure(Enum):
    BLOCK = "block"  # try again when the socket is ready
    LOST = "lost"  # the connection is gone


class TcpState(Enum):
    OPEN = "open"
    FLUSHING = "flushing"  # close() called; buffered writes still going out
    OUTPUT_CLOSED = "output closed"  # our FIN is sent; waiting for the peer's
    CLOSED = "closed"


class TcpConnection(EventSource):
    """
    An asynchronous TCP connection.

    Emits:
     - data (chunk): incoming data
     - close (): the other party has closed its side of the connection
     - disconnect (): the socket is closed
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

    If you want to close the connection immediately, discarding any buffered
    data, call abort:

    > tcp_conn.abort()

    If you try to write to a closed connection, an OSError will be raised.

    If the other side closes its end of the connection, the 'close' event
    will be emitted;

    > def handle_close():
    >   print "oops, they don't like us any more..."
    > tcp_conn.on('close', handle_close)

    Nothing more will arrive, but the peer may still be reading: you can
    keep writing, and the socket is only closed once you call close() and
    the buffered data has gone (or you call abort()). 'disconnect' is
    emitted when the socket is closed, whoever started it.

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

    write_bufsize = 16  # number of chunks
    max_write_buffer_chunks = 4096
    max_write_buffer_size = 1024 * 1024 * 16  # bytes
    write_coalesce = 1024 * 64  # bytes: small chunks joined into one send
    read_bufsize = 1024 * 16  # bytes
    close_timeout = 5  # seconds

    block_errs = {errno.EAGAIN, errno.EWOULDBLOCK, errno.ETIMEDOUT}
    close_errs = {
        errno.EBADF,
        errno.ECONNRESET,
        errno.ESHUTDOWN,
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.ENOTCONN,
        errno.EPIPE,
    }

    def __init__(
        self, sock: socket.socket, address: Address, loop: Optional[LoopBase] = None
    ) -> None:
        EventSource.__init__(self, loop)
        self.socket = sock
        self.address = address
        self._state = TcpState.OPEN  # we assume a connected socket
        self._peer_closed = False  # the peer has sent its FIN
        self._input_paused = True  # we start with input paused
        self._output_paused = False
        self._close_timeout_ev: Optional[ScheduledEvent] = None
        self._write_buffer = WriteBuffer()
        # What send() was last offered and didn't take: TLS wants the same
        # bytes again after a want-write, so they aren't re-peeked.
        self._pending_send: Optional[Union[bytes, memoryview]] = None

        self.register_fd(sock.fileno())
        self.on("fd_readable", self.handle_readable)
        self.on("fd_writable", self.handle_writable)
        self.once("fd_close", self._handle_close)
        self.event_add("fd_close")

    def __repr__(self) -> str:
        status = [self.__class__.__module__ + "." + self.__class__.__name__]
        status.append(self._state.value)
        status.append(f"{self.address[0]}:{self.address[1]}")
        if self._peer_closed:
            status.append("peer closed")
        if self._input_paused:
            status.append("input paused")
        if self._output_paused:
            status.append("output paused")
        if self._write_buffer:
            status.append(f"{len(self._write_buffer)} write buffered")
        return f"<{', '.join(status)} at {id(self):#x}>"

    @property
    def tcp_connected(self) -> bool:
        "Is the socket open?"
        return self._state is not TcpState.CLOSED

    def handle_readable(self) -> None:
        "The connection has data read for reading"
        try:
            data = self.socket.recv(self.read_bufsize)
        except OSError as why:
            failure = self._io_failure(why)
            if failure is IoFailure.BLOCK:
                return
            if failure is IoFailure.LOST:
                self._lost()
                return
            raise
        if data == b"":
            self._handle_peer_eof()
        elif self._state is TcpState.OUTPUT_CLOSED:
            return  # we've said we're done; nothing to give it to
        else:
            self.emit("data", data)

    def _drain_readable(self) -> bool:
        """
        Read pending bytes after a hangup notification.

        Some pollers can report a close before the final readable event is
        delivered. Drain any queued bytes so protocol layers can complete.
        Returns True when peer EOF was observed.
        """
        while True:
            try:
                data = self.socket.recv(self.read_bufsize)
            except OSError as why:
                failure = self._io_failure(why)
                if failure is IoFailure.BLOCK:
                    return False
                if failure is IoFailure.LOST:
                    return True
                raise
            if data == b"":
                return True
            if self._state is not TcpState.OUTPUT_CLOSED:
                self.emit("data", data)

    def handle_writable(self) -> None:
        "The connection is ready for writing; write any buffered data."
        if self._write_buffer:
            if self._pending_send is None:
                self._pending_send = self._write_buffer.peek(self.write_coalesce)
            try:
                sent = self.socket.send(self._pending_send)
            except OSError as why:
                failure = self._io_failure(why)
                if failure is IoFailure.BLOCK:
                    return
                if failure is IoFailure.LOST:
                    self._lost()
                    return
                raise
            self._pending_send = None
            self._write_buffer.consume(sent)
            if sent:
                self.emit("sent")
        if (
            self._output_paused
            and len(self._write_buffer) < self.write_bufsize
            and self._write_buffer.size < self.max_write_buffer_size
        ):
            self._output_paused = False
            self.emit("pause", False)
        if self._state is TcpState.FLUSHING and not self._write_buffer:
            self._shutdown_output()
            return
        if not self._write_buffer:
            self.event_del("fd_writable")

    def write(self, data: bytes) -> None:
        "Write data to the connection."
        if self._state is not TcpState.OPEN:
            raise OSError("Connection closed")
        if not data:
            return  # nothing to send, and a lone empty chunk would never drain
        if len(self._write_buffer) >= self.max_write_buffer_chunks:
            if not self._output_paused:
                self._output_paused = True
                self.emit("pause", True)
            raise BufferError("TCP write buffer chunk limit exceeded")
        if self._write_buffer.size + len(data) > self.max_write_buffer_size:
            if not self._output_paused:
                self._output_paused = True
                self.emit("pause", True)
            raise BufferError("TCP write buffer limit exceeded")
        self._write_buffer.add(data)
        if len(self._write_buffer) > self.write_bufsize:
            self._output_paused = True
            self.emit("pause", True)
        self.event_add("fd_writable")

    def pause(self, paused: bool) -> None:
        """
        Temporarily stop/start reading from the connection and pushing
        it to the app.
        """
        if paused or self._peer_closed or self._state is TcpState.CLOSED:
            self.event_del("fd_readable")  # at EOF there's nothing to wait for
        else:
            self.event_add("fd_readable")
        self._input_paused = paused

    def close(self) -> None:
        "Flush buffered data (if any) and close the connection."
        if self._state is not TcpState.OPEN:
            return
        if self._write_buffer:
            self._state = TcpState.FLUSHING
            self.event_add("fd_writable")
        else:
            self._shutdown_output()

    def abort(self) -> None:
        "Close the connection immediately, discarding buffered data."
        self._close()

    def _shutdown_output(self) -> None:
        "Our side is done sending."
        if self._peer_closed:  # and so is theirs: nothing left to wait for
            self._close()
            return
        try:
            self.socket.shutdown(socket.SHUT_WR)
        except (socket.error, OSError) as why:
            if why.args[0] not in self.close_errs:
                raise
        self._state = TcpState.OUTPUT_CLOSED
        self.event_del("fd_writable")
        self.event_add("fd_readable")  # to see the peer's FIN
        self._input_paused = False
        if self.close_timeout > 0 and not self._close_timeout_ev:
            self._close_timeout_ev = self.loop.schedule(self.close_timeout, self._close)

    def _handle_peer_eof(self) -> None:
        """
        The peer has sent its FIN. Nothing more will arrive, but it may still
        be reading, so writes go on until close(); the socket is closed once
        both sides are done.
        """
        if self._peer_closed or self._state is TcpState.CLOSED:
            return
        self._peer_closed = True
        self.event_del("fd_readable")
        if self._state is TcpState.OUTPUT_CLOSED:
            self._close()
        self.emit("close")

    def _io_failure(self, why: OSError) -> Optional[IoFailure]:
        """
        What a failed recv or send means: wait for the socket, or give the
        connection up. None for anything else, which the caller raises.
        TLS sockets ask to wait with their own exceptions, and any other TLS
        error (EOF without close_notify, a protocol error) ends the connection.
        """
        if isinstance(why, (ssl.SSLWantReadError, ssl.SSLWantWriteError)):
            return IoFailure.BLOCK
        if isinstance(why, ssl.SSLError):
            return IoFailure.LOST
        if why.args[0] in self.block_errs:
            return IoFailure.BLOCK
        if why.args[0] in self.close_errs:
            return IoFailure.LOST
        return None

    def _lost(self) -> None:
        "The connection has failed: nothing can be sent or received."
        if self._close() and not self._peer_closed:
            self.emit("close")  # unless the peer's FIN already said so

    def _handle_close(self) -> None:
        "The poller reports a hangup; read what's left to find the peer's FIN."
        if self._state is TcpState.CLOSED or self._peer_closed:
            return
        eof = self._drain_readable()
        if not self.tcp_connected:
            return  # a data handler closed it
        if eof:
            self._handle_peer_eof()
            return
        self.event_add("fd_readable")
        if self.close_timeout > 0 and not self._close_timeout_ev:
            self._close_timeout_ev = self.loop.schedule(
                self.close_timeout, self._close_after_drain_timeout
            )

    def _close_after_drain_timeout(self) -> None:
        "The FIN still hasn't shown up after a hangup; stop waiting for it."
        self._close_timeout_ev = None
        eof = self._drain_readable()
        if self._state is TcpState.CLOSED:
            return
        if eof:
            self._handle_peer_eof()
        else:
            self._lost()

    def _close(self) -> bool:
        if self._state is TcpState.CLOSED:
            return False
        self._state = TcpState.CLOSED
        self._write_buffer.clear()
        self._pending_send = None
        if self._close_timeout_ev:
            self._close_timeout_ev.delete()
            self._close_timeout_ev = None
        self.remove_listeners("fd_readable", "fd_writable", "fd_close")
        self.unregister_fd()
        if self.socket:
            self.socket.close()
        self.emit("disconnect")
        return True


def _errnos(*names: str) -> Set[int]:
    "Errno values for those names that this platform defines."
    return {getattr(errno, name) for name in names if hasattr(errno, name)}


class TcpServer(EventSource):
    """
    An asynchronous TCP server.

    Emits:
      - connect (tcp_conn): upon connection
      - accept_paused (errno): accepting has paused for a backoff period
      - accept_resumed (): the backoff period has ended

    To start listening:

    > s = TcpServer(host, port)
    > s.on('connect', conn_handler)

    conn_handler is called every time a new client connects.
    """

    # Failures of the pending connection itself (see accept(2)).
    accept_errs = _errnos(
        "EAGAIN",
        "EWOULDBLOCK",
        "EINTR",
        "ECONNABORTED",
        "EPROTO",
        "ENETDOWN",
        "EHOSTDOWN",
        "ENONET",
        "EHOSTUNREACH",
        "ENETUNREACH",
        "ETIMEDOUT",
    )
    # The pending connection may stay queued (resource exhaustion, or a policy
    # layer refusing before it is dequeued), so the listener stays readable.
    # Stop watching it for accept_backoff seconds, then retry.
    accept_backoff_errs = _errnos(
        "EMFILE", "ENFILE", "ENOBUFS", "ENOMEM", "EPERM", "EOPNOTSUPP", "ENOPROTOOPT"
    )
    accept_backoff = 1.0

    def __init__(
        self,
        host: bytes,
        port: int,
        sock: Optional[socket.socket] = None,
        loop: Optional[LoopBase] = None,
        backlog: Optional[int] = None,
    ) -> None:
        EventSource.__init__(self, loop)
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = sock or server_listen(host, port, backlog)
        self.on("fd_readable", self.handle_accept)
        # self.sock is guaranteed to be a socket here because of server_listen,
        # but can become None later.
        if self.sock:
            self.register_fd(self.sock.fileno(), "fd_readable")
        self.loop.schedule(0, self.emit, "start")
        self.active_connections: Set[TcpConnection] = set()
        self._shutting_down_gracefully = False
        self._accept_timer: Optional[ScheduledEvent] = None

    def handle_accept(self) -> None:
        if not self.sock:
            return
        try:
            conn, _ = self.sock.accept()
        except (TypeError, IndexError):
            # sometimes accept() returns None if we have
            # multiple processes listening
            return
        except OSError as why:
            if why.errno in self.accept_errs:
                return
            if why.errno in self.accept_backoff_errs:
                self._pause_accept(why.errno)
                return
            raise
        conn.setblocking(False)
        try:
            tcp_conn = TcpConnection(
                conn, (self.host.decode("idna"), self.port), self.loop
            )
        except FileNotFoundError:
            return  # Connection closed in the meantime
        self.active_connections.add(tcp_conn)
        tcp_conn.on("disconnect", lambda: self._conn_closed(tcp_conn))
        self.emit("connect", tcp_conn)

    def _pause_accept(self, err: int) -> None:
        self.event_del("fd_readable")
        if not self._accept_timer:
            self._accept_timer = self.loop.schedule(
                self.accept_backoff, self._resume_accept
            )
            self.emit("accept_paused", err)

    def _resume_accept(self) -> None:
        self._accept_timer = None
        if self.sock:
            self.event_add("fd_readable")
            self.emit("accept_resumed")

    def _stop_listening(self) -> None:
        self.remove_listeners("fd_readable")
        if self._accept_timer:
            self._accept_timer.delete()
            self._accept_timer = None
        if self.sock:
            self.unregister_fd()
            self.sock.close()
            self.sock = None

    def _conn_closed(self, conn: TcpConnection) -> None:
        self.active_connections.discard(conn)
        if self._shutting_down_gracefully and not self.active_connections:
            self.emit("stop")

    def graceful_shutdown(self) -> None:
        """
        Stop accepting requests and wait for active connections to close.

        Emits "stop" when all connections are closed.
        """
        self._stop_listening()
        self._shutting_down_gracefully = True
        if not self.active_connections:
            self.emit("stop")

    def shutdown(self) -> None:
        "Stop accepting requests and close the listening socket."
        self._stop_listening()
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
            self._timeout_ev = self.loop.schedule(
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
        self.register_fd(self.sock.fileno())
        self.once("fd_error", self.handle_fd_error)
        self.event_add("fd_error")
        self.once("fd_writable", self.handle_connect)
        self.event_add("fd_writable")
        try:
            err = self.sock.connect_ex(self.address)
        except socket.error as why:
            self.handle_socket_error(why)
            return
        if err == 0:
            self.handle_connect()
            return
        if err != errno.EINPROGRESS:
            self.handle_socket_error(socket.error(err, os.strerror(err)))
            return

    def handle_connect(self) -> None:
        "The socket is writable: the connect has finished, one way or the other."
        if self._error_sent:
            return
        assert self.sock, "Socket not found in handle_connect"
        err = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err:
            self.handle_socket_error(socket.error(err, os.strerror(err)))
        else:
            self._connected()

    def _connected(self) -> None:
        """
        The TCP connection is up. Hand it over; a subclass with more to do
        first (a TLS handshake, say) does that here and calls _hand_over when
        it is ready.
        """
        self._hand_over(self.sock)

    def _hand_over(self, sock: Optional[socket.socket]) -> None:
        "Stop watching the socket ourselves and emit it as a connection."
        self.unregister_fd()
        if self._timeout_ev:
            self._timeout_ev.delete()
        if self._error_sent:
            return
        assert sock, "Socket not found in _hand_over"
        assert self.address, "address not found in _hand_over"
        self.emit("connect", TcpConnection(sock, self.address, self.loop))

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
            if chunk.strip().lower() in [b"quit", b"stop"]:
                stop()
            else:
                conn.write(b"-> %s" % chunk)

        conn.on("data", echo)

    server.on("connect", handle_conn)
    run()
