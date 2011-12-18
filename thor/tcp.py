#!/usr/bin/env python

"""
push-based asynchronous TCP

This is a generic library for building event-based / asynchronous
TCP servers and clients.

It uses a push model; i.e., the network connection pushes data to
you (using a 'data' event), and you push data to the network connection
(using the write method).
"""

__author__ = "Mark Nottingham <mnot@mnot.net>"
__copyright__ = """\
Copyright (c) 2005-2011 Mark Nottingham

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import errno
import sys
import socket

from thor.loop import EventSource


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

    _block_errs = set([
        errno.EAGAIN, errno.EWOULDBLOCK
    ])
    _close_errs = set([
        errno.EBADF, errno.ECONNRESET, errno.ESHUTDOWN,
        errno.ECONNABORTED, errno.ECONNREFUSED,
        errno.ENOTCONN, errno.EPIPE
    ])

    def __init__(self, sock, host, port, loop=None):
        EventSource.__init__(self, loop)
        self.socket = sock
        self.host = host
        self.port = port
        self.tcp_connected = True # we assume a connected socket
        self._input_paused = True # we start with input paused
        self._output_paused = False
        self._closing = False
        self._write_buffer = []

        self.register_fd(sock.fileno())
        self.on('readable', self.handle_read)
        self.on('writable', self.handle_write)
        self.on('close', self.handle_close)

    def __repr__(self):
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

    def handle_read(self):
        "The connection has data read for reading"
        try:
            # TODO: look into recv_into (but see python issue7827)
            data = self.socket.recv(self.read_bufsize)
        except Exception, why:
            if why[0] in self._block_errs:
                return
            elif why[0] in self._close_errs:
                self.emit('close')
                return
            else:
                raise
        if data == "":
            self.emit('close')
        else:
            self.emit('data', data)

    # TODO: try using buffer; see
    # http://itamarst.org/writings/pycon05/fast.html
    def handle_write(self):
        "The connection is ready for writing; write any buffered data."
        if len(self._write_buffer) > 0:
            data = "".join(self._write_buffer)
            try:
                sent = self.socket.send(data)
            except socket.error, why:
                if why[0] in self._block_errs:
                    return
                elif why[0] in self._close_errs:
                    self.emit('close')
                    return
                else:
                    raise
            if sent < len(data):
                self._write_buffer = [data[sent:]]
            else:
                self._write_buffer = []
        if self._output_paused and \
          len(self._write_buffer) < self.write_bufsize:
            self._output_paused = False
            self.emit('pause', False)
        if self._closing:
            self.close()
        if len(self._write_buffer) == 0:
            self.event_del('writable')

    def handle_close(self):
        """
        The connection has been closed by the other side.
        """
        self.tcp_connected = False
        # TODO: make sure removing close doesn't cause problems.
        self.removeListeners('readable', 'writable', 'close')
        self.unregister_fd()
        self.socket.close()

    def write(self, data):
        "Write data to the connection."
        self._write_buffer.append(data)
        if len(self._write_buffer) > self.write_bufsize:
            self._output_paused = True
            self.emit('pause', True)
        self.event_add('writable')

    def pause(self, paused):
        """
        Temporarily stop/start reading from the connection and pushing
        it to the app.
        """
        if paused:
            self.event_del('readable')
        else:
            self.event_add('readable')
        self._input_paused = paused

    def close(self):
        "Flush buffered data (if any) and close the connection."
        self.pause(True)
        if len(self._write_buffer) > 0:
            self._closing = True
        else:
            self.handle_close()

        # TODO: should loop stop automatically close all conns?

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
    def __init__(self, host, port, sock=None, loop=None):
        EventSource.__init__(self, loop)
        self.host = host
        self.port = port
        self.sock = sock or server_listen(host, port)
        self.on('readable', self.handle_accept)
        self.register_fd(self.sock.fileno(), 'readable')

    def handle_accept(self):
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

    def shutdown(self):
        "Stop accepting requests and close the listening socket."
        self.removeListeners('readable')
        self.sock.close()
        # TODO: emit close?


def server_listen(host, port, backlog=None):
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
      - connect_error (err_type, err): if there's a problem before getting
        a connection. err_type is socket.error or socket.gaierror; err
        is the specific error encountered.

    To connect to a server:

    > c = TcpClient()
    > c.on('connect', conn_handler)
    > c.on('connect_error', error_handler)
    > c.connect(host, port)

    conn_handler will be called with the tcp_conn as the argument
    when the connection is made.
    """
    def __init__(self, loop=None):
        EventSource.__init__(self, loop)
        self.host = None
        self.port = None
        self._timeout_ev = None
        self._error_sent = False
        # TODO: IPV6
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setblocking(False)
        self.on('error', self.handle_conn_error)
        self.register_fd(self.sock.fileno(), 'writable')
        self.event_add('error')


    def connect(self, host, port, connect_timeout=None):
        """
        Connect to host:port (with an optional connect timeout)
        and emit 'connect' when connected, or 'connect_error' in
        the case of an error.
        """
        self.host = host
        self.port = port
        self.on('writable', self.handle_connect)
        # TODO: use socket.getaddrinfo(); needs to be non-blocking.
        try:
            err = self.sock.connect_ex((host, port))
        except socket.gaierror, why:
            self.handle_conn_error(socket.gaierror, why[0])
            return
        except socket.error, why:
            self.handle_conn_error(socket.error, why[0])
            return
        if err != errno.EINPROGRESS:
            self.handle_conn_error(socket.error, err)
            return
        if connect_timeout:
            self._timeout_ev = self._loop.schedule(
                connect_timeout,
                self.handle_conn_error,
                socket.error, errno.ETIMEDOUT, True
            )

    def handle_connect(self):
        self.unregister_fd()
        if self._timeout_ev:
            self._timeout_ev.delete()
        if self._error_sent:
            return
        err = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err:
            self.handle_conn_error(socket.error, err)
        else:
            tcp_conn = TcpConnection(
                self.sock, self.host, self.port, self._loop
            )
            self.emit('connect', tcp_conn)

    def handle_conn_error(self, err_type=None, err=None, close=False):
        if self._timeout_ev:
            self._timeout_ev.delete()
        if self._error_sent:
            return
        if err_type is None or err is None:
            err_type = socket.error
            err = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        self._error_sent = True
        self.unregister_fd()
        self.emit('connect_error', err_type, err)
        if close:
            self.sock.close()


if __name__ == "__main__":
    # quick demo server
    from thor.loop import run, stop
    server = TcpServer('localhost', int(sys.argv[-1]))
    def handle_conn(conn):
        conn.pause(False)
        def echo(chunk):
            if chunk.strip().lower() in ['quit', 'stop']:
                stop()
            else:
                conn.write("-> %s" % chunk)
        conn.on('data', echo)
    server.on('connect', handle_conn)
    run()


