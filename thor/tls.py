#!/usr/bin/env python


"""
push-based asynchronous SSL/TLS-over-TCP

This is a generic library for building event-based / asynchronous
SSL/TLS servers and clients.
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
import socket
import ssl as sys_ssl

from thor.tcp import TcpServer, TcpClient, TcpConnection, server_listen

TcpConnection._block_errs.add(sys_ssl.SSL_ERROR_WANT_READ)
TcpConnection._block_errs.add(sys_ssl.SSL_ERROR_WANT_WRITE)
TcpConnection._close_errs.add(sys_ssl.SSL_ERROR_EOF)

# TODO: TlsServer
# TODO: expose cipher info, peer info

class TlsClient(TcpClient):
    """
    An asynchronous SSL/TLS client.

    Emits:
      - connect (tcp_conn): upon connection
      - connect_error (err_type, err): if there's a problem before getting
        a connection. err_type is socket.error or socket.gaierror; err
        is the specific error encountered.

    To connect to a server:

    > c = TlsClient()
    > c.on('connect', conn_handler)
    > c.on('connect_error', error_handler)
    > c.connect(host, port)

    conn_handler will be called with the tcp_conn as the argument
    when the connection is made.
    """
    def __init__(self, loop=None):
        TcpClient.__init__(self, loop)
        # FIXME: CAs
        self.sock = sys_ssl.wrap_socket(
            self.sock, 
            cert_reqs=sys_ssl.CERT_NONE,
            do_handshake_on_connect=False
        )

    def handshake(self):
        try:
            self.sock.do_handshake()
            self.once('writable', self.handle_connect)
        except sys_ssl.SSLError, why:
            if why[0] == sys_ssl.SSL_ERROR_WANT_READ:
                self.once('readable', self.handshake)
                self.once('writable', self.handshake) # Oh, Linux...
            elif why[0] == sys_ssl.SSL_ERROR_WANT_WRITE:
                self.once('writable', self.handshake)
            else:
                self.handle_conn_error(sys_ssl.SSLError, why[0])

    # TODO: refactor into tcp.py
    def connect(self, host, port, connect_timeout=None):
        """
        Connect to host:port (with an optional connect timeout)
        and emit 'connect' when connected, or 'connect_error' in
        the case of an error.
        """
        self.host = host
        self.port = port
        self.once('writable', self.handshake)
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

def monkey_patch_ssl():
    """
    Oh, god, I feel dirty.
    
    See Python bug 11326.
    """
    if not hasattr(sys_ssl.SSLSocket, '_real_connect'):
        import _ssl
        def _real_connect(self, addr, return_errno):
            if self._sslobj:
                raise ValueError(
                    "attempt to connect already-connected SSLSocket!"
                )
            self._sslobj = _ssl.sslwrap(self._sock, False, self.keyfile,
                self.certfile, self.cert_reqs, self.ssl_version,
                self.ca_certs, self.ciphers)
            try:
                socket.socket.connect(self, addr)
                if self.do_handshake_on_connect:
                    self.do_handshake()
            except socket.error as e:
                if return_errno:
                    return e.errno
                else:
                    self._sslobj = None
                    raise e
            return 0
        def connect(self, addr):
            self._real_connect(addr, False)
        def connect_ex(self, addr):
            return self._real_connect(addr, True)
        sys_ssl.SSLSocket._real_connect = _real_connect
        sys_ssl.SSLSocket.connect = connect
        sys_ssl.SSLSocket.connect_ex = connect_ex
monkey_patch_ssl()


if __name__ == "__main__":
    import sys
    from thor import run
    test_host = sys.argv[1]

    def go(conn):
        conn.on('data', sys.stdout.write)
        conn.write("GET / HTTP/1.1\r\nHost: %s\r\n\r\n" % test_host)
        conn.pause(False)
        print conn.socket.cipher()

    c = TlsClient()
    c.on('connect', go)
    c.connect(test_host, 443)
    run()