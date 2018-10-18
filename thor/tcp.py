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
from typing import Tuple, List, Union, Type # pylint: disable=unused-import
import ssl as sys_ssl # pylint: disable=unused-import

from thor.loop import EventSource, LoopBase, schedule
from thor.loop import ScheduledEvent # pylint: disable=unused-import


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

    # TODO: play with various buffer sizes
    write_bufsize = 16
    read_bufsize = 1024 * 16

    block_errs = set([errno.EAGAIN, errno.EWOULDBLOCK, errno.ETIMEDOUT])
    close_errs = set([
        errno.EBADF, errno.ECONNRESET, errno.ESHUTDOWN,
        errno.ECONNABORTED, errno.ECONNREFUSED,
        errno.ENOTCONN, errno.EPIPE])

    def __init__(self, sock: socket.socket, host: bytes, port: int, loop: LoopBase = None) -> None:
        EventSource.__init__(self, loop)
        self.socket = sock
        self.host = host
        self.port = port
        self.tcp_connected = True # we assume a connected socket
        self._input_paused = True # we start with input paused
        self._output_paused = False
        self._closing = False
        self._write_buffer = []   # type: List[bytes]

        self.register_fd(sock.fileno())
        self.on('fd_readable', self.handle_readable)
        self.on('fd_writable', self.handle_writable)
        self.on('fd_close', self._handle_close)

    def __repr__(self) -> str:
        status = [self.__class__.__module__ + "." + self.__class__.__name__]
        status.append(self.tcp_connected and 'connected' or 'disconnected')
        status.append('%s:%s' % (self.host, self.port))
        if self._input_paused:
            status.append('input paused')
        if self._output_paused:
            status.append('output paused')
        if self._closing:
            status.append('closing')
        if self._write_buffer:
            status.append('%s write buffered' % len(self._write_buffer))
        return "<%s at %#x>" % (", ".join(status), id(self))

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
            self.emit('data', data)

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
            self.emit('pause', False)
        if self._closing:
            self._close()
        if not self._write_buffer:
            self.event_del('fd_writable')

    def write(self, data: bytes) -> None:
        "Write data to the connection."
        self._write_buffer.append(data)
        if len(self._write_buffer) > self.write_bufsize:
            self._output_paused = True
            self.emit('pause', True)
        self.event_add('fd_writable')

    def pause(self, paused: bool) -> None:
        """
        Temporarily stop/start reading from the connection and pushing
        it to the app.
        """
        if paused:
            self.event_del('fd_readable')
        else:
            self.event_add('fd_readable')
        self._input_paused = paused

    def close(self) -> None:
        "Flush buffered data (if any) and close the connection."
        self.pause(True)
        if self._write_buffer:
            self._closing = True
        else:
            self._close()
        # TODO: should loop stop automatically close all conns?

    def _handle_close(self) -> None:
        "The connection has been closed by the other side."
        self._close()
        self.emit('close')

    def _close(self) -> None:
        self.tcp_connected = False
        self.removeListeners('fd_readable', 'fd_writable', 'fd_close')
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
    def __init__(self, host: bytes, port: int, sock: socket.socket = None,
                 loop: LoopBase = None) -> None:
        EventSource.__init__(self, loop)
        self.host = host
        self.port = port
        self.sock = sock or server_listen(host, port)
        self.on('fd_readable', self.handle_accept)
        self.register_fd(self.sock.fileno(), 'fd_readable')
        schedule(0, self.emit, 'start')

    def handle_accept(self) -> None:
        try:
            conn, addr = self.sock.accept()
        except (TypeError, IndexError):
            # sometimes accept() returns None if we have
            # multiple processes listening
            return
        conn.setblocking(False)
        tcp_conn = TcpConnection(conn, self.host, self.port, self._loop)
        self.emit('connect', tcp_conn)

    # TODO: should loop stop close listening sockets?

    def shutdown(self) -> None:
        "Stop accepting requests and close the listening socket."
        self.removeListeners('fd_readable')
        self.sock.close()
        self.emit('stop')
        # TODO: emit close?


def server_listen(host: bytes, port: int, backlog: int = None) -> socket.socket:
    "Return a socket listening to host:port."
    # TODO: IPV6
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
    > c.connect(host, port)

    conn_handler will be called with the tcp_conn as the argument
    when the connection is made.
    """
    def __init__(self, loop: LoopBase = None) -> None:
        EventSource.__init__(self, loop)
        self.host = None  # type: bytes
        self.port = None  # type: int
        self._timeout_ev = None  # type: ScheduledEvent
        self._error_sent = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setblocking(False)
        self.on('fd_error', self.handle_fd_error)
        self.register_fd(self.sock.fileno(), 'fd_writable')
        self.event_add('fd_error')

    def connect(self, host: bytes, port: int, connect_timeout: float = None) -> None:
        """
        Connect to host:port (with an optional connect timeout)
        and emit 'connect' when connected, or 'connect_error' in
        the case of an error.
        """
        self.host = host
        self.port = port
        self.on('fd_writable', self.handle_connect)
        # TODO: use socket.getaddrinfo(); needs to be non-blocking.
        try:
            err = self.sock.connect_ex((host, port))
        except socket.gaierror as why:
            self.handle_socket_error(why, 'gai')
            return
        except socket.error as why:
            self.handle_socket_error(why)
            return
        if err != errno.EINPROGRESS:
            self.handle_socket_error(socket.error(err, os.strerror(err)))
            return
        if connect_timeout:
            self._timeout_ev = self._loop.schedule(
                connect_timeout,
                self.handle_socket_error,
                socket.error(errno.ETIMEDOUT, os.strerror(errno.ETIMEDOUT))
            )

    def handle_connect(self) -> None:
        self.unregister_fd()
        if self._timeout_ev:
            self._timeout_ev.delete()
        if self._error_sent:
            return
        err = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err:
            self.handle_socket_error(socket.error(err, os.strerror(err)))
        else:
            tcp_conn = TcpConnection(self.sock, self.host, self.port, self._loop)
            self.emit('connect', tcp_conn)

    def handle_fd_error(self) -> None:
        err_id = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        err_str = os.strerror(err_id)
        self.handle_conn_error('socket', err_id, err_str)

    def handle_socket_error(self, why: Union[socket.error, socket.gaierror, sys_ssl.SSLError],
                            err_type: str = "socket") -> None:
        err_id = why.args[0]
        err_str = why.args[1]
        self.handle_conn_error(err_type, err_id, err_str)

    def handle_conn_error(self, err_type: str, err_id: int, err_str: str,
                          close: bool = True) -> None:
        """
        Handle a connect error.
        """
        if self._timeout_ev:
            self._timeout_ev.delete()
        if self._error_sent:
            return
        self._error_sent = True
        self.unregister_fd()
        self.emit('connect_error', err_type, err_id, err_str)
        if close:
            self.sock.close()


if __name__ == "__main__":
    # quick demo server
    from thor.loop import run, stop
    server = TcpServer(b'localhost', int(sys.argv[-1]))
    def handle_conn(conn: TcpConnection) -> None:
        conn.pause(False)
        def echo(chunk: bytes) -> None:
            if chunk.strip().lower() in ['quit', 'stop']:
                stop()
            else:
                conn.write(b"-> %s" % chunk)
        conn.on('data', echo)
    server.on('connect', handle_conn)
    run()
