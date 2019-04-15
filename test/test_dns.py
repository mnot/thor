#!/usr/bin/env python

import errno
import socket
import sys
import threading
import unittest


from thor import loop
from thor.dns import lookup

test_host = "127.0.0.1"
test_port = 9002


class TestDns(unittest.TestCase):

    def setUp(self):
        self.loop = loop.make()
        self.loop.schedule(5, self.timeout)
        self.timeout_hit = False

    def timeout(self):
        self.timeout_hit = True
        self.loop.stop()

    def check_success(self, results):
        self.assertTrue(type(results) is str, results)

    def test_basic(self):
        lookup(b'www.google.com', self.check_success)
        self.loop.run()

    def test_lots(self):
        lookup(b'www.google.com', self.check_success)
        lookup(b'www.facebook.com', self.check_success)
        lookup(b'www.example.com', self.check_success)
        lookup(b'www.ietf.org', self.check_success)
        lookup(b'www.github.com', self.check_success)
        lookup(b'www.twitter.com', self.check_success)
        lookup(b'www.abc.net.au', self.check_success)
        lookup(b'www.mnot.net', self.check_success)
        lookup(b'www.eff.org', self.check_success)
        lookup(b'www.aclu.org', self.check_success)
        self.loop.run()

if __name__ == '__main__':
    unittest.main()
