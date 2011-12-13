#!/usr/bin/env python

import socket
import sys
import time
import unittest

import framework

import thor
from thor.events import on

class TestTcpServer(framework.ClientServerTestCase):

    def create_server(self, host, port, server_side):
        server = thor.TcpServer(host, port, loop=self.loop)
        server.conn_count = 0
        def run_server(conn):
            server.conn_count += 1
            server_side(conn)
        server.on('connect', run_server)
        @on(self.loop)
        def stop():
            self.assertTrue(server.conn_count > 0)
            server.shutdown()
                    
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
                self.assertEqual(chunk, "foo!")
                self.server_recv += 1
            server_conn.on('data', check_data)
            server_conn.pause(False)
            server_conn.write("bar!")
            
        def client_side(client_conn):
            sent = client_conn.send('foo!')

        self.go([server_side], [client_side])
        self.assertTrue(self.server_recv > 0, self.server_recv)
 
# TODO:
#   def test_pause(self):
#   def test_shutdown(self):

if __name__ == '__main__':
    unittest.main()
