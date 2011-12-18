#!/usr/bin/env python

import errno
import socket
import sys
import threading
import unittest


from thor import loop
from thor.udp import UdpEndpoint

test_host = "127.0.0.1"
test_port = 9002


class TestUdpEndpoint(unittest.TestCase):

    def setUp(self):
        self.loop = loop.make()
        self.ep1 = UdpEndpoint(self.loop)
        self.ep1.bind(test_host, test_port)
        self.ep1.on('datagram', self.input)
        self.ep1.pause(False)
        self.ep2 = UdpEndpoint()
        self.loop.schedule(5, self.timeout)
        self.timeout_hit = False
        self.datagrams = []

    def tearDown(self):
        self.ep1.shutdown()
        
    def timeout(self):
        self.timeout_hit = True
        self.loop.stop()

    def input(self, data, host, port):
        self.datagrams.append((data, host, port))

    def output(self, msg):
        self.ep2.send(msg, test_host, test_port)
        
    def test_basic(self):
        self.loop.schedule(1, self.output, 'foo!')
        self.loop.schedule(2, self.output, 'bar!')
        
        def check():
            self.assertEqual(self.datagrams[0][0], 'foo!')
            self.assertEqual(self.datagrams[1][0], 'bar!')
            self.loop.stop()
        self.loop.schedule(3, check)
        self.loop.run()
        
    def test_bigdata(self):
        self.loop.schedule(1, self.output, 'a' * 100)
        self.loop.schedule(2, self.output, 'b' * 1000)
        self.loop.schedule(3, self.output, 'c' * self.ep1.max_dgram)
        
        def check():
            self.assertEqual(self.datagrams[0][0], 'a' * 100)
            self.assertEqual(self.datagrams[1][0], 'b' * 1000)
            # we only check the first 1000 characters because, well, 
            # it's lossy.
            self.assertEqual(self.datagrams[2][0][:1000], 'c' * 1000)
            self.loop.stop()
        self.loop.schedule(4, check)
        self.loop.run()

#   def test_pause(self):


if __name__ == '__main__':
    unittest.main()