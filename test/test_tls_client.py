#!/usr/bin/env python

try:
    import SocketServer
except ImportError:
    import socketserver as SocketServer

import socket
import ssl
import sys
import threading
import unittest

import framework

from thor import loop
from thor.tls import TlsClient
import pytest


class LittleTlsServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    def __init__(
        self,
        server_address,
        RequestHandlerClass,
        certfile,
        keyfile,
        ssl_version=ssl.PROTOCOL_TLS_SERVER,
        bind_and_activate=True,
    ):
        self.context = ssl.SSLContext(ssl_version)
        self.context.load_cert_chain(certfile, keyfile)

        SocketServer.TCPServer.__init__(
            self, server_address, RequestHandlerClass, bind_and_activate
        )

    def get_request(self):
        newsocket, fromaddr = self.socket.accept()
        connstream = self.context.wrap_socket(newsocket, server_side=True)
        return connstream, fromaddr


class TestTlsClientConnect(framework.ClientServerTestCase):
    def setUp(self):
        self.loop = loop.make()
        self.connect_count = 0
        self.error_count = 0
        self.last_error_type = None
        self.last_error = None
        self.last_error_str = None
        self.timeout_hit = False
        self.conn = None

        def check_connect(conn):
            self.conn = conn
            self.assertTrue(conn.tcp_connected)
            self.connect_count += 1
            conn.write(b"GET / HTTP/1.0\r\n\r\n")
            self.loop.schedule(1, self.loop.stop)

        def check_error(err_type, err_id, err_str):
            self.error_count += 1
            self.last_error_type = err_type
            self.last_error = err_id
            self.last_error_str = err_str
            self.loop.schedule(1, self.loop.stop)

        def timeout():
            self.loop.stop()
            self.timeout_hit = True

        self.timeout = timeout
        self.client = TlsClient(self.loop)
        self.client.on("connect", check_connect)
        self.client.on("connect_error", check_error)

    def start_server(self):
        self.server = LittleTlsServer(
            (framework.tls_host, framework.tls_port),
            framework.LittleRequestHandler,
            "test/test.cert",
            "test/test.key",
        )

        def serve():
            self.server.serve_forever(poll_interval=0.1)

        self.move_to_thread(serve)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def test_connect(self):
        self.start_server()
        self.client.connect(framework.tls_host, framework.tls_port)
        self.loop.schedule(5, self.timeout)
        try:
            self.loop.run()
        finally:
            self.stop_server()
        self.assertEqual(
            self.error_count,
            0,
            (self.last_error_type, self.last_error, self.last_error_str),
        )
        self.assertEqual(self.timeout_hit, False)
        self.assertEqual(self.connect_count, 1)

    @pytest.mark.xfail
    def test_connect_refused(self):
        self.client.connect(framework.refuse_host, framework.refuse_port)
        self.loop.schedule(3, self.timeout)
        self.loop.run()
        self.assertEqual(self.connect_count, 0)
        self.assertEqual(self.error_count, 1)
        self.assertEqual(self.last_error, ssl.errno.EINVAL)
        self.assertEqual(self.timeout_hit, False)

    def test_connect_noname(self):
        self.client.connect(b"does.not.exist", framework.tls_port)
        self.loop.schedule(3, self.timeout)
        self.loop.run()
        self.assertEqual(self.connect_count, 0)
        self.assertEqual(self.error_count, 1)
        self.assertEqual(self.last_error_type, "socket")
        self.assertEqual(self.last_error, socket.EAI_NONAME)
        self.assertEqual(self.timeout_hit, False)

    def test_connect_timeout(self):
        self.client.connect(framework.timeout_host, framework.timeout_port, 1)
        self.loop.schedule(3, self.timeout)
        self.loop.run()
        self.assertEqual(self.connect_count, 0)
        self.assertEqual(self.error_count, 1)
        self.assertEqual(self.last_error_type, "socket")
        self.assertEqual(self.last_error, ssl.errno.ETIMEDOUT)
        self.assertEqual(self.timeout_hit, False)


#   def test_pause(self):

if __name__ == "__main__":
    unittest.main()
