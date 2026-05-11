#!/usr/bin/env python

try:
    import SocketServer
except ImportError:
    import socketserver as SocketServer

import errno
import socket
import unittest
from unittest.mock import MagicMock, patch

import framework

from thor import loop
from thor.tcp import TcpClient, TcpConnection


class FakeSocket:
    def __init__(self, sends=None, recvs=None, connect_error=errno.EINPROGRESS):
        self.sends = sends or []
        self.recvs = recvs or []
        self.sent = []
        self.connect_error = connect_error
        self.shutdowns = []
        self.closed = False

    def fileno(self):
        return 1

    def setblocking(self, blocking):
        pass

    def send(self, data):
        self.sent.append(data)
        return self.sends.pop(0)

    def recv(self, bufsize):
        return self.recvs.pop(0)

    def shutdown(self, how):
        self.shutdowns.append(how)

    def close(self):
        self.closed = True

    def connect_ex(self, address):
        return self.connect_error

    def getsockopt(self, level, optname):
        return 0


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
        self.server = LittleServer(
            (framework.test_host, 0), framework.LittleRequestHandler
        )
        test_port = self.server.server_address[1]

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
        self.client.connect(b"does.not.exist.invalid", 80)
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


    def test_write_closed_crash(self):
        # Use real sockets to avoid issues with loop registration
        rsock, wsock = socket.socketpair()
        rsock.setblocking(False)

        # Create connection and close it
        conn = TcpConnection(rsock, ("127.0.0.1", 80), self.loop)
        conn._close()  # Force internal close
        wsock.close()

        # Expectation: write should raise OSError
        with self.assertRaisesRegex(OSError, "Connection closed"):
            conn.write(b"foo")

    def test_stuck_close(self):
        rsock, wsock = socket.socketpair()
        rsock.setblocking(False)

        conn = TcpConnection(rsock, ("127.0.0.1", 80), self.loop)
        # Simulate buffered data
        conn._write_buffer.append(b"pending")

        # Call close
        conn.close()

        # Expectation: conn.tcp_connected should still be True because it's waiting to flush
        self.assertTrue(conn.tcp_connected)
        self.assertTrue(conn._closing)

        # Use abort to force close
        conn.abort()
        self.assertFalse(conn.tcp_connected)
        wsock.close()

    def test_close_flushes_partial_write_before_closing(self):
        loop_mock = MagicMock()
        sock = FakeSocket(sends=[2, 5], recvs=[b""])
        conn = TcpConnection(sock, ("127.0.0.1", 80), loop_mock)
        disconnects = []
        closes = []
        conn.on("disconnect", lambda: disconnects.append(True))
        conn.on("close", lambda: closes.append(True))
        conn.write(b"abcdefg")

        conn.close()
        conn.handle_writable()
        self.assertTrue(conn.tcp_connected)
        self.assertEqual(conn._write_buffer, [b"cdefg"])
        self.assertEqual(disconnects, [])
        self.assertFalse(sock.closed)

        conn.handle_writable()
        self.assertTrue(conn.tcp_connected)
        self.assertEqual(conn._write_buffer, [])
        self.assertEqual(sock.shutdowns, [socket.SHUT_WR])
        self.assertEqual(disconnects, [])
        self.assertFalse(sock.closed)

        conn.handle_readable()
        self.assertFalse(conn.tcp_connected)
        self.assertEqual(disconnects, [True])
        self.assertEqual(closes, [True])
        self.assertTrue(sock.closed)

    def test_close_half_closes_and_waits_for_peer_close(self):
        loop_mock = MagicMock()
        sock = FakeSocket(recvs=[b"ignored", b""])
        conn = TcpConnection(sock, ("127.0.0.1", 80), loop_mock)
        data = []
        closes = []
        conn.on("data", data.append)
        conn.on("close", lambda: closes.append(True))

        conn.close()

        self.assertTrue(conn.tcp_connected)
        self.assertTrue(conn._closing)
        self.assertFalse(conn._input_paused)
        self.assertEqual(sock.shutdowns, [socket.SHUT_WR])
        loop_mock.schedule.assert_called_once_with(conn.close_timeout, conn._close)

        conn.handle_readable()
        self.assertEqual(data, [])
        self.assertTrue(conn.tcp_connected)

        conn.handle_readable()
        self.assertFalse(conn.tcp_connected)
        self.assertEqual(closes, [True])
        self.assertTrue(sock.closed)

    def test_close_timeout_forces_close_after_half_close(self):
        loop_mock = MagicMock()
        timeout_ev = MagicMock()
        loop_mock.schedule.return_value = timeout_ev
        sock = FakeSocket()
        conn = TcpConnection(sock, ("127.0.0.1", 80), loop_mock)
        disconnects = []
        conn.on("disconnect", lambda: disconnects.append(True))

        conn.close()
        close_callback = loop_mock.schedule.call_args.args[1]
        close_callback()

        self.assertFalse(conn.tcp_connected)
        self.assertEqual(disconnects, [True])
        self.assertTrue(sock.closed)
        timeout_ev.delete.assert_called_once_with()

    def test_close_is_idempotent(self):
        loop_mock = MagicMock()
        sock = FakeSocket()
        conn = TcpConnection(sock, ("127.0.0.1", 80), loop_mock)
        disconnects = []
        closes = []
        conn.on("disconnect", lambda: disconnects.append(True))
        conn.on("close", lambda: closes.append(True))

        conn._handle_close()
        conn._handle_close()
        conn.close()
        conn.abort()

        self.assertEqual(disconnects, [True])
        self.assertEqual(closes, [True])
        self.assertTrue(sock.closed)

    def test_write_buffer_byte_limit(self):
        loop_mock = MagicMock()
        sock = FakeSocket()
        conn = TcpConnection(sock, ("127.0.0.1", 80), loop_mock)
        conn.max_write_buffer_size = 8
        pauses = []
        conn.on("pause", lambda paused: pauses.append(paused))

        conn.write(b"12345")
        with self.assertRaisesRegex(BufferError, "write buffer limit"):
            conn.write(b"6789")

        self.assertEqual(conn._write_buffer, [b"12345"])
        self.assertEqual(pauses, [True])

    def test_write_buffer_chunk_limit(self):
        loop_mock = MagicMock()
        sock = FakeSocket()
        conn = TcpConnection(sock, ("127.0.0.1", 80), loop_mock)
        conn.max_write_buffer_chunks = 2
        pauses = []
        conn.on("pause", lambda paused: pauses.append(paused))

        conn.write(b"1")
        conn.write(b"2")
        with self.assertRaisesRegex(BufferError, "chunk limit"):
            conn.write(b"3")

        self.assertEqual(conn._write_buffer, [b"1", b"2"])
        self.assertEqual(pauses, [True])

    def test_write_buffer_unpauses_after_dropping_below_byte_limit(self):
        loop_mock = MagicMock()
        sock = FakeSocket(sends=[5])
        conn = TcpConnection(sock, ("127.0.0.1", 80), loop_mock)
        conn.max_write_buffer_size = 8
        conn._output_paused = True
        pauses = []
        conn.on("pause", lambda paused: pauses.append(paused))
        conn.write(b"12345")

        conn.handle_writable()

        self.assertEqual(conn._write_buffer, [])
        self.assertEqual(pauses, [False])

    def test_immediate_connect_success(self):
        loop_mock = MagicMock()
        sock = FakeSocket(connect_error=0)
        client = TcpClient(loop_mock)
        connects = []
        client.on("connect", lambda conn: connects.append(conn))

        with patch("thor.tcp.socket.socket", return_value=sock):
            client.connect(b"127.0.0.1", 80)

        self.assertEqual(len(connects), 1)
        self.assertTrue(connects[0].tcp_connected)


if __name__ == "__main__":
    unittest.main()
