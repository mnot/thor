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
from typing import Union

from thor.dns import lookup
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
        self.tls_sock = None

    def handle_connect(self) -> None:
        tls_context = sys_ssl.create_default_context()
        tls_context.check_hostname = False
        tls_context.verify_mode = sys_ssl.CERT_NONE
        self.tls_sock = tls_context.wrap_socket(  # type: ignore
            self.sock,
            do_handshake_on_connect=False,
            server_hostname=self.host.decode("idna"),
        )
        self.once("fd_writable", self.handshake)

    def handshake(self) -> None:
        try:
            self.tls_sock.do_handshake()  # type: ignore
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
        tls_conn = TcpConnection(self.tls_sock, self.host, self.port, self._loop)
        self.emit("connect", tls_conn)


if __name__ == "__main__":
    import sys
    from thor import run

    test_host = sys.argv[1].encode("utf-8")

    def out(outbytes: bytes) -> None:
        sys.stdout.write(outbytes.decode("utf-8", "replace"))

    def go(conn: TcpConnection) -> None:
        conn.on("data", out)
        conn.write(b"GET / HTTP/1.1\r\nHost: %s\r\n\r\n" % test_host)
        conn.pause(False)

    c = TlsClient()
    c.on("connect", go)
    c.connect(test_host, 443)
    run()
