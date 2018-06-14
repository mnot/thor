#!/usr/bin/env python

import socket
import ssl
import sys
import threading
import unittest

from thor import loop
from thor.tls import TlsClient

# TODO: set up ssl servers for testing, don't use these.
test_host = b"www.google.com"
test_host2 = b"www.mnot.net"
test_port = 443

# TODO: update with framework
class TestTlsClientConnect(unittest.TestCase):

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
            conn.write(b"GET / HTTP/1.0\r\n\r\n")
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
        self.client = TlsClient(self.loop)
        self.client.on('connect', check_connect)
        self.client.on('connect_error', check_error)

    def test_connect(self):
        self.client.connect(test_host, test_port)
        self.loop.schedule(2, self.timeout)
        self.loop.run()
        self.assertFalse(self.conn.tcp_connected)
        self.assertEqual(self.connect_count, 1)
        self.assertEqual(self.error_count, 0)
        self.assertEqual(self.timeout_hit, False)

# causing problems on CI infra
#    def test_connect_refused(self):
#        self.client.connect(test_host2, 25)
#        self.loop.schedule(3, self.timeout)
#        self.loop.run()
#        self.assertEqual(self.connect_count, 0)
#        self.assertEqual(self.error_count, 1)
#        self.assertEqual(self.last_error_type, socket.error)
#        self.assertEqual(self.last_error, ssl.errno.EINVAL)
#        self.assertEqual(self.timeout_hit, False)

    def test_connect_noname(self):
        self.client.connect('does.not.exist', test_port)
        self.loop.schedule(3, self.timeout)
        self.loop.run()
        self.assertEqual(self.connect_count, 0)
        self.assertEqual(self.error_count, 1)
        self.assertEqual(self.last_error_type, 'gai')
        self.assertEqual(self.last_error, socket.EAI_NONAME)
        self.assertEqual(self.timeout_hit, False)

    def test_connect_timeout(self):
        self.client.connect('128.66.0.1', test_port, 1)
        self.loop.schedule(3, self.timeout)
        self.loop.run()
        self.assertEqual(self.connect_count, 0)
        self.assertEqual(self.error_count, 1)
        self.assertEqual(self.last_error_type, 'socket')
        self.assertEqual(self.last_error, ssl.errno.ETIMEDOUT)
        self.assertEqual(self.timeout_hit, False)

# TODO:
#   def test_pause(self):

if __name__ == '__main__':
    unittest.main()
