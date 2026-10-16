import errno
import os
import socket
import unittest
from typing import List
from unittest.mock import MagicMock, patch

import thor
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


    def test_accept_paused_emitted_once_with_errno(self):
        for err in [errno.EMFILE, errno.EPERM]:
            with self.subTest(err=err):
                server, _, _ = self.make_exhausted_server(err)
                paused = MagicMock()
                server.on("accept_paused", paused)

                server.handle_accept()
                server.handle_accept()  # still paused: timer pending

                paused.assert_called_once_with(err)

    def test_no_accept_events_on_ignored_accept_errors(self):
        for err in sorted(TcpServer.accept_errs):
            with self.subTest(err=err):
                server, _, _ = self.make_exhausted_server(err)
                listener = MagicMock()
                server.on("accept_paused", listener)
                server.on("accept_resumed", listener)

                server.handle_accept()

                listener.assert_not_called()

    def test_accept_resumed_emitted_when_backoff_ends(self):
        server, _, mock_loop = self.make_exhausted_server(errno.EMFILE)
        resumed = MagicMock()
        server.on("accept_resumed", resumed)
        server.handle_accept()
        resumed.assert_not_called()

        mock_loop.schedule.call_args[0][1]()

        resumed.assert_called_once_with()

    def test_accept_paused_again_after_resume(self):
        server, _, mock_loop = self.make_exhausted_server(errno.EMFILE)
        paused = MagicMock()
        server.on("accept_paused", paused)
        server.handle_accept()
        mock_loop.schedule.call_args[0][1]()

        server.handle_accept()

        self.assertEqual(paused.call_count, 2)

    def test_shutdown_during_accept_backoff_emits_no_accept_resumed(self):
        for method in ["shutdown", "graceful_shutdown"]:
            with self.subTest(method=method):
                server, _, mock_loop = self.make_exhausted_server(errno.EMFILE)
                resumed = MagicMock()
                server.on("accept_resumed", resumed)
                server.handle_accept()
                resume = mock_loop.schedule.call_args[0][1]

                getattr(server, method)()
                resume()

                resumed.assert_not_called()

    def test_accept_paused_listener_error_leaves_server_paused(self):
        server, _, mock_loop = self.make_exhausted_server(errno.EMFILE)
        server.on("accept_paused", MagicMock(side_effect=RuntimeError("boom")))

        with self.assertRaises(RuntimeError):
            server.handle_accept()

        mock_loop.event_del.assert_called_once_with(17, "fd_readable")
        self.assertIsNotNone(server._accept_timer)  # pylint: disable=protected-access
        mock_loop.schedule.call_args[0][1]()
        mock_loop.event_add.assert_called_once_with(17, "fd_readable")

    def test_accept_resumed_listener_error_leaves_server_listening(self):
        server, _, mock_loop = self.make_exhausted_server(errno.EMFILE)
        server.on("accept_resumed", MagicMock(side_effect=RuntimeError("boom")))
        server.handle_accept()

        with self.assertRaises(RuntimeError):
            mock_loop.schedule.call_args[0][1]()

        mock_loop.event_add.assert_called_once_with(17, "fd_readable")
        self.assertIsNone(server._accept_timer)  # pylint: disable=protected-access

    def test_sets_are_disjoint(self):
        self.assertFalse(TcpServer.accept_errs & TcpServer.accept_backoff_errs)

    def test_persistent_accept_failure_does_not_spin(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        listener.bind(("127.0.0.1", 0))
        listener.setblocking(False)
        try:
            listener.accept()
        except OSError as why:
            if why.errno not in TcpServer.accept_backoff_errs:
                listener.close()
                self.skipTest(f"accept() on a datagram socket gave {why!r}")
        # the datagram is never consumed, so the listener stays readable
        sender.sendto(b"x", listener.getsockname())

        class CountingServer(TcpServer):
            accepts = 0
            accept_backoff = 0.05

            def handle_accept(self) -> None:
                self.accepts += 1
                super().handle_accept()

        paused_errnos: List[int] = []
        resumed = 0

        def on_resumed() -> None:
            nonlocal resumed
            resumed += 1

        window = 0.5
        loop = thor.loop.make()
        server = CountingServer(b"127.0.0.1", 0, sock=listener, loop=loop)
        server.on("accept_paused", paused_errnos.append)
        server.on("accept_resumed", on_resumed)
        loop.schedule(window, loop.stop)
        loop.run()
        server.shutdown()

        self.assertGreaterEqual(server.accepts, 1)
        self.assertLessEqual(server.accepts, window / server.accept_backoff + 2)
        # one pause per backoff window; each is resumed except possibly the last
        self.assertEqual(len(paused_errnos), server.accepts)
        self.assertTrue(set(paused_errnos) <= TcpServer.accept_backoff_errs)
        self.assertIn(resumed, (len(paused_errnos), len(paused_errnos) - 1))


if __name__ == "__main__":
    unittest.main()
