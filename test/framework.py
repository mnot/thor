#!/usr/bin/env python

"""
Framework for testing clients and servers, moving one of them into
a separate thread.
"""

try:
    import SocketServer
except ImportError:
    import socketserver as SocketServer

import os
import socket
import sys
import threading
import time
import unittest

import thor
from thor.http.common import HttpMessageHandler, States

test_host = b"127.0.0.1"
tls_host = test_host
tls_port = 24443
timeout_host = b"www.mnot.net"
timeout_port = 31000
refuse_host = test_host
refuse_port = 45000
udp_port = 48500
timeout = 10


class ClientServerTestCase(unittest.TestCase):
    def setUp(self):
        self.loop = thor.loop.make()
        self.loop.debug = True
        self.timeout_hit = False

    def tearDown(self):
        if self.loop.running:
            sys.stdout.write("WARNING: loop still running at end of test.")
            self.loop.stop()

    @staticmethod
    def move_to_thread(target, args=None):
        t = threading.Thread(target=target, args=args or [])
        t.daemon = True
        t.start()

    def go(self, server_sides, client_sides, timeout=timeout):
        """
        Start the server(s), handling connections with server_side (handler),
        and then run the client(s), calling client_side (client).

        One of the handlers MUST stop the loop before the timeout, which
        is considered failure.
        """

        assert len(server_sides) == len(client_sides)
        steps = []
        for server_side in server_sides:
            steps.append(self.create_server(server_side))
        time.sleep(1)
        i = 0
        for client_side in client_sides:
            self.create_client(test_host, steps[i][1], client_side)
            i += 1

        def do_timeout():
            self.loop.stop()
            self.timeout_hit = True

        self.loop.schedule(timeout, do_timeout)
        try:
            self.loop.run()
        finally:
            [step[0]() for step in steps]
        self.assertEqual(self.timeout_hit, False)

    def get_port(self):
        sock = socket.socket()
        sock.bind(("", 0))
        _, port = sock.getsockname()
        return port

    def create_server(self, server_side):
        raise NotImplementedError

    def create_client(self, host, port, client_side):
        raise NotImplementedError


class DummyHttpParser(HttpMessageHandler):
    default_state = States.WAITING

    def __init__(self, *args, **kw):
        HttpMessageHandler.__init__(self, *args, **kw)
        self.test_top_line = None
        self.test_hdrs = None
        self.test_body = b""
        self.test_trailers = None
        self.test_err = None
        self.test_states = []

    def input_start(
        self, top_line, hdr_tuples, conn_tokens, transfer_codes, content_length
    ):
        self.test_states.append("START")
        self.test_top_line = top_line
        self.test_hdrs = hdr_tuples
        return True, True

    def input_body(self, chunk):
        self.test_states.append("BODY")
        self.test_body += chunk

    def input_end(self, trailers):
        self.test_states.append("END")
        self.test_trailers = trailers

    def input_error(self, err):
        self.test_states.append("ERROR")
        self.test_err = err
        return False  # never recover.

    def check(self, asserter, expected):
        """
        Check the parsed message against expected attributes and
        assert using asserter as necessary.
        """
        aE = asserter.assertEqual
        aE(expected.get("top_line", self.test_top_line), self.test_top_line)
        aE(expected.get("hdrs", self.test_hdrs), self.test_hdrs)
        aE(expected.get("body", self.test_body), self.test_body)
        aE(expected.get("trailers", self.test_trailers), self.test_trailers)
        aE(expected.get("error", self.test_err), self.test_err)
        aE(expected.get("states", self.test_states), self.test_states)


class LittleRequestHandler(SocketServer.BaseRequestHandler):
    def handle(self):
        # Echo back to the client
        data = self.request.recv(1024)
        self.request.send(data)
        self.request.close()


def make_fifo(filename):
    try:
        os.unlink(filename)
    except OSError:
        pass  # wasn't there
    try:
        os.mkfifo(filename)
    except OSError as e:
        print(f"Failed to create FIFO: {e}")
    else:
        r = os.open(filename, os.O_RDONLY | os.O_NONBLOCK)
        w = os.open(filename, os.O_WRONLY | os.O_NONBLOCK)
        return r, w
