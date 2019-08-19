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


class LittleRequestHandler(SocketServer.BaseRequestHandler):
    def handle(self):
        # Echo the back to the client
        data = self.request.recv(1024)
        self.request.send(data)

class LittleServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    allow_reuse_address = True

# TODO: update with framework
class TestTcpClientConnect(unittest.TestCase):

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
            conn.close()
            self.loop.schedule(1, self.loop.stop)
        def check_error(err_type, err_id, err_str):
            self.error_count += 1
            self.last_error_type = err_type
            self.last_error = err_id
            self.loop.schedule(1, self.loop.stop)
        def timeout():
            self.loop.stop()
            self.timeout_hit = True
        self.timeout = timeout
        self.client = TcpClient(self.loop)
        self.client.on('connect', check_connect)
        self.client.on('connect_error', check_error)

    def test_connect(self):
        self.server = LittleServer(
            (framework.test_host, framework.test_port),
            LittleRequestHandler
        )
        def serve():
            self.server.serve_forever(poll_interval=0.1)
        t = threading.Thread(target=serve)
        t.setDaemon(True)
        t.start()
        self.client.connect(framework.test_host, framework.test_port)
        self.loop.schedule(2, self.timeout)
        self.loop.run()
        self.assertFalse(self.conn.tcp_connected)
        self.assertEqual(self.connect_count, 1)
        self.assertEqual(self.error_count, 0)
        self.assertEqual(self.timeout_hit, False)
        self.server.shutdown()
        self.server.socket.close()

    def test_connect_refused(self):
        self.client.connect(framework.refuse_host, framework.refuse_port)
        self.loop.schedule(3, self.timeout)
        self.loop.run()
        self.assertEqual(self.connect_count, 0)
        self.assertEqual(self.error_count, 1)
        self.assertEqual(self.last_error_type, 'socket')
        self.assertEqual(self.last_error, errno.ECONNREFUSED)
        self.assertEqual(self.timeout_hit, False)

    def test_connect_noname(self):
        self.client.connect(b'does.not.exist', framework.test_port)
        self.loop.schedule(3, self.timeout)
        self.loop.run()
        self.assertEqual(self.connect_count, 0)
        self.assertEqual(self.error_count, 1)
        self.assertEqual(self.last_error_type, 'gai')
        self.assertEqual(self.last_error, socket.EAI_NONAME)
        self.assertEqual(self.timeout_hit, False)

    def test_connect_timeout(self):
        self.client.connect(framework.timeout_host, framework.timeout_port, 1)
        self.loop.schedule(3, self.timeout)
        self.loop.run()
        self.assertEqual(self.connect_count, 0)
        self.assertEqual(self.error_count, 1)
        self.assertEqual(self.last_error_type, 'socket')
        self.assertEqual(self.last_error, errno.ETIMEDOUT,
                         errno.errorcode.get(self.last_error, self.last_error))
        self.assertEqual(self.timeout_hit, False)

# TODO:
#   def test_pause(self):

if __name__ == '__main__':
    unittest.main()
