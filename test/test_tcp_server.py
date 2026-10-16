#!/usr/bin/env python

import os
import socket
import subprocess
import sys
import time
import unittest

try:
    import resource
except ImportError:  # not available on Windows
    resource = None  # type: ignore

import framework

import thor
from thor.events import on


class TestTcpServer(framework.ClientServerTestCase):
    def create_server(self, server_side):
        server = thor.TcpServer(framework.test_host, 0, loop=self.loop)
        test_port = server.sock.getsockname()[1]
        server.conn_count = 0

        def run_server(conn):
            server.conn_count += 1
            server_side(conn)

        server.on("connect", run_server)

        def stop():
            self.assertTrue(server.conn_count > 0)
            server.shutdown()

        return (stop, test_port)

    def create_client(self, host, port, client_side):
        def run_client(client_side1):
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect((host, port))
            client_side1(client)
            client.close()

        self.move_to_thread(target=run_client, args=(client_side,))
        self.loop.schedule(1, self.loop.stop)

    def test_basic(self):
        def server_side(server_conn):
            self.server_recv = 0

            def check_data(chunk):
                self.assertEqual(chunk, b"foo!")
                self.server_recv += 1

            server_conn.on("data", check_data)
            server_conn.pause(False)
            server_conn.write(b"bar!")

        def client_side(client_conn):
            sent = client_conn.send(b"foo!")

        self.go([server_side], [client_side])
        self.assertTrue(self.server_recv > 0, self.server_recv)

    @unittest.skipIf(resource is None, "resource module not available")
    def test_fd_exhaustion_backoff(self):
        # Run in a subprocess: the scenario exhausts the process's descriptors,
        # which threads left over from other tests could otherwise disturb.
        here = os.path.dirname(os.path.abspath(__file__))
        env = dict(
            os.environ, PYTHONPATH=os.pathsep.join([here, os.path.dirname(here)])
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import test_tcp_server; test_tcp_server.fd_exhaustion_scenario()",
            ],
            env=env,
            cwd=here,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


def fd_exhaustion_scenario():
    "Check that a TcpServer backs off when accept() fails with EMFILE, then recovers."
    check = unittest.TestCase()
    loop = thor.loop.make()
    backoff = 0.2
    attempts = []  # (monotonic time, connections accepted so far)
    accepted = []
    clients = []

    class CountingServer(thor.TcpServer):
        def handle_accept(self):
            attempts.append((time.monotonic(), len(accepted)))
            super().handle_accept()
            if len(attempts) == 2:
                clients.pop().close()  # free a descriptor for the next try

    def on_connect(conn):
        accepted.append(conn)
        loop.stop()

    server = CountingServer(framework.test_host, 0, loop=loop)
    server.accept_backoff = backoff
    server.on("connect", on_connect)
    port = server.sock.getsockname()[1]

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        # leave a handful of spare descriptors, then use them all up
        resource.setrlimit(
            resource.RLIMIT_NOFILE, (len(os.listdir("/dev/fd")) + 8, hard)
        )
        while True:
            try:
                client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except OSError:
                break
            clients.append(client)
            client.connect((framework.test_host.decode(), port))
        loop.schedule(10, loop.stop)
        loop.run()
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        for client in clients:
            client.close()
        for conn in accepted:
            conn.close()
        server.shutdown()

    check.assertEqual(len(accepted), 1)
    check.assertEqual([count for _, count in attempts], [0, 0, 0])
    # the first two attempts failed; the server must have waited between attempts
    check.assertGreaterEqual(attempts[1][0] - attempts[0][0], backoff - 0.05)
    check.assertGreaterEqual(attempts[2][0] - attempts[1][0], backoff - 0.05)


#   def test_pause(self):
#   def test_shutdown(self):

if __name__ == "__main__":
    unittest.main()
