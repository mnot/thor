#!/usr/bin/env python
# coding: utf-8

try:
    import SocketServer
except ImportError:
    import socketserver as SocketServer

import sys
import time
import unittest

import framework
test_host = framework.test_host
test_port = framework.test_port


import thor
from thor.events import on
from thor.http import HttpClient

thor.loop.debug = True

class LittleServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    allow_reuse_address = True


class TestHttpClient(framework.ClientServerTestCase):

    def create_server(self, test_host, test_port, server_side):
        class LittleRequestHandler(SocketServer.BaseRequestHandler):
            handle = server_side
        server = LittleServer((framework.test_host, framework.test_port), LittleRequestHandler)
        self.move_to_thread(target=server.serve_forever)

        @on(self.loop)
        def stop():
            server.shutdown()
            server.server_close()

    def create_client(self, host, port, client_side):
        client = HttpClient(loop=self.loop)
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
            self.assertEqual(exchange.res_version, expected.get('version', exchange.res_version))
            self.assertEqual(status, expected.get('status', status))
            self.assertEqual(phrase, expected.get('phrase', phrase))

        exchange.tmp_res_body = b""
        @on(exchange)
        def response_body(chunk):
            exchange.tmp_res_body += chunk

        @on(exchange)
        def response_done(trailers):
            exchange.test_happened = True
            self.assertEqual(exchange.tmp_res_body, expected.get('body', exchange.tmp_res_body))

        @on(self.loop)
        def stop():
            self.assertTrue(exchange.test_happened, expected)



    def test_basic(self):
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': b"1.1",
                'status': b"200",
                'phrase': b'OK',
                'body': b"12345"
            })

            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/basic" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
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
                'version': b"1.1",
                'status': b"200",
                'phrase': b'OK',
                'body': b"12345"
            })
            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/chunked_response" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
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
        req_body = b"54321"
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': b"1.1",
                'status': b"200",
                'phrase': b'OK',
                'body': b"12345"
            })
            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/chunked_request" % (test_host, test_port)
            exchange.request_start(b"POST", req_uri, [])
            exchange.request_body(req_body)
            exchange.request_body(req_body)
            exchange.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
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


    def test_1xx(self):
        req_body = b"54321"
        def client_side(client):
            self.nonfinal_seen = False
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': b"1.1",
                'status': b"200",
                'phrase': b'OK',
                'body': b"12345"
            })
            @on(exchange)
            def response_nonfinal(status_code, status_phrase, headers):
                self.nonfinal_seen = True
            @on(exchange)
            def response_done(trailers):
                self.assertTrue(self.nonfinal_seen)
                self.loop.stop()

            req_uri = b"http://%s:%i/chunked_request" % (test_host, test_port)
            exchange.request_start(b"POST", req_uri, [])
            exchange.request_body(req_body)
            exchange.request_body(req_body)
            exchange.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
HTTP/1.1 110 Whatever
This: that

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
                'version': b"1.1",
                'status': b"200",
                'phrase': b'OK',
                'body': b"12345"
            })

            exchange1.on('response_done', check_done)
            exchange2 = client.exchange()
            self.check_exchange(exchange2, {
                'version': b"1.1",
                'status': b"200",
                'phrase': b'OK',
                'body': b"12345"
            })
            exchange2.on('response_done', check_done)

            req_uri = b"http://%s:%i/multiconn" % (test_host, test_port)
            exchange1.request_start(b"GET", req_uri, [])
            exchange2.request_start(b"GET", req_uri, [])
            exchange1.request_done([])
            exchange2.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
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
                self.assertEqual(err_msg.__class__, thor.http.error.ConnectError)
                self.loop.stop()

            req_uri = b"http://%s:%i/conn_refuse_err" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])
        self.go([], [client_side])


    # FIXME: works because dns is currently blocking
    def test_conn_noname_err(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()
        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.ConnectError)
            self.loop.stop()

        req_uri = b"http://foo.bar/conn_noname_err"
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])


    def test_url_err(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()
        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"foo://%s:%i/url_err" % (test_host, test_port)
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])


    def test_url_port_err(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()
        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"http://%s:ABC123/url_port_err" % (test_host)
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])


    def test_url_port_range(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()
        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"http://%s:80000/" % (test_host)
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])


    def test_http_version_err(self):
        def client_side(client):
            exchange = client.exchange()
            @on(exchange)
            def error(err_msg):
                self.assertEqual(err_msg.__class__, thor.http.error.HttpVersionError)
                self.loop.stop()

            req_uri = b"http://%s:%i/http_version_err" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
HTTP/2.5 200 OK
Content-Type: text/plain
Content-Length: 5
Connection: close

