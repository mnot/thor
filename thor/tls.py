#!/usr/bin/env python


"""
push-based asynchronous SSL/TLS-over-TCP

This is a generic library for building event-based / asynchronous
SSL/TLS servers and clients.
"""

import errno
import os
import socket
import ssl as sys_ssl

from thor.loop import LoopBase
from thor.tcp import TcpClient, TcpConnection

TcpConnection.block_errs.add(sys_ssl.SSL_ERROR_WANT_READ)
TcpConnection.block_errs.add(sys_ssl.SSL_ERROR_WANT_WRITE)
TcpConnection.close_errs.add(sys_ssl.SSL_ERROR_EOF)
TcpConnection.close_errs.add(sys_ssl.SSL_ERROR_SSL)

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
    def __init__(self, loop: LoopBase = None) -> None:
        TcpClient.__init__(self, loop)
        try:
            self.tls_context = sys_ssl.SSLContext(sys_ssl.PROTOCOL_SSLv23)
        except AttributeError:
            self.tls_context = None

    def handshake(self) -> None:
        try:
            self.sock.do_handshake() # type: ignore
            self.once('fd_writable', self.handle_connect)
        except sys_ssl.SSLError as why:
            if isinstance(why, sys_ssl.SSLWantReadError):
#            if why == sys_ssl.SSL_ERROR_WANT_READ:
#                self.once('fd_readable', self.handshake)
                self.once('fd_writable', self.handshake) # Oh, Linux...
#            elif why == sys_ssl.SSL_ERROR_WANT_WRITE:
            elif isinstance(why, sys_ssl.SSLWantWriteError):
                self.once('fd_writable', self.handshake)
            else:
                self.handle_socket_error(why, 'ssl')
        except socket.error as why:
            self.handle_socket_error(why, 'ssl')

    # TODO: refactor into tcp.py
    def connect(self, host: bytes, port: int, connect_timeout: float = None) -> None:
        """
        Connect to host:port (with an optional connect timeout)
        and emit 'connect' when connected, or 'connect_error' in
        the case of an error.
        """
        self.host = host
        self.port = port
        self.once('fd_writable', self.handshake)
        # FIXME: CAs
        if self.tls_context:
            self.sock = self.tls_context.wrap_socket( # type: ignore
                self.sock,
                do_handshake_on_connect=False,
                server_hostname=self.host
            )
        else: # server_hostname requires 2.7.9
            self.sock = sys_ssl.wrap_socket(
                self.sock,
                cert_reqs=sys_ssl.CERT_NONE,
                do_handshake_on_connect=False
            )
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


if __name__ == "__main__":
    import sys
    from thor import run
    test_host = sys.argv[1].encode('utf-8')

    def out(outbytes: bytes) -> None:
        sys.stdout.write(outbytes.decode('utf-8', 'replace'))

    def go(conn: TcpConnection) -> None:
        conn.on('data', out)
        conn.write(b"GET / HTTP/1.1\r\nHost: %s\r\n\r\n" % test_host)
        conn.pause(False)

    c = TlsClient()
    c.on('connect', go)
    c.connect(test_host, 443)
    run()
