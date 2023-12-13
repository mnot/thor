#!/usr/bin/env python


"""
push-based asynchronous SSL/TLS-over-TCP

This is a generic library for building event-based / asynchronous
SSL/TLS servers and clients.
"""

import socket
import ssl as sys_ssl
from typing import Optional

from thor.loop import LoopBase
from thor.tcp import TcpClient, TcpConnection

TcpConnection.block_errs.add(sys_ssl.SSL_ERROR_WANT_READ)
TcpConnection.block_errs.add(sys_ssl.SSL_ERROR_WANT_WRITE)
TcpConnection.close_errs.add(sys_ssl.SSL_ERROR_EOF)
TcpConnection.close_errs.add(sys_ssl.SSL_ERROR_SSL)


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
    > c.connect(address)

    conn_handler will be called with the tcp_conn as the argument
    when the connection is made.
    """

    _tls_context = sys_ssl.create_default_context()

    def __init__(self, loop: Optional[LoopBase] = None) -> None:
        TcpClient.__init__(self, loop)
        self.tls_sock: Optional[sys_ssl.SSLSocket] = None
        self._tls_context.check_hostname = False
        self._tls_context.verify_mode = sys_ssl.CERT_NONE

    def handle_connect(self) -> None:
        assert self.sock, "self.sock not found in handle_connect"
        assert self.hostname, "hostname not found in handle_connect"
        try:
            self.tls_sock = self._tls_context.wrap_socket(
                self.sock,
                do_handshake_on_connect=False,
                server_hostname=self.hostname.decode("idna"),
            )
        except OSError as why:
            self.handle_socket_error(why, "ssl")
            return
        self.once("fd_writable", self.handshake)

    def handshake(self) -> None:
        assert self.tls_sock, "tls_sock not found in handshake"
        try:
            self.tls_sock.do_handshake()
            self.once("fd_writable", self.handle_tls_connect)
        except sys_ssl.SSLError as why:
            if isinstance(why, sys_ssl.SSLWantReadError):
                self.once("fd_writable", self.handshake)  # Oh, Linux...
            elif isinstance(why, sys_ssl.SSLWantWriteError):
                self.once("fd_writable", self.handshake)
            else:
                self.handle_socket_error(why, "ssl")
        except socket.error as why:
            self.handle_socket_error(why, "ssl")
        except AttributeError:
            # For some reason, wrap_context is returning None. Try again.
            self.once("fd_writable", self.handshake)

    def handle_tls_connect(self) -> None:
        self.unregister_fd()
        if self._timeout_ev:
            self._timeout_ev.delete()
        assert self.tls_sock, "tls_sock not found in handle_tls_connect"
        assert self.address, "address not found in handle_tls_connect"
        tls_conn = TcpConnection(self.tls_sock, self.address, self._loop)
        self.emit("connect", tls_conn)
