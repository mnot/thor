#!/usr/bin/env python

import SocketServer
import sys
import time
import unittest

import framework
from framework import test_host, test_port

import thor
from thor.events import on

        
class LittleServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    allow_reuse_address = True


class TestHttpClient(framework.ClientServerTestCase):

    def create_server(self, test_host, test_port, server_side):
        class LittleRequestHandler(SocketServer.BaseRequestHandler):
            handle = server_side
        server = LittleServer(
            (framework.test_host, framework.test_port), 
            LittleRequestHandler
        )
        self.move_to_thread(target=server.serve_forever)

        @on(self.loop)
        def stop():
            server.shutdown()
            server.server_close()

    def create_client(self, host, port, client_side):
        client = thor.HttpClient(loop=self.loop)
        client.connect_timeout = 1
        client_side(client)

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

        @on(exchange)
        def response_start(status, phrase, headers):
            self.assertEqual(
                exchange.res_version, 
                expected.get('version', exchange.res_version)
            )
            self.assertEqual(status, expected.get('status', status))
            self.assertEqual(phrase, expected.get('phrase', phrase))

        exchange.tmp_res_body = ""
        @on(exchange)
        def response_body(chunk):
            exchange.tmp_res_body += chunk

        @on(exchange)
        def response_done(trailers):
            exchange.test_happened = True
            self.assertEqual(
                exchange.tmp_res_body, 
                expected.get('body', exchange.tmp_res_body)
            )
            
        @on(self.loop)
        def stop():
            self.assertTrue(exchange.test_happened)



    def test_basic(self):
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': "1.1",
                'status': "200",
                'phrase': 'OK',
                'body': "12345"
            })
            
            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = "http://%s:%s/" % (test_host, test_port)
            exchange.request_start(
                "GET", req_uri, []
            )
            exchange.request_done([])
                
        def server_side(conn):
            conn.request.send("""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 5
Connection: close

12345""")
            conn.request.close()
        self.go([server_side], [client_side])


    def test_chunked_response(self):
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': "1.1",
                'status': "200",
                'phrase': 'OK',
                'body': "12345"
            })
            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = "http://%s:%s/" % (test_host, test_port)
            exchange.request_start(
                "GET", req_uri, []
            )
            exchange.request_done([])
                
        def server_side(conn):
            conn.request.send("""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

5\r
12345\r
0\r
\r
""")
            conn.request.close()
        self.go([server_side], [client_side])


    def test_chunked_request(self):
        req_body = "54321"
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': "1.1",
                'status': "200",
                'phrase': 'OK',
                'body': "12345"
            })
            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = "http://%s:%s/" % (test_host, test_port)
            exchange.request_start(
                "POST", req_uri, []
            )
            exchange.request_body(req_body)
            exchange.request_body(req_body)
            exchange.request_done([])
                
        def server_side(conn):
            conn.request.send("""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

5\r
12345\r
0\r
\r
""")
            # TODO: check server-side recv
            conn.request.close()
        self.go([server_side], [client_side])


    def test_multiconn(self):
        self.test_req_count = 0
        def check_done(trailers):
            self.test_req_count += 1
            if self.test_req_count == 2:
                self.loop.stop()
        
        def client_side(client):
            exchange1 = client.exchange()
            self.check_exchange(exchange1, {
                'version': "1.1",
                'status': "200",
                'phrase': 'OK',
                'body': "12345"
            })

            exchange1.on('response_done', check_done)
            exchange2 = client.exchange()
            self.check_exchange(exchange2, {
                'version': "1.1",
                'status': "200",
                'phrase': 'OK',
                'body': "12345"
            })
            exchange2.on('response_done', check_done)

            req_uri = "http://%s:%s/" % (test_host, test_port)
            exchange1.request_start(
                "GET", req_uri, []
            )
            exchange2.request_start(
                "GET", req_uri, []
            )
            exchange1.request_done([])
            exchange2.request_done([])                
                
        def server_side(conn):
            conn.request.send("""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 5
Connection: close

12345""")
            time.sleep(1)
            conn.request.close()
        self.go([server_side], [client_side])

        
    def test_conn_refuse_err(self):
        def client_side(client):
            exchange = client.exchange()
            @on(exchange)
            def error(err_msg):
                self.assertEqual(
                    err_msg.__class__, thor.http.error.ConnectError
                )
                self.loop.stop()

            req_uri = "http://%s:%s/" % (test_host, test_port)
            exchange.request_start(
                "GET", req_uri, []
            )
            exchange.request_done([])
        self.go([], [client_side])


    # FIXME: works because dns is currently blocking
    def test_conn_noname_err(self):
        client = thor.HttpClient(loop=self.loop)
        exchange = client.exchange()
        @on(exchange)
        def error(err_msg):
            self.assertEqual(
                err_msg.__class__, thor.http.error.ConnectError
            )
            self.loop.stop()

        req_uri = "http://foo.bar/"
        exchange.request_start(
            "GET", req_uri, []
        )
        exchange.request_done([])

        
    def test_url_err(self):
        client = thor.HttpClient(loop=self.loop)
        exchange = client.exchange()
        @on(exchange)
        def error(err_msg):
            self.assertEqual(
                err_msg.__class__, thor.http.error.UrlError
            )
            self.loop.stop()

        req_uri = "foo://%s:%s/" % (test_host, test_port)
        exchange.request_start(
            "GET", req_uri, []
        )
        exchange.request_done([])


    def test_url_port_err(self):
        client = thor.HttpClient(loop=self.loop)
        exchange = client.exchange()
        @on(exchange)
        def error(err_msg):
            self.assertEqual(
                err_msg.__class__, thor.http.error.UrlError
            )
            self.loop.stop()

        req_uri = "http://%s:ABC123/" % (test_host)
        exchange.request_start(
            "GET", req_uri, []
        )
        exchange.request_done([])


    def test_http_version_err(self):
        def client_side(client):
            exchange = client.exchange()
            @on(exchange)
            def error(err_msg):
                self.assertEqual(
                    err_msg.__class__, thor.http.error.HttpVersionError
                )
                self.loop.stop()

            req_uri = "http://%s:%s/" % (test_host, test_port)
            exchange.request_start(
                "GET", req_uri, []
            )
            exchange.request_done([])
                
        def server_side(conn):
            conn.request.send("""\
HTTP/2.5 200 OK
Content-Type: text/plain
Content-Length: 5
Connection: close

12345""")
            conn.request.close()
        self.go([server_side], [client_side])


    def test_http_protoname_err(self):
        def client_side(client):
            exchange = client.exchange()
            @on(exchange)
            def error(err_msg):
                self.assertEqual(
                    err_msg.__class__, thor.http.error.HttpVersionError
                )
                self.loop.stop()

            req_uri = "http://%s:%s/" % (test_host, test_port)
            exchange.request_start(
                "GET", req_uri, []
            )
            exchange.request_done([])
                
        def server_side(conn):
            conn.request.send("""\
ICY/1.1 200 OK
Content-Type: text/plain
Content-Length: 5
Connection: close

12345""")
            conn.request.close()
        self.go([server_side], [client_side])

    def test_close_in_body(self):
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': "1.1",
                'status': "200",
                'phrase': 'OK',
            })
            
            @on(exchange)
            def error(err_msg):
                self.assertEqual(
                    err_msg.__class__, 
                    thor.http.error.ConnectError
                )
                self.loop.stop()

            req_uri = "http://%s:%s/" % (test_host, test_port)
            exchange.request_start(
                "GET", req_uri, []
            )
            exchange.request_done([])
                
        def server_side(conn):
            conn.request.send("""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 15
Connection: close

12345""")
            conn.request.close()
        self.go([server_side], [client_side])
        

    def test_conn_reuse(self):
        self.conn_checked = False
        def client_side(client):
            req_uri = "http://%s:%s/" % (test_host, test_port)
            exchange1 = client.exchange()
            self.check_exchange(exchange1, {
                'version': "1.1",
                'status': "200",
                'phrase': 'OK',
                'body': "12345"
            })
            exchange2 = client.exchange()
            self.check_exchange(exchange2, {
                'version': "1.1",
                'status': "404",
                'phrase': 'Not Found',
                'body': "54321"
            })

            @on(exchange1)
            def response_start(*args):
                self.conn_id = id(exchange1.tcp_conn)

            @on(exchange1)
            def response_done(trailers):
                exchange2.request_start("GET", req_uri, [])
                exchange2.request_done([])

            @on(exchange2)
            def response_start(*args):
                self.assertEqual(self.conn_id, id(exchange2.tcp_conn))
                self.conn_checked = True

            @on(exchange2)
            def response_done(trailers):
                self.loop.stop()

            exchange1.request_start("GET", req_uri, [])
            exchange1.request_done([])
                
        def server_side(conn):
            conn.request.sendall("""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 5

12345""")
            time.sleep(1)
            conn.request.sendall("""\
HTTP/1.1 404 Not Found
Content-Type: text/plain
Content-Length: 5
Connection: close

54321""")
            conn.request.close()
        self.go([server_side], [client_side])
        self.assertTrue(self.conn_checked)


    def test_conn_succeed_then_err(self):
        self.conn_checked = False
        def client_side(client):
            req_uri = "http://%s:%s/" % (test_host, test_port)
            exchange1 = client.exchange()
            self.check_exchange(exchange1, {
                'version': "1.1",
                'status': "200",
                'phrase': 'OK',
                'body': "12345"
            })
            exchange2 = client.exchange()

            @on(exchange1)
            def response_start(*args):
                self.conn_id = id(exchange1.tcp_conn)

            @on(exchange1)
            def response_done(trailers):
                exchange2.request_start("GET", req_uri, [])
                exchange2.request_done([])

            @on(exchange2)
            def error(err_msg):
                self.conn_checked = True
                self.assertEqual(
                    err_msg.__class__, thor.http.error.HttpVersionError
                )
                self.loop.stop()

            exchange1.request_start("GET", req_uri, [])
            exchange1.request_done([])
                
        def server_side(conn):
            conn.request.sendall("""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 5

12345""")
            time.sleep(1)
            conn.request.sendall("""\
HTTP/9.1 404 Not Found
Content-Type: text/plain
Content-Length: 5
Connection: close

54321""")
            conn.request.close()
        self.go([server_side], [client_side])
        self.assertTrue(self.conn_checked)


    def test_HEAD(self):
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': "1.1",
                'status': "200",
                'phrase': 'OK',
                'body': ""
            })
            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = "http://%s:%s/" % (test_host, test_port)
            exchange.request_start(
                "HEAD", req_uri, []
            )
            exchange.request_done([])
                
        def server_side(conn):
            conn.request.send("""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 5
Connection: close

""")
            time.sleep(1)
            conn.request.close()
        self.go([server_side], [client_side])
        

    def test_req_retry(self):
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': "1.1",
                'status': "200",
                'phrase': 'OK',
                'body': "12345"
            })
            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = "http://%s:%s" % (test_host, test_port)
            exchange.request_start(
                "OPTIONS", req_uri, []
            )
            exchange.request_done([])
                
        self.conn_num = 0
        def server_side(conn):
            self.conn_num += 1
            if self.conn_num > 1:
                conn.request.send("""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 5
Connection: close

12345""")
            conn.request.close()
        self.go([server_side], [client_side])   


    def test_req_retry_fail(self):
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': "1.1",
                'status': "200",
                'phrase': 'OK',
                'body': "12345"
            })
            
            @on(exchange)
            def error(err_msg):
                self.assertEqual(
                    err_msg.__class__, thor.http.error.ConnectError
                )
                self.loop.stop()
                
            req_uri = "http://%s:%s" % (test_host, test_port)
            exchange.request_start(
                "OPTIONS", req_uri, []
            )
            exchange.request_done([])
                
        self.conn_num = 0
        def server_side(conn):
            self.conn_num += 1
            if self.conn_num > 3:
                conn.request.send("""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 5
Connection: close

12345""")
            conn.request.close()
        self.go([server_side], [client_side])   


# TODO:
#    def test_req_body(self):
#    def test_req_body_dont_retry(self):
#    def test_req_body_close_on_err(self):
#    def test_pipeline(self):
#    def test_malformed_hdr(self):
#    def test_unexpected_res(self):
#    def test_pause(self):
#    def test_options_star(self):
#    def test_idle_timeout(self):
#    def test_idle_timeout_reuse(self):
#    def test_alternate_tcp_client(self):

if __name__ == '__main__':
    unittest.main()