12345""")
            conn.request.close()
        self.go([server_side], [client_side])


    def test_http_start_encoding(self):
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': b"1.1",
                'status': b"200",
                'phrase': u'ÖK'.encode('utf-8'),
                'body': b"12345"
            })
            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/http_start_encoding" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
HTTP/1.1 200 \xc3\x96K
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
                self.assertEqual(err_msg.__class__, thor.http.error.HttpVersionError)
                self.loop.stop()

            req_uri = b"http://%s:%i/protoname_err" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
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
                'version': b"1.1",
                'status': b"200",
                'phrase': b'OK',
            })

            @on(exchange)
            def error(err_msg):
                self.assertEqual(err_msg.__class__, thor.http.error.ConnectError)
                self.loop.stop()

            req_uri = b"http://%s:%i/close_in_body" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
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
            req_uri = b"http://%s:%i/conn_reuse" % (test_host, test_port)
            exchange1 = client.exchange()
            self.check_exchange(exchange1, {
                'version': b"1.1",
                'status': b"200",
                'phrase': b'OK',
                'body': b"12345"
            })

            @on(exchange1)
            def response_start(*args):
                self.conn_id = id(exchange1.tcp_conn)

            @on(exchange1)
            def response_done(trailers):
                exchange2 = client.exchange()
                self.check_exchange(exchange2, {
                    'version': b"1.1",
                    'status': b"404",
                    'phrase': b'Not Found',
                    'body': b"54321"
                })
                def start2():
                    exchange2.request_start(b"GET", req_uri, [])
                    exchange2.request_done([])
                self.loop.schedule(1, start2)

                @on(exchange2)
                def response_start(*args):
                    self.assertEqual(self.conn_id, id(exchange2.tcp_conn))
                    self.conn_checked = True

                @on(exchange2)
                def response_done(trailers):
                    self.loop.stop()

            exchange1.request_start(b"GET", req_uri, [])
            exchange1.request_done([])

        def server_side(conn):
            conn.request.sendall(b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 5

12345""")
            time.sleep(2)
            conn.request.sendall(b"""\
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
            req_uri = b"http://%s:%i/succeed_then_err" % (test_host, test_port)
            exchange1 = client.exchange()
            self.check_exchange(exchange1, {
                'version': b"1.1",
                'status': b"200",
                'phrase': b'OK',
                'body': b"12345"
            })
            exchange2 = client.exchange()

            @on(exchange1)
            def response_start(*args):
                self.conn_id = id(exchange1.tcp_conn)

            @on(exchange1)
            def response_done(trailers):
                def start2():
                    exchange2.request_start(b"GET", req_uri, [])
                    exchange2.request_done([])
                self.loop.schedule(1, start2)

            @on(exchange2)
            def error(err_msg):
                self.conn_checked = True
                self.assertEqual(err_msg.__class__, thor.http.error.HttpVersionError)
                self.loop.stop()

            exchange1.request_start(b"GET", req_uri, [])
            exchange1.request_done([])

        def server_side(conn):
            conn.request.sendall(b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 5

12345""")
            time.sleep(2)
            conn.request.sendall(b"""\
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
                'version': b"1.1",
                'status': b"200",
                'phrase': b'OK',
                'body': b""
            })
            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/HEAD" % (test_host, test_port)
            exchange.request_start(b"HEAD", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
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
                'version': b"1.1",
                'status': b"200",
                'phrase': b'OK',
                'body': b"12345"
            })
            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/req_retry" % (test_host, test_port)
            exchange.request_start(b"OPTIONS", req_uri, [])
            exchange.request_done([])

        self.conn_num = 0
        def server_side(conn):
            self.conn_num += 1
            if self.conn_num > 1:
                conn.request.send(b"""\
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
                'version': b"1.1",
                'status': b"200",
                'phrase': b'OK',
                'body': b"12345"
            })

            @on(exchange)
            def error(err_msg):
                self.assertEqual(err_msg.__class__, thor.http.error.ConnectError)
                self.loop.stop()

            req_uri = b"http://%s:%i/req_retry_fail" % (test_host, test_port)
            exchange.request_start(b"OPTIONS", req_uri, [])
            exchange.request_done([])

        self.conn_num = 0
        def server_side(conn):
            self.conn_num += 1
            if self.conn_num > 3:
                conn.request.send(b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 5
Connection: close

12345""")
            conn.request.close()
        self.go([server_side], [client_side])


    def test_nobody(self):
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': b"1.1",
                'status': b"304",
                'phrase': b"Not Modified",
                'body': b''
            })

            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
HTTP/1.1 304 Not Modified
Content-Type: text/plain
Content-Length: 5
Connection: close

""")
            conn.request.close()
        self.go([server_side], [client_side])



    def test_nobody_body(self):
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': b"1.1",
                'status': b"304",
                'phrase': b"Not Modified",
                'body': b''
            })

            @on(exchange)
            def error(err_msg):
                self.assertEqual(err_msg.__class__, thor.http.error.ExtraDataError)
                self.loop.stop()

            req_uri = b"http://%s:%i" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
HTTP/1.1 304 Not Modified
Content-Type: text/plain
Content-Length: 5
Connection: close

12345""")
            conn.request.close()
        self.go([server_side], [client_side])


    def test_extra_body(self):
        def client_side(client):
            exchange = client.exchange()
            self.check_exchange(exchange, {
                'version': b"1.1",
                'status': b"200",
                'phrase': b"OK",
                'body': b'12345'
            })

            @on(exchange)
            def error(err_msg):
                self.assertEqual(err_msg.__class__, thor.http.error.ExtraDataError)
                self.loop.stop()

            req_uri = b"http://%s:%i" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.send(b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 5
Connection: close

1234567890""")
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

