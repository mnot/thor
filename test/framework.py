#!/usr/bin/env python

"""
Framework for testing clients and servers, moving one of them into 
a separate thread.
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, "..")
import thor

test_host = "127.0.0.1"
test_port = 8001


class ClientServerTestCase(unittest.TestCase):
    
    def setUp(self):
        self.loop = thor.loop.make()
        self.timeout_hit = False

    def tearDown(self):
        if self.loop.running:
            sys.stdout.write("WARNING: loop still running at end of test.")
            self.loop.stop()

    def move_to_thread(self, target, args=None):
        t = threading.Thread(target=target, args=args or [])
        t.setDaemon(True)
        t.start()
            
    def go(self, server_sides, client_sides, timeout=5):
        """
        Start the server(s), handling connections with server_side (handler),
        and then run the client(s), calling client_side (client).
        
        One of the handlers MUST stop the loop before the timeout, which
        is considered failure.
        """

        for server_side in server_sides:
            offset = 0
            if hasattr(server_side, "port_offset"):
                offset = server_side.port_offset
            self.create_server(test_host, test_port + offset, server_side)
        
        for client_side in client_sides:
            self.create_client(test_host, test_port, client_side)
            
        def do_timeout():
            self.loop.stop()
            self.timeout_hit = True
        self.loop.schedule(timeout, do_timeout)
        self.loop.run()
        self.assertEqual(self.timeout_hit, False)

    def create_server(self, host, port, server_side):
        raise NotImplementedError
        
    def create_client(self, host, port, client_side):
        raise NotImplementedError
        

class DummyHttpParser(thor.http.common.HttpMessageHandler):
    def __init__(self, *args, **kw):
        thor.http.common.HttpMessageHandler.__init__(self, *args, **kw)
        self.test_top_line = None
        self.test_hdrs = None
        self.test_body = ""
        self.test_err = None
        self.test_states = []
    
    def input_start(self, top_line, hdr_tuples, conn_tokens, 
                     transfer_codes, content_length):
        self.test_states.append("START")
        self.test_top_line = top_line
        self.test_hdrs = hdr_tuples
        return bool
        
    def input_body(self, chunk):
        self.test_states.append("BODY")
        self.test_body += chunk

    def input_end(self, trailers):
        self.test_states.append("END")
        self.test_trailers = trailers

    def input_error(self, err):
        self.test_states.append("ERROR")
        self.test_err = err
        return False # never recover.


def make_fifo(filename):
    try:
        os.mkfifo(filename)
    except OSError, e:
        print "Failed to create FIFO: %s" % e
    else:
        fifo = open(filename, 'w+')
        return fifo
