import errno
import os
import socket
import unittest
from unittest.mock import MagicMock, patch

from thor.tcp import TcpServer, server_listen


class TestTcpServerOptions(unittest.TestCase):
    @patch("thor.tcp.socket.socket")
    def test_server_listen_options(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        # Test default backlog
        server_listen(b"localhost", 1234)
        mock_sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        mock_sock.setblocking.assert_called_with(False)
        mock_sock.bind.assert_called_with((b"localhost", 1234))
        mock_sock.listen.assert_called_with(socket.SOMAXCONN)

        # Test custom backlog
        server_listen(b"localhost", 1234, backlog=10)
        mock_sock.listen.assert_called_with(10)

    @patch("thor.tcp.server_listen")
    def test_tcp_server_passes_backlog(self, mock_server_listen):
        # We need to mock event source init stuff or just let it fail later if we don't care,
        # but TcpServer calls EventSource.__init__ which might use 'loop'.
        # Since we just want to verify __init__ calls server_listen, we can rely on mock_server_listen returning a mock socket.
        mock_sock = MagicMock()
        mock_sock.fileno.return_value = 17
        mock_server_listen.return_value = mock_sock

        # We need to mock loop to avoid side effects or errors if TcpServer uses it immediately
        # TcpServer init: EventSource.__init__(self, loop) -> schedule(0, self.emit, "start")
        # So we should probably pass a mock loop.
        mock_loop = MagicMock()

        TcpServer(b"localhost", 1234, loop=mock_loop, backlog=50)
        mock_server_listen.assert_called_with(b"localhost", 1234, 50)

        TcpServer(b"localhost", 1234, loop=mock_loop)
        # If backlog is None (default), it passes None.
        mock_server_listen.assert_called_with(b"localhost", 1234, None)

    def test_tcp_server_uses_supplied_loop_for_start_event(self):
        mock_sock = MagicMock()
        mock_sock.fileno.return_value = 17
        mock_loop = MagicMock()

        server = TcpServer(b"localhost", 1234, sock=mock_sock, loop=mock_loop)

        mock_loop.schedule.assert_called_once_with(0, server.emit, "start")

    def test_shutdown_unregisters_listening_fd(self):
        mock_sock = MagicMock()
        mock_sock.fileno.return_value = 17
        mock_loop = MagicMock()
        server = TcpServer(b"localhost", 1234, sock=mock_sock, loop=mock_loop)

        server.shutdown()

        mock_loop.unregister_fd.assert_called_with(17)
        mock_sock.close.assert_called_once_with()
        self.assertIsNone(server.sock)

    def test_graceful_shutdown_unregisters_listening_fd(self):
        mock_sock = MagicMock()
        mock_sock.fileno.return_value = 17
        mock_loop = MagicMock()
        server = TcpServer(b"localhost", 1234, sock=mock_sock, loop=mock_loop)

        server.graceful_shutdown()

        mock_loop.unregister_fd.assert_called_with(17)
        mock_sock.close.assert_called_once_with()
        self.assertIsNone(server.sock)

    def test_handle_accept_ignores_transient_accept_errors(self):
        for err in [errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR, errno.ECONNABORTED]:
            with self.subTest(err=err):
                mock_sock = MagicMock()
                mock_sock.fileno.return_value = 17
                mock_sock.accept.side_effect = OSError(err, os.strerror(err))
                mock_loop = MagicMock()
                server = TcpServer(b"localhost", 1234, sock=mock_sock, loop=mock_loop)
                connects = []
                server.on("connect", connects.append)

                server.handle_accept()

                self.assertEqual(connects, [])

    def test_handle_accept_ignores_network_accept_errors(self):
        names = [
            "EPROTO",
            "ENETDOWN",
            "EHOSTDOWN",
            "ENONET",
            "EHOSTUNREACH",
            "ENETUNREACH",
            "ETIMEDOUT",
        ]
        for name in names:
            err = getattr(errno, name, None)
            if err is None:
                continue
            with self.subTest(err=name):
                server, _, mock_loop = self.make_exhausted_server(err)
                connects = []
                server.on("connect", connects.append)

                server.handle_accept()

                self.assertEqual(connects, [])
                mock_loop.event_del.assert_not_called()
                mock_loop.schedule.assert_not_called()
                self.assertIn("fd_readable", server.interesting_events())
                self.assertIsNone(server._accept_timer)  # pylint: disable=protected-access

    def test_handle_accept_backs_off_on_policy_accept_errors(self):
        for name in ["EPERM", "EOPNOTSUPP", "ENOPROTOOPT"]:
            err = getattr(errno, name, None)
            if err is None:
                continue
            with self.subTest(err=name):
                server, _, mock_loop = self.make_exhausted_server(err)

                server.handle_accept()

                mock_loop.event_del.assert_called_once_with(17, "fd_readable")
                self.assertEqual(mock_loop.schedule.call_count, 1)
                self.assertNotIn("fd_readable", server.interesting_events())
                self.assertIsNotNone(server.sock)

    def test_handle_accept_reraises_unexpected_accept_errors(self):
        mock_sock = MagicMock()
        mock_sock.fileno.return_value = 17
        mock_sock.accept.side_effect = OSError(errno.EBADF, os.strerror(errno.EBADF))
        mock_loop = MagicMock()
        server = TcpServer(b"localhost", 1234, sock=mock_sock, loop=mock_loop)

        with self.assertRaises(OSError):
            server.handle_accept()

    def make_exhausted_server(self, err):
        mock_sock = MagicMock()
        mock_sock.fileno.return_value = 17
        mock_sock.accept.side_effect = OSError(err, os.strerror(err))
        mock_loop = MagicMock()
        server = TcpServer(b"localhost", 1234, sock=mock_sock, loop=mock_loop)
        mock_loop.reset_mock()
        return server, mock_sock, mock_loop

    def test_handle_accept_survives_resource_exhaustion(self):
        for err in [errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM]:
            with self.subTest(err=err):
                server, _, mock_loop = self.make_exhausted_server(err)

                server.handle_accept()

                # stops watching the listener, and schedules a retry
                mock_loop.event_del.assert_called_once_with(17, "fd_readable")
                self.assertEqual(mock_loop.schedule.call_count, 1)
                self.assertGreater(mock_loop.schedule.call_args[0][0], 0)
                self.assertNotIn("fd_readable", server.interesting_events())
                self.assertIsNotNone(server.sock)

    def test_accept_resumes_after_backoff(self):
        server, _, mock_loop = self.make_exhausted_server(errno.EMFILE)
        server.handle_accept()
        resume = mock_loop.schedule.call_args[0][1]

        resume()

        mock_loop.event_add.assert_called_once_with(17, "fd_readable")
        self.assertIn("fd_readable", server.interesting_events())

    def test_shutdown_during_accept_backoff_does_not_resume(self):
        for method in ["shutdown", "graceful_shutdown"]:
            with self.subTest(method=method):
                server, _, mock_loop = self.make_exhausted_server(errno.EMFILE)
                server.handle_accept()
                timer = mock_loop.schedule.return_value
                resume = mock_loop.schedule.call_args[0][1]

                getattr(server, method)()
                timer.delete.assert_called_once_with()
                resume()  # even if the timer had already fired

                mock_loop.event_add.assert_not_called()
                self.assertNotIn("fd_readable", server.interesting_events())


if __name__ == "__main__":
    unittest.main()
