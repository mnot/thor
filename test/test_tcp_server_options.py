import unittest
from unittest.mock import patch, MagicMock
import socket
from thor.tcp import TcpServer, server_listen

class TestTcpServerOptions(unittest.TestCase):
    @patch('thor.tcp.socket.socket')
    def test_server_listen_options(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock
        
        # Test default backlog
        server_listen(b'localhost', 1234)
        mock_sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        mock_sock.setblocking.assert_called_with(False)
        mock_sock.bind.assert_called_with((b'localhost', 1234))
        mock_sock.listen.assert_called_with(socket.SOMAXCONN)

        # Test custom backlog
        server_listen(b'localhost', 1234, backlog=10)
        mock_sock.listen.assert_called_with(10)

    @patch('thor.tcp.server_listen')
    def test_tcp_server_passes_backlog(self, mock_server_listen):
        # We need to mock event source init stuff or just let it fail later if we don't care, 
        # but TcpServer calls EventSource.__init__ which might use 'loop'.
        # Since we just want to verify __init__ calls server_listen, we can rely on mock_server_listen returning a mock socket.
        mock_sock = MagicMock()
        mock_server_listen.return_value = mock_sock
        
        # We need to mock loop to avoid side effects or errors if TcpServer uses it immediately
        # TcpServer init: EventSource.__init__(self, loop) -> schedule(0, self.emit, "start")
        # So we should probably pass a mock loop.
        mock_loop = MagicMock()
        
        TcpServer(b'localhost', 1234, loop=mock_loop, backlog=50)
        mock_server_listen.assert_called_with(b'localhost', 1234, 50)
        
        TcpServer(b'localhost', 1234, loop=mock_loop)
        # If backlog is None (default), it passes None.
        mock_server_listen.assert_called_with(b'localhost', 1234, None)

if __name__ == '__main__':
    unittest.main()
