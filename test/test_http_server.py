##!/usr/bin/env python

import socket
import sys
import time
import unittest

import framework

import thor
from thor.events import on

class TestHttpServer(framework.ClientServerTestCase):
            
    def create_server(self, test_host, test_port, server_side):
        server = thor.HttpServer(test_host, test_port, loop=self.loop)
        server_side(server)
        @on(self.loop)
        def stop():
            server.shutdown()

    def create_client(self, test_host, test_port, client_side):
        def run_client(client_side1):
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect((test_host, test_port))
            client_side1(client)
            client.close()
        self.move_to_thread(target=run_client, args=(client_side,))

    def check_exchange(self, exchange, expected):
        """
        Given an exchange, check that the status, phrase and body are as
        expected, and verify that it actually happened.
        """
        exchange.test_happened = False
        
        @on(exchange)
        def error(err_msg):
            exchange.test_happened = True
            self.assertEqual(err_msg, expected.get('error', err_msg))
            self.loop.stop()

        @on(exchange)
        def request_start(method, uri, headers):
            self.assertEqual(method, expected.get('method', method))
            self.assertEqual(uri, expected.get('phrase', uri))

        exchange.tmp_req_body = ""
        @on(exchange)
        def request_body(chunk):
            exchange.tmp_req_body += chunk

        @on(exchange)
        def request_done(trailers):
            exchange.test_happened = True
            self.assertEqual(
                trailers, 
                expected.get('req_trailers', trailers)
            )
            self.assertEqual(
                exchange.tmp_req_body, 
                expected.get('body', exchange.tmp_req_body)
            )
            self.loop.stop()
            
        @on(self.loop)
        def stop():
            self.assertTrue(exchange.test_happened)


    def test_basic(self):
        def server_side(server):
            def check(exchange):
                self.check_exchange(exchange, {
                    'method': 'GET',
                    'uri': '/'                    
                })
            server.on('exchange', check)
            
        def client_side(client_conn):
            client_conn.sendall("""\
GET / HTTP/1.1
Host: %s:%s

""" % (framework.test_host, framework.test_port))
            time.sleep(1)
            client_conn.close()
        self.go([server_side], [client_side])


    def test_extraline(self):
        def server_side(server):
            def check(exchange):
                self.check_exchange(exchange, {
                    'method': 'GET',
                    'uri': '/'
                })
            server.on('exchange', check)
            
        def client_side(client_conn):
            client_conn.sendall("""\
            
GET / HTTP/1.1
Host: %s:%s

""" % (framework.test_host, framework.test_port))
            time.sleep(1)
            client_conn.close()
        self.go([server_side], [client_side])


    def test_post(self):
        def server_side(server):
            def check(exchange):
                self.check_exchange(exchange, {
                    'method': 'POST',
                    'uri': '/foo'                    
                })
            server.on('exchange', check)
            
        def client_side(client_conn):
            client_conn.sendall("""\
POST / HTTP/1.1
Host: %s:%s
Content-Type: text/plain
Content-Length: 5

12345""" % (framework.test_host, framework.test_port))
            time.sleep(1)
            client_conn.close()
        self.go([server_side], [client_side])
        

    def test_post_extra_crlf(self):
        def server_side(server):
            def check(exchange):
                self.check_exchange(exchange, {
                    'method': 'POST',
                    'uri': '/foo'                    
                })
            server.on('exchange', check)
            
        def client_side(client_conn):
            client_conn.sendall("""\
POST / HTTP/1.1
Host: %s:%s
Content-Type: text/plain
Content-Length: 5

12345
""" % (framework.test_host, framework.test_port))
            time.sleep(1)
            client_conn.close()
        self.go([server_side], [client_side])        


#    def test_pipeline(self):
#        def server_side(server):
#            server.ex_count = 0
#            def check(exchange):
#                self.check_exchange(exchange, {
#                    'method': 'GET',
#                    'uri': '/'
#                })
#                server.ex_count += 1
#            server.on('exchange', check)
#            @on(self.loop)
#            def stop():
#                self.assertEqual(server.ex_count, 2)
#            
#        def client_side(client_conn):
#            client_conn.sendall("""\
#GET / HTTP/1.1
#Host: %s:%s
#
#GET / HTTP/1.1
#Host: %s:%s
#
#""" % (
#    framework.test_host, framework.test_port,
#    framework.test_host, framework.test_port
#))
#            time.sleep(1)
#            client_conn.close()
#        self.go([server_side], [client_side])



#    def test_conn_close(self):
#    def test_req_nobody(self):
#    def test_res_nobody(self):
#    def test_bad_http_version(self):
#    def test_pause(self):
#    def test_extra_crlf_after_post(self):
#    def test_absolute_uri(self): # ignore host header
#    def test_host_header(self):#
#    def test_unknown_transfercode(self): # should be 501
#    def test_shutdown(self):
#    def test_alternate_tcp_server(self):


if __name__ == '__main__':
    unittest.main()

