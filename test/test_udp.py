#!/usr/bin/env python

import errno
import socket
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

import framework

from thor import loop
from thor.udp import UdpEndpoint


class TestUdpEndpoint(unittest.TestCase):
    def setUp(self):
        self.loop = loop.make()
        self.ep1 = UdpEndpoint(self.loop)
        self.ep1.bind(framework.test_host, 0)
        self.ep1.on("datagram", self.input)
        self.ep1.pause(False)
        self.ep2 = UdpEndpoint()
        self.loop.schedule(5, self.timeout)
        self.timeout_hit = False
        self.datagrams = []

    def tearDown(self):
        self.ep1.shutdown()
        self.ep2.shutdown()

    def timeout(self):
        self.timeout_hit = True
        self.loop.stop()

    def input(self, data, host, port):
        self.datagrams.append((data, host, port))

    def output(self, msg):
        assert self.ep1.sock
        port = self.ep1.sock.getsockname()[1]
        self.ep2.send(msg, framework.test_host.decode("ascii"), port)

    def test_basic(self):
        self.loop.schedule(1, self.output, b"foo!")
        self.loop.schedule(2, self.output, b"bar!")

        def check():
            try:
                self.assertEqual(self.datagrams[0][0], b"foo!")
                self.assertEqual(self.datagrams[1][0], b"bar!")
            finally:
                self.loop.stop()

        self.loop.schedule(3, check)
        self.loop.run()

    @patch("thor.udp.socket.socket")
    def test_shutdown_unregisters_fd_and_closes_socket(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.fileno.return_value = 17
        mock_sock.getsockopt.return_value = 8192
        mock_socket_cls.return_value = mock_sock
        mock_loop = MagicMock()
        endpoint = UdpEndpoint(mock_loop)

        endpoint.shutdown()

        mock_loop.unregister_fd.assert_called_once_with(17)
        mock_sock.close.assert_called_once_with()
        self.assertIsNone(endpoint.sock)

    @patch("thor.udp.socket.socket")
    def test_shutdown_is_idempotent(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.fileno.return_value = 17
        mock_sock.getsockopt.return_value = 8192
        mock_socket_cls.return_value = mock_sock
        endpoint = UdpEndpoint(MagicMock())

        endpoint.shutdown()
        endpoint.shutdown()

        mock_sock.close.assert_called_once_with()

    @patch("thor.udp.socket.socket")
    def test_late_bind_callback_after_shutdown_is_ignored(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.fileno.return_value = 17
        mock_sock.getsockopt.return_value = 8192
        mock_socket_cls.return_value = mock_sock
        endpoint = UdpEndpoint(MagicMock())

        endpoint.shutdown()
        endpoint._continue_bind(
            [
                (
                    socket.AF_INET,
                    socket.SOCK_DGRAM,
                    socket.IPPROTO_IP,
                    "",
                    ("127.0.0.1", 9999),
                )
            ]
        )

        mock_sock.bind.assert_not_called()

    @patch("thor.udp.socket.socket")
    def test_send_rejects_oversized_datagram(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.fileno.return_value = 17
        mock_sock.getsockopt.return_value = 8192
        mock_socket_cls.return_value = mock_sock
        endpoint = UdpEndpoint(MagicMock())
        endpoint.max_dgram = 3

        with self.assertRaisesRegex(ValueError, "UDP datagram"):
            endpoint.send(b"1234", "127.0.0.1", 9999)

        mock_sock.sendto.assert_not_called()

    @patch("thor.udp.socket.socket")
    def test_send_after_shutdown_raises_clear_error(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.fileno.return_value = 17
        mock_sock.getsockopt.return_value = 8192
        mock_socket_cls.return_value = mock_sock
        endpoint = UdpEndpoint(MagicMock())
        endpoint.shutdown()

        with self.assertRaisesRegex(OSError, "UDP endpoint closed"):
            endpoint.send(b"hello", "127.0.0.1", 9999)


#    def test_bigdata(self):
#        self.loop.schedule(1, self.output, b"a" * 100)
#        self.loop.schedule(2, self.output, b"b" * 1000)
#        self.loop.schedule(3, self.output, b"c" * self.ep1.max_dgram)
#
#        def check():
#            self.assertEqual(self.datagrams[0][0], b"a" * 100)
#            self.assertEqual(self.datagrams[1][0], b"b" * 1000)
#            # we only check the first 1000 characters because, well,
#            # it's lossy.
#            self.assertEqual(self.datagrams[2][0][:1000], b"c" * 1000)
#            self.loop.stop()
#
#        self.loop.schedule(4, check)
#        self.loop.run()


#   def test_pause(self):


if __name__ == "__main__":
    unittest.main()
