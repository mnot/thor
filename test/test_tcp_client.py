#!/usr/bin/env python

try:
    import SocketServer
except ImportError:
    import socketserver as SocketServer

import errno
import socket
import sys
import threading
import unittest

import framework

from thor import loop
from thor.tcp import TcpClient


class LittleServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    allow_reuse_address = True


class TestTcpClientConnect(framework.ClientServerTestCase):
    def setUp(self):
        self.loop = loop.make()
        self.connect_count = 0
        self.error_count = 0
        self.last_error_type = None
        self.last_error = None
        self.timeout_hit = False
        self.conn = None

        def check_connect(conn):
            self.conn = conn
            self.assertTrue(conn.tcp_connected)
            self.connect_count += 1
            conn.write(b"test")
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
        self.client = TcpClient(self.loop)
        self.client.on("connect", check_connect)
        self.client.on("connect_error", check_error)

    def start_server(self):
        test_port = self.get_port()
        self.server = LittleServer(
            (framework.test_host, test_port), framework.LittleRequestHandler
        )

        def serve():
            self.server.serve_forever(poll_interval=0.1)

        self.move_to_thread(serve)
        return test_port

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def test_connect(self):
        test_port = self.start_server()
        self.client.connect(framework.test_host, test_port)
        self.loop.schedule(2, self.timeout)
        try:
            self.loop.run()
        finally:
            self.stop_server()
        self.assertEqual(self.connect_count, 1)
        self.assertEqual(self.error_count, 0)
        self.assertEqual(self.timeout_hit, False)

    def test_connect_refused(self):
        self.client.connect(framework.refuse_host, framework.refuse_port)
        self.loop.schedule(3, self.timeout)
        self.loop.run()
        self.assertEqual(self.connect_count, 0)
        self.assertEqual(self.error_count, 1)
        self.assertEqual(self.last_error_type, "socket")
        self.assertEqual(self.last_error, errno.ECONNREFUSED)
        self.assertEqual(self.timeout_hit, False)

    def test_connect_noname(self):
        self.client.connect(b"does.not.exist", 80)
        self.loop.schedule(3, self.timeout)
        self.loop.run()
        self.assertEqual(self.connect_count, 0)
        self.assertEqual(self.error_count, 1)
        self.assertEqual(self.last_error_type, "socket", self.last_error_str)
        self.assertEqual(self.last_error, socket.EAI_NONAME)
        self.assertEqual(self.timeout_hit, False)

    def test_ip_check(self):
        test_port = self.start_server()

        def ip_check(dns_result):
            return False

        self.client.check_ip = ip_check
        self.client.connect(framework.test_host, test_port)
        self.loop.schedule(2, self.timeout)
        try:
            self.loop.run()
        finally:
            self.stop_server()
        self.assertEqual(self.connect_count, 0)
        self.assertEqual(self.error_count, 1)
        self.assertEqual(self.timeout_hit, False)

    def test_connect_timeout(self):
        self.client.connect(framework.timeout_host, framework.timeout_port, 1)
        self.loop.schedule(3, self.timeout)
        self.loop.run()
        self.assertEqual(self.connect_count, 0)
        self.assertEqual(self.error_count, 1)
        self.assertEqual(self.last_error_type, "socket")
        self.assertEqual(
            self.last_error,
            errno.ETIMEDOUT,
            errno.errorcode.get(self.last_error, self.last_error),
        )
        self.assertEqual(self.timeout_hit, False)


#   def test_pause(self):

if __name__ == "__main__":
    unittest.main()
