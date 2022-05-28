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

    def timeout(self):
        self.timeout_hit = True
        self.loop.stop()

    def check_success(self, results):
        self.assertTrue(type(results) == list and len(results) > 0, results)

    def check_gai_error(self, results):
        self.assertTrue(isinstance(results, socket.gaierror), results)

    def test_basic(self):
        lookup(b"www.google.com", 80, socket.SOCK_STREAM, self.check_success)
        self.loop.run()

    def test_lots(self):
        lookup(b"www.google.com", 443, socket.SOCK_STREAM, self.check_success)
        lookup(b"www.facebook.com", 80, socket.SOCK_STREAM, self.check_success)
        lookup(b"www.example.com", 80, socket.SOCK_STREAM, self.check_success)
        lookup(b"www.ietf.org", 443, socket.SOCK_STREAM, self.check_success)
        lookup(b"www.github.com", 443, socket.SOCK_STREAM, self.check_success)
        lookup(b"www.twitter.com", 443, socket.SOCK_STREAM, self.check_success)
        lookup(b"www.abc.net.au", 80, socket.SOCK_STREAM, self.check_success)
        lookup(b"www.mnot.net", 443, socket.SOCK_STREAM, self.check_success)
        lookup(b"www.eff.org", 443, socket.SOCK_STREAM, self.check_success)
        lookup(b"www.aclu.org", 443, socket.SOCK_STREAM, self.check_success)
        self.loop.run()

    def test_gai(self):
        lookup(b"foo.foo", 23, socket.SOCK_STREAM, self.check_gai_error)
        lookup(b"bar.bar", 23, socket.SOCK_DGRAM, self.check_gai_error)


if __name__ == "__main__":
    unittest.main()
