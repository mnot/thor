#!/usr/bin/env python

import errno
import socket
import sys
import threading
import unittest

from thor import loop
from thor.dns import lookup


class TestDns(unittest.TestCase):
    def setUp(self):
        self.loop = loop.make()
        self.loop.schedule(5, self.timeout)
        self.timeout_hit = False
        self.outstanding = 0
        self.run_threads = []

    def timeout(self):
        self.timeout_hit = True
        self.loop.stop()

    def expect(self, n):
        "Expect n callbacks before stopping the loop."
        self.outstanding = n

    def done(self):
        "Record one callback; stop the loop once all are in."
        self.run_threads.append(threading.current_thread())
        self.outstanding -= 1
        if self.outstanding <= 0:
            self.loop.stop()

    def check_success(self, results):
        self.assertTrue(type(results) == list and len(results) > 0, results)
        self.done()

    def check_gai_error(self, results):
        self.assertTrue(isinstance(results, socket.gaierror), results)
        self.done()

    def assert_ran_in_loop_thread(self):
        # Callbacks must run in the loop (main) thread, never a DNS worker.
        self.assertFalse(self.timeout_hit)
        for thread in self.run_threads:
            self.assertIs(thread, threading.main_thread())

    def test_basic(self):
        self.expect(1)
        lookup(self.loop, b"www.google.com", 80, socket.SOCK_STREAM, self.check_success)
        self.loop.run()
        self.assert_ran_in_loop_thread()

    def test_lots(self):
        self.expect(4)
        lookup(self.loop, b"www.example.com", 80, socket.SOCK_STREAM, self.check_success)
        lookup(self.loop, b"www.ietf.org", 443, socket.SOCK_STREAM, self.check_success)
        lookup(self.loop, b"www.abc.net.au", 80, socket.SOCK_STREAM, self.check_success)
        lookup(self.loop, b"www.mnot.net", 443, socket.SOCK_STREAM, self.check_success)
        self.loop.run()
        self.assert_ran_in_loop_thread()

    def test_gai(self):
        self.expect(2)
        # .invalid is reserved by RFC 6761 and guaranteed never to resolve;
        # real-looking bogus TLDs (.foo, .bar) are now delegated gTLDs.
        lookup(self.loop, b"nonexistent.invalid", 23, socket.SOCK_STREAM, self.check_gai_error)
        lookup(self.loop, b"alsobogus.invalid", 23, socket.SOCK_DGRAM, self.check_gai_error)
        self.loop.run()
        self.assert_ran_in_loop_thread()


if __name__ == "__main__":
    unittest.main()
