#!/usr/bin/env python
# coding: utf-8

try:
    import SocketServer
except ImportError:
    import socketserver as SocketServer

import sys
import time
import socket
import threading
import unittest
from unittest.mock import MagicMock

import framework

import thor
import thor.http.error
from thor.events import on
from thor.http import HttpClient
from thor.http.common import Delimiters, States


def drain(conn, delimiter=b"\r\n\r\n"):
    """
    Consume the request data from the connection until the delimiter is found.
    This ensures that the test server has read the entire request (e.g., headers
    or a chunked body) before it sends a response and closes the connection,
    preventing race conditions and RST packets on slow environments.

    CAUTION: Use with care for requests without bodies, as it can cause
    ReadTimeoutErrors if the client hasn't sent the delimiter yet (e.g.,
    due to loop scheduling). For most simple requests, a well-placed
    sendall() on the server side is sufficient to prevent flakiness.
    """
    conn.request.settimeout(30.0)  # Longer than test timeout to avoid premature timeouts
    data = b""
    while delimiter not in data:
        try:
            chunk = conn.request.recv(1024)
            if not chunk:
                break
            data += chunk
        except socket.timeout:
            break
    conn.request.settimeout(None)


class LittleServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    allow_reuse_address = True


class DummyClientConnection(framework.DummyHttpParser):
    tcp_connected = True

    def attach(self, exchange):
        pass


class TestHttpClient(framework.ClientServerTestCase):
    def setUp(self):
        super().setUp()
        self.nonfinal_seen = False
        self.conn_id = None
        self.conn_checked = False
        self.test_req_count = 0
        self.conn_num = 0

    def create_server(self, server_side):
        class LittleRequestHandler(SocketServer.BaseRequestHandler):
            handle = server_side

        server = LittleServer((framework.test_host, 0), LittleRequestHandler)
        test_port = server.server_address[1]

        def serve():
            server.serve_forever(poll_interval=0.1)

        self.move_to_thread(target=serve)

        def stop():
            server.shutdown()
            server.server_close()

        return (stop, test_port)

    def create_client(self, host, port, client_side):
        client = HttpClient(loop=self.loop)
        client.connect_timeout = 15
        client_side(client, host, port)

    def check_exchange(self, exchange, expected):
        """
        Given an exchange, check that the status, phrase and body are as
        expected, and verify that it actually happened.
        """
        exchange.test_happened = False

        @on(exchange)
        def error(err_msg):
            exchange.test_happened = True
            if "error" in expected:
                exp = expected["error"]
                if isinstance(exp, type):
                    self.assertIsInstance(err_msg, exp)
                elif isinstance(exp, str):
                    self.assertEqual(str(err_msg), exp)
                else:
                    self.assertEqual(str(err_msg), str(exp))
            else:
                self.fail(f"Unexpected error: {repr(err_msg)} (expected: {expected})")
            self.loop.stop()

        @on(exchange)
        def response_start(status, phrase, headers):
            self.assertEqual(
                exchange.res_version, expected.get("version", exchange.res_version)
            )
            self.assertEqual(status, expected.get("status", status))
            self.assertEqual(phrase, expected.get("phrase", phrase))

        exchange.tmp_res_body = b""

        @on(exchange)
        def response_body(chunk):
            exchange.tmp_res_body += chunk

        @on(exchange)
        def response_done(trailers):
            exchange.test_happened = True
            self.assertEqual(
                exchange.tmp_res_body, expected.get("body", exchange.tmp_res_body)
            )

        @on(self.loop)
        def stop():
            self.assertTrue(exchange.test_happened, expected)

    def check_invalid_request_output(self, method, headers):
        client = HttpClient(loop=self.loop)
        conn = DummyClientConnection()

        def attach_conn(origin, handle_connect, handle_error):
            handle_connect(conn)

        client.attach_conn = attach_conn
        client.dead_conn = MagicMock()
        exchange = client.exchange()
        errors = []
        exchange.on("error", errors.append)
        exchange.request_start(method, b"http://example.com/", headers)
        exchange.request_body(b"abc")
        exchange.request_done([])

        client.dead_conn.assert_called_once_with(conn)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], thor.http.error.OutputSyntaxError)

    def test_request_short_body_reports_error(self):
        client = HttpClient(loop=self.loop)
        conn = DummyClientConnection()

        def attach_conn(origin, handle_connect, handle_error):
            handle_connect(conn)

        client.attach_conn = attach_conn
        client.dead_conn = MagicMock()
        exchange = client.exchange()
        errors = []
        exchange.on("error", errors.append)
        exchange.request_start(
            b"POST", b"http://example.com/", [(b"Content-Length", b"10")]
        )
        exchange.request_body(b"abc")
        exchange.request_done([])

        client.dead_conn.assert_called_once_with(conn)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], thor.http.error.OutputError)

    def test_request_bad_content_length_output_syntax(self) -> None:
        for value in [b"+5", b"1_0", b"abc", b"", b"\x0c5", b"5\x0b", b"5,5", b"5, 5"]:
            with self.subTest(value=value):
                self.check_invalid_request_output(b"POST", [(b"Content-Length", value)])

    def test_request_bad_method_output_syntax(self):
        self.check_invalid_request_output(b"GET\r\nX-Injected: yes", [])

    def test_request_bad_header_name_output_syntax(self):
        self.check_invalid_request_output(b"GET", [(b"Bad Name", b"value")])

    def test_basic(self):
        def client_side(client, test_host, test_port):
            exchange = client.exchange()
            self.check_exchange(
                exchange,
                {
                    "version": b"1.1",
                    "status": b"200",
                    "phrase": b"OK",
                    "body": b"12345",
                },
            )

            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/basic" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"12345"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_chunked_response(self):
        def client_side(client, test_host, test_port):
            exchange = client.exchange()
            self.check_exchange(
                exchange,
                {
                    "version": b"1.1",
                    "status": b"200",
                    "phrase": b"OK",
                    "body": b"12345",
                },
            )

            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/chunked_response" % (
                test_host,
                test_port,
            )
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
                b"5\r\n"
                b"12345\r\n"
                b"0\r\n"
                b"\r\n"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_chunked_request(self):
        req_body = b"54321"

        def client_side(client, test_host, test_port):
            exchange = client.exchange()
            self.check_exchange(
                exchange,
                {
                    "version": b"1.1",
                    "status": b"200",
                    "phrase": b"OK",
                    "body": b"12345",
                },
            )

            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/chunked_request" % (
                test_host,
                test_port,
            )
            exchange.request_start(b"POST", req_uri, [])
            exchange.request_body(req_body)
            exchange.request_body(req_body)
            exchange.request_done([])

        def server_side(conn):
            drain(conn, b"0\r\n\r\n")
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"12345"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_HEAD(self):
        def client_side(client, test_host, test_port):
            exchange = client.exchange()
            self.check_exchange(
                exchange,
                {"version": b"1.1", "status": b"200", "phrase": b"OK", "body": b""},
            )

            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/HEAD" % (test_host, test_port)
            exchange.request_start(b"HEAD", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 0\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_1xx(self):
        req_body = b"54321"

        def client_side(client, test_host, test_port):
            self.nonfinal_seen = False
            exchange = client.exchange()
            self.check_exchange(
                exchange,
                {
                    "version": b"1.1",
                    "status": b"200",
                    "phrase": b"OK",
                    "body": b"12345",
                },
            )

            @on(exchange)
            def response_nonfinal(status_code, status_phrase, headers):
                self.assertEqual(status_code, b"110")
                self.nonfinal_seen = True

            @on(exchange)
            def response_done(trailers):
                self.assertTrue(self.nonfinal_seen)
                self.loop.stop()

            req_uri = b"http://%s:%i/chunked_request" % (
                test_host,
                test_port,
            )
            exchange.request_start(b"POST", req_uri, [])
            exchange.request_body(req_body)
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/1.1 110 Whatever\r\n"
                b"This: that\r\n"
                b"\r\n"
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"12345\r\n"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_multiconn(self):
        self.test_req_count = 0

        def check_done(trailers):
            self.test_req_count += 1
            if self.test_req_count == 2:
                self.loop.stop()

        def client_side(client, test_host, test_port):
            exchange1 = client.exchange()
            self.check_exchange(
                exchange1,
                {
                    "version": b"1.1",
                    "status": b"200",
                    "phrase": b"OK",
                    "body": b"12345",
                },
            )

            exchange1.on("response_done", check_done)
            exchange2 = client.exchange()
            self.check_exchange(
                exchange2,
                {
                    "version": b"1.1",
                    "status": b"200",
                    "phrase": b"OK",
                    "body": b"12345",
                },
            )
            exchange2.on("response_done", check_done)

            req_uri = b"http://%s:%i/multiconn" % (
                test_host,
                test_port,
            )
            exchange1.request_start(b"GET", req_uri, [])
            exchange2.request_start(b"GET", req_uri, [])
            exchange1.request_done([])
            exchange2.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"12345"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_queued_long_body_reports_error(self):
        server_state = {"conns": 0, "data": b"", "eof": threading.Event()}

        def server_side(conn):
            server_state["conns"] += 1
            conn.request.settimeout(10.0)
            try:
                while True:
                    chunk = conn.request.recv(1024)
                    if not chunk:
                        break
                    server_state["data"] += chunk
            except socket.timeout:
                pass
            server_state["eof"].set()

        def client_side(client, test_host, test_port):
            errors = []
            exchange = client.exchange()

            @on(exchange)
            def error(err_msg):
                errors.append(err_msg)

            @on(exchange)
            def response_start(*args):
                errors.append("unexpected response")

            def check():
                self.assertTrue(server_state["eof"].wait(5))
                self.assertEqual(len(errors), 1, errors)
                self.assertIsInstance(errors[0], thor.http.error.OutputError)
                self.assertEqual(client.conn_counts, {})
                self.assertEqual(server_state["conns"], 1)
                self.assertTrue(server_state["data"].endswith(b"\r\n\r\n"))
                self.loop.stop()

            req_uri = b"http://%s:%i/queued" % (test_host, test_port)
            exchange.request_start(b"POST", req_uri, [(b"Content-Length", b"10")])
            exchange.request_body(b"x" * 20)
            exchange.request_done([])
            self.loop.schedule(0.5, check)

        self.go([server_side], [client_side])

    def test_max_server_conn_close(self):
        """
        max_server_conn holds when every response closes the connection.

        Covers sequential reuse (double-decrement and queue draining) only;
        counting of in-flight connects during a burst is #152.
        """
        total = 20
        lock = threading.Lock()
        both_arrived = threading.Barrier(2)
        active = 0
        arrived = 0
        self.peak = 0
        self.done_count = 0

        def check_done(trailers):
            self.done_count += 1
            if self.done_count == total:
                self.loop.stop()

        def client_side(client, test_host, test_port):
            client.max_server_conn = 2
            req_uri = b"http://%s:%i/max_conn" % (test_host, test_port)
            rest_started = False

            def start(count):
                for _ in range(count):
                    exchange = client.exchange()
                    self.check_exchange(
                        exchange,
                        {"status": b"200", "body": b"12345"},
                    )
                    exchange.on("response_done", check_done)
                    exchange.on("response_start", start_rest)
                    exchange.request_start(b"GET", req_uri, [])
                    exchange.request_done([])

            def start_rest(*args):
                # Both connections are open now, so the rest have to queue.
                nonlocal rest_started
                if not rest_started:
                    rest_started = True
                    start(total - 2)

            start(2)

        def server_side(conn):
            nonlocal active, arrived
            drain(conn)
            with lock:
                arrived += 1
                first_wave = arrived <= 2
                active += 1
                self.peak = max(self.peak, active)
            if first_wave:
                both_arrived.wait(timeout=10)
            time.sleep(0.02)
            with lock:
                active -= 1
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"12345"
            )
            conn.request.close()

        self.go([server_side], [client_side])
        self.assertEqual(self.done_count, total)
        self.assertLessEqual(self.peak, 2)

    def test_conn_refuse_err(self):
        def server_side(conn):
            pass

        def client_side(client, test_host, test_port):
            exchange = client.exchange()

            @on(exchange)
            def error(err_msg):
                self.assertEqual(err_msg.__class__, thor.http.error.ConnectError)
                self.loop.stop()

            req_uri = b"http://%s:%i/conn_refuse_err" % (
                framework.refuse_host,
                framework.refuse_port,
            )
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        self.go([server_side], [client_side])

    def test_conn_noname_err(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            # Relaxed check for any error type
            self.loop.stop()

        req_uri = b"http://foo.bar/conn_noname_err"
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_conn_bad_idna_err(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        errors = []

        @on(exchange)
        def error(err_msg):
            errors.append(err_msg)
            self.loop.stop()

        def do_timeout():
            self.timeout_hit = True
            self.loop.stop()

        # Passes URL parsing, but the lookup cannot decode it; no query is made.
        req_uri = b"http://xn--a.example/bad_idna"
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])
        self.loop.schedule(5, do_timeout)
        self.loop.run()
        self.assertFalse(self.timeout_hit)
        self.assertEqual([e.__class__ for e in errors], [thor.http.error.DnsError])

    def test_url_unsupported_scheme(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"notascheme://www.example.com:80000/"
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_url_nonascii_scheme(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = "httpü://www.example.com:80000/".encode("utf-8")
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_url_unbalanced_brackets(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"http://[www.example.com:80000/"
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_url_port_err(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"http://%s:ABC123/url_port_err" % (framework.test_host)
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_url_port_range(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"http://%s:80000/" % (framework.test_host)
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_url_empty_host(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"http://:80000/"
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_url_dot_host(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"http://.example.com:80000/"
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_url_nonascii_host(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = "http://www.ü.example.com:80000/".encode("utf-8")
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_url_percent_host(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"http://foo%.example.com:80000/"
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_url_star_host(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"http://example*.com:80000/"
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_url_startingdigit_label(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"http://www.5test.example.com:80000/"
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_url_long_label(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"http://%s:80000/" % (b"t" * 256)
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_url_long_hostname(self):
        client = HttpClient(loop=self.loop)
        exchange = client.exchange()

        @on(exchange)
        def error(err_msg):
            self.assertEqual(err_msg.__class__, thor.http.error.UrlError)
            self.loop.stop()

        req_uri = b"http://wwww.%s.example.com:80000/" % (b"t" * 64)
        exchange.request_start(b"GET", req_uri, [])
        exchange.request_done([])

    def test_http_version_err(self):
        def client_side(client, test_host, test_port):
            exchange = client.exchange()

            @on(exchange)
            def error(err_msg):
                self.assertEqual(err_msg.__class__, thor.http.error.HttpVersionError)
                self.loop.stop()

            req_uri = b"http://%s:%i/http_version_err" % (
                test_host,
                test_port,
            )
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/2.5 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"12345\r\n"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_http_start_encoding(self):
        def client_side(client, test_host, test_port):
            exchange = client.exchange()
            self.check_exchange(
                exchange,
                {
                    "version": b"1.1",
                    "status": b"200",
                    "phrase": "ÖK".encode("utf-8"),
                    "body": b"12345",
                },
            )

            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/http_start_encoding" % (
                test_host,
                test_port,
            )
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/1.1 200 \xc3\x96K\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"12345\r\n"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_http_protoname_err(self):
        def client_side(client, test_host, test_port):
            exchange = client.exchange()

            @on(exchange)
            def error(err_msg):
                self.assertEqual(err_msg.__class__, thor.http.error.HttpVersionError)
                self.loop.stop()

            req_uri = b"http://%s:%i/protoname_err" % (
                test_host,
                test_port,
            )
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"ICY/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"12345\r\n"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_close_in_body(self):
        def client_side(client, test_host, test_port):
            exchange = client.exchange()
            self.check_exchange(
                exchange,
                {
                    "version": b"1.1",
                    "status": b"200",
                    "phrase": b"OK",
                    "error": thor.http.error.ConnectError,
                },
            )

            req_uri = b"http://%s:%i/close_in_body" % (
                test_host,
                test_port,
            )
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 15\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"12345\r\n"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_slow_body_not_timed_out(self):
        self.check_slow_body(read_timeout=1.5)

    def test_slow_body_not_timed_out_default(self):
        # read_timeout unset: connect_timeout applies.
        self.check_slow_body(read_timeout=None)

    def check_slow_body(self, read_timeout):
        # A response that keeps making progress outlives the timeout.
        def client_side(client, test_host, test_port):
            client.connect_timeout = 1.5
            client.read_timeout = read_timeout
            exchange = client.exchange()
            self.check_exchange(
                exchange,
                {
                    "version": b"1.1",
                    "status": b"200",
                    "phrase": b"OK",
                    "body": b"abcdef",
                },
            )

            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/slow_body" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 6\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            for byte in (b"a", b"b", b"c", b"d", b"e", b"f"):
                time.sleep(0.3)
                conn.request.sendall(byte)
            conn.request.close()

        self.go([server_side], [client_side])

    def test_stalled_body_times_out(self):
        self.check_stalled_body(read_timeout=1)

    def test_stalled_body_times_out_default(self):
        # read_timeout unset: connect_timeout applies.
        self.check_stalled_body(read_timeout=None)

    def check_stalled_body(self, read_timeout):
        def client_side(client, test_host, test_port):
            client.connect_timeout = 1
            client.read_timeout = read_timeout
            exchange = client.exchange()
            self.check_exchange(
                exchange, {"error": thor.http.error.ReadTimeoutError}
            )

            @on(exchange)
            def error(err):
                self.assertEqual(err.detail, "read")

            req_uri = b"http://%s:%i/stalled_body" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            drain(conn)
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 6\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"abc"
            )
            # Stall until the client gives up and closes the connection.
            conn.request.settimeout(10.0)
            try:
                conn.request.recv(1024)
            except socket.timeout:
                pass
            conn.request.close()

        self.go([server_side], [client_side])

    def test_conn_reuse(self):
        self.conn_checked = False

        def client_side(client, test_host, test_port):
            req_uri = b"http://%s:%i/conn_reuse" % (
                test_host,
                test_port,
            )
            exchange1 = client.exchange()
            self.check_exchange(
                exchange1,
                {
                    "body": b"12345",
                },
            )

            @on(exchange1)
            def response_start(*args):
                self.conn_id = id(exchange1.conn.tcp_conn)

            @on(exchange1)
            def response_done(trailers):
                exchange2 = client.exchange()
                self.check_exchange(
                    exchange2,
                    {
                        "body": b"54321",
                    },
                )

                @on(exchange2)
                def response_start(*args):
                    self.assertEqual(self.conn_id, id(exchange2.conn.tcp_conn))
                    self.conn_checked = True

                @on(exchange2)
                def response_done(trailers):
                    self.loop.stop()

                exchange2.request_start(b"GET", req_uri, [])
                exchange2.request_done([])

            exchange1.request_start(b"GET", req_uri, [])
            exchange1.request_done([])

        def server_side(conn):
            drain(conn)
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"\r\n"
                b"12345"
            )
            # Second request - use recv with timeout to avoid indefinite hang
            conn.request.settimeout(30.0)
            try:
                conn.request.recv(8192)
            except socket.timeout:
                pass  # Client didn't send - close anyway
            finally:
                conn.request.settimeout(None)
            
            conn.request.sendall(
                b"HTTP/1.1 404 Not Found\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"54321"
            )
            conn.request.close()

        self.go([server_side], [client_side])
        self.assertTrue(self.conn_checked)

    def test_idle_timeout_closes_conn(self):
        "Guards that the server sees EOF after expiry; says nothing about the fd."
        server_eof = threading.Event()
        server_done = threading.Event()

        def client_side(client, test_host, test_port):
            client.idle_timeout = 0.1
            req_uri = b"http://%s:%i/idle_timeout" % (test_host, test_port)
            exchange = client.exchange()
            self.check_exchange(exchange, {"body": b"12345"})

            def wait_for_eof():
                if server_done.is_set():
                    self.assertEqual(len(client._idle_conns), 0)
                    self.assertEqual(len(client.conn_counts), 0)
                    self.loop.stop()
                else:
                    self.loop.schedule(0.05, wait_for_eof)

            @on(exchange)
            def response_done(trailers):
                wait_for_eof()

            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"\r\n"
                b"12345"
            )
            conn.request.settimeout(3.0)
            try:
                while conn.request.recv(8192):
                    pass
                server_eof.set()
            except socket.timeout:
                pass
            finally:
                server_done.set()

        self.go([server_side], [client_side])
        self.assertTrue(server_eof.is_set())

    def test_idle_timeout_unresponsive_peer(self):
        """
        Guards that an expired idle connection is fully closed (a peer that
        ignores the FIN is reset when it writes) and releases its slot.
        """
        release = threading.Event()
        self.peer_reset = False

        def client_side(client, test_host, test_port):
            client.idle_timeout = 0.1
            client.max_server_conn = 1
            req_uri = b"http://%s:%i/idle_timeout" % (test_host, test_port)
            exchange1 = client.exchange()
            self.check_exchange(exchange1, {"body": b"12345"})
            exchange2 = client.exchange()
            self.check_exchange(exchange2, {"body": b"12345"})

            def second_request():
                self.assertEqual(len(client._idle_conns), 0)
                self.assertEqual(len(client.conn_counts), 0)
                exchange2.request_start(b"GET", req_uri, [])
                exchange2.request_done([])

            @on(exchange1)
            def response_done(trailers):
                self.loop.schedule(0.5, second_request)

            @on(exchange2)
            def response_done(trailers):
                release.set()
                self.loop.stop()

            exchange1.request_start(b"GET", req_uri, [])
            exchange1.request_done([])

        def server_side(conn):
            self.conn_num += 1
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"\r\n"
                b"12345"
            )
            if self.conn_num == 1:
                # Ignore the client closing, then probe whether it's really gone;
                # a half-closed client would still accept this data.
                conn.request.settimeout(0.25)
                try:
                    while conn.request.recv(8192):
                        pass
                    for _ in range(20):
                        try:
                            conn.request.sendall(b"x")
                        except (ConnectionResetError, BrokenPipeError):
                            self.peer_reset = True
                            break
                        time.sleep(0.05)  # give any RST time to arrive
                except socket.timeout:
                    pass
                release.wait(10)

        self.go([server_side], [client_side])
        self.assertEqual(self.conn_num, 2)
        self.assertTrue(self.peer_reset)

    def test_conn_succeed_then_err(self):
        self.conn_checked = False

        def client_side(client, test_host, test_port):
            req_uri = b"http://%s:%i/succeed_then_err" % (
                test_host,
                test_port,
            )
            exchange1 = client.exchange()
            self.check_exchange(
                exchange1,
                {
                    "version": b"1.1",
                    "status": b"200",
                    "phrase": b"OK",
                    "body": b"foo",
                },
            )
            exchange2 = client.exchange()

            @on(exchange1)
            def response_start(*args):
                self.conn_id = id(exchange1.conn.tcp_conn)

            @on(exchange1)
            def response_done(trailers):
                def start2():
                    exchange2.request_start(b"GET", req_uri, [])
                    exchange2.request_done([])

                self.loop.schedule(0.1, start2)

            @on(exchange2)
            def error(err_msg):
                self.conn_checked = True
                self.assertEqual(err_msg.__class__, thor.http.error.HttpVersionError)
                self.loop.stop()

            exchange1.request_start(b"GET", req_uri, [])
            exchange1.request_done([])

        def server_side(conn):
            conn.request.send(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 3\r\n"
                b"\r\n"
                b"foo"
            )
            time.sleep(0.2)
            conn.request.sendall(
                b"HTTP/9.1 404 Not Found\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"54321\r\n"
            )
            conn.request.close()

        self.go([server_side], [client_side])
        self.assertTrue(self.conn_checked)

    def test_req_retry(self):
        def client_side(client, test_host, test_port):
            exchange = client.exchange()
            self.check_exchange(
                exchange,
                {
                    "version": b"1.1",
                    "status": b"200",
                    "phrase": b"OK",
                    "body": b"12345",
                },
            )

            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i/req_retry" % (
                test_host,
                test_port,
            )
            exchange.request_start(b"OPTIONS", req_uri, [])
            exchange.request_done([])

        self.conn_num = 0
        self.conn_lock = threading.Lock()

        def server_side(conn):
            with self.conn_lock:
                self.conn_num += 1
                send_response = self.conn_num > 1
            
            if send_response:
                drain(conn)
                conn.request.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Content-Length: 5\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                    b"12345\r\n"
                )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_retry_clears_dead_connection(self):
        client = HttpClient(loop=MagicMock())
        client.retry_delay = 1
        exchange = client.exchange()
        exchange.method = b"GET"
        exchange.origin = (b"http", b"example.com", 80)
        exchange.conn = DummyClientConnection()

        exchange.conn_closed(States.WAITING, Delimiters.NONE)

        self.assertIsNone(exchange.conn)
        client.loop.schedule.assert_called_once_with(1, exchange._retry)

    def test_retry_replays_request_start_on_new_connection(self):
        client = HttpClient(loop=MagicMock())
        client.attach_conn = MagicMock()
        exchange = client.exchange()
        exchange.method = b"OPTIONS"
        exchange.req_hdrs = []
        exchange.req_target = b"/"
        exchange.authority = b"example.com"
        exchange.origin = ("http", "example.com", 80)
        exchange.conn = DummyClientConnection()
        exchange._req_started = True
        exchange._req_done_trailers = []

        exchange._retry()

        queued_events = [item[0] for item in exchange._output_q]
        self.assertEqual(queued_events, ["start", "end"])
        self.assertIsNone(exchange.conn)
        client.attach_conn.assert_called_once_with(
            exchange.origin, exchange.handle_connect, exchange.handle_connect_error
        )

    def test_req_retry_fail(self):
        def client_side(client, test_host, test_port):
            exchange = client.exchange()

            self.check_exchange(
                exchange,
                {
                    "error": thor.http.error.ConnectError,
                },
            )

            @on(exchange)
            def response_done(trailers):
                self.fail("Should have failed")

            @on(exchange)
            def error(err_msg):
                self.assertEqual(err_msg.__class__, thor.http.error.ConnectError)
                self.loop.stop()

            req_uri = b"http://%s:%i/req_retry_fail" % (
                test_host,
                test_port,
            )
            exchange.request_start(b"OPTIONS", req_uri, [])
            exchange.request_done([])

        self.conn_num = 0
        self.conn_lock = threading.Lock()

        def server_side(conn):
            with self.conn_lock:
                self.conn_num += 1
                send_response = self.conn_num > 3
            
            if send_response:
                drain(conn)  # Only drain if we're sending a response
                conn.request.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Content-Length: 5\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                    b"12345\r\n"
                )
            conn.request.close()

        self.go([server_side], [client_side])

    def _retry_body_test(
        self, req_hdrs, delimiter, body_limit=None, expect_error=False
    ):
        "PUT a two-chunk body; the server drops the first connection after reading it."
        self.second_request = None

        def client_side(client, test_host, test_port):
            client.retry_delay = 0.01
            if body_limit is not None:
                client.retry_body_limit = body_limit
            exchange = client.exchange()
            if expect_error:
                self.check_exchange(exchange, {"error": thor.http.error.ConnectError})
            else:
                self.check_exchange(exchange, {"status": b"200", "body": b"12345"})

                @on(exchange)
                def response_done(trailers):
                    self.loop.stop()

            req_uri = b"http://%s:%i/retry_body" % (test_host, test_port)
            exchange.request_start(b"PUT", req_uri, req_hdrs)
            exchange.request_body(b"IMPORTANT ")
            exchange.request_body(b"DATA")
            exchange.request_done([])

        self.conn_num = 0
        self.conn_lock = threading.Lock()

        def server_side(conn):
            with self.conn_lock:
                self.conn_num += 1
                conn_num = self.conn_num
            conn.request.settimeout(30.0)
            data = b""
            while delimiter not in data:
                chunk = conn.request.recv(1024)
                if not chunk:
                    break
                data += chunk
            if conn_num == 1:
                conn.request.close()
                return
            self.second_request = data
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"12345"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_req_retry_body_content_length(self):
        self._retry_body_test([(b"Content-Length", b"14")], b"IMPORTANT DATA")
        self.assertIsNotNone(self.second_request)
        self.assertTrue(
            self.second_request.startswith(b"PUT /retry_body HTTP/1.1\r\n")
        )
        self.assertTrue(self.second_request.endswith(b"\r\n\r\nIMPORTANT DATA"))

    def test_req_retry_body_chunked(self):
        self._retry_body_test([], b"0\r\n\r\n")
        self.assertIsNotNone(self.second_request)
        self.assertTrue(
            self.second_request.startswith(b"PUT /retry_body HTTP/1.1\r\n")
        )
        self.assertTrue(
            self.second_request.endswith(
                b"\r\n\r\na\r\nIMPORTANT \r\n4\r\nDATA\r\n0\r\n\r\n"
            )
        )

    def test_retry_body_not_kept_for_non_idempotent(self):
        client = HttpClient(loop=MagicMock())
        client.attach_conn = MagicMock()
        exchange = client.exchange()
        exchange.request_start(b"POST", b"http://example.com/", [])
        exchange.request_body(b"IMPORTANT DATA")
        self.assertEqual(exchange._req_body_chunks, [])

        # A connect-failure retry leaves the queued body intact.
        exchange.request_done([])
        exchange._retry()
        queued_events = [item[0] for item in exchange._output_q]
        self.assertEqual(queued_events, ["start", "body", "end"])

    def test_req_retry_body_over_limit(self):
        self._retry_body_test(
            [(b"Content-Length", b"14")],
            b"IMPORTANT DATA",
            body_limit=8,
            expect_error=True,
        )
        self.assertIsNone(self.second_request)

    def test_nobody(self):
        def client_side(client, test_host, test_port):
            exchange = client.exchange()
            self.check_exchange(
                exchange,
                {
                    "version": b"1.1",
                    "status": b"304",
                    "phrase": b"Not Modified",
                    "body": b"",
                },
            )

            @on(exchange)
            def response_done(trailers):
                self.loop.stop()

            req_uri = b"http://%s:%i" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/1.1 304 Not Modified\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_nobody_body(self):
        def client_side(client, test_host, test_port):
            exchange = client.exchange()
            self.check_exchange(
                exchange,
                {
                    "version": b"1.1",
                    "status": b"304",
                    "phrase": b"Not Modified",
                    "body": b"",
                    "error": thor.http.error.ExtraDataError,
                },
            )

            req_uri = b"http://%s:%i" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/1.1 304 Not Modified\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"12345\r\n"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def test_extra_body(self):
        def client_side(client, test_host, test_port):
            exchange = client.exchange()
            self.check_exchange(
                exchange,
                {
                    "version": b"1.1",
                    "status": b"200",
                    "phrase": b"OK",
                    "body": b"12345",
                    "error": thor.http.error.ExtraDataError,
                },
            )

            req_uri = b"http://%s:%i" % (test_host, test_port)
            exchange.request_start(b"GET", req_uri, [])
            exchange.request_done([])

        def server_side(conn):
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"1234567890\r\n"
            )
            conn.request.close()

        self.go([server_side], [client_side])

    def check_injected_response(self, injected, queued):
        """
        The server sends a second, unsolicited response in the same write as the
        first. It must not be delivered to the next request, and the connection
        must be dropped. The next request is attached from the first one's
        response_done handler, or (if queued) by a handoff from the pool.
        """

        test_done = threading.Event()

        def client_side(client, test_host, test_port):
            self.loop.on("stop", test_done.set)
            client.max_server_conn = 1
            req_uri = b"http://%s:%i/" % (test_host, test_port)
            exchange1 = client.exchange()
            self.check_exchange(exchange1, {"body": b"12345"})
            exchange2 = client.exchange()
            self.check_exchange(exchange2, {"error": thor.http.error.ExtraDataError})

            @on(exchange2)
            def response_start(*args):
                self.fail(f"Injected response delivered: {args!r}")

            @on(exchange2)
            def response_body(chunk):
                self.fail(f"Injected response delivered: {chunk!r}")

            @on(exchange2)
            def error(err):
                conn = exchange2.conn
                self.assertFalse(client._idle_conns)
                tcp = conn.tcp_conn
                self.assertTrue(
                    tcp._closing or tcp._closed_output or not tcp.tcp_connected
                )

            def start2():
                exchange2.request_start(b"GET", req_uri, [])
                exchange2.request_done([])

            exchange1.request_start(b"GET", req_uri, [])
            exchange1.request_done([])
            # Once the connection exists the second request has to queue for it.
            event = "response_start" if queued else "response_done"
            exchange1.on(event, lambda *args: start2())

        def server_side(conn):
            drain(conn)
            conn.request.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n12345" + injected
            )
            test_done.wait(30)
            conn.request.close()

        self.go([server_side], [client_side])

    def test_coalesced_extra(self):
        self.check_injected_response(
            b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nPOISON", False
        )

    def test_coalesced_extra_nobody(self):
        self.check_injected_response(b"HTTP/1.1 204 No Content\r\n\r\n", False)

    def test_coalesced_extra_queued(self):
        self.check_injected_response(
            b"HTTP/1.1 200 OK\r\nX-Evil: 1\r\nContent-Length: 0\r\n\r\n", True
        )

    def test_coalesced_extra_queued_nobody(self):
        self.check_injected_response(b"HTTP/1.1 204 No Content\r\n\r\n", True)

    def test_idle_extra_data(self):
        """
        Data that arrives on a pooled connection after a complete response must
        not be buffered and parsed as the response to a later request; the
        connection is discarded instead.
        """
        self.conn_num = 0
        self.conn_checked = False
        first_done = threading.Event()
        test_done = threading.Event()

        def client_side(client, test_host, test_port):
            self.loop.on("stop", test_done.set)
            req_uri = b"http://%s:%i/" % (test_host, test_port)
            exchange1 = client.exchange()
            self.check_exchange(exchange1, {"body": b"A"})

            def start2():
                exchange2 = client.exchange()
                self.check_exchange(exchange2, {"body": b"GOOD"})

                @on(exchange2)
                def response_start(*args):
                    self.assertNotEqual(self.conn_id, id(exchange2.conn.tcp_conn))
                    self.conn_checked = True

                @on(exchange2)
                def response_done(trailers):
                    self.loop.stop()

                exchange2.request_start(b"GET", req_uri, [])
                exchange2.request_done([])

            @on(exchange1)
            def response_start(*args):
                self.conn_id = id(exchange1.conn.tcp_conn)

            @on(exchange1)
            def response_done(trailers):
                # Run after the connection has seen the unsolicited data.
                exchange1.conn.tcp_conn.once(
                    "data", lambda _: self.loop.schedule(0, start2)
                )
                first_done.set()

            exchange1.request_start(b"GET", req_uri, [])
            exchange1.request_done([])

        def server_side(conn):
            self.conn_num += 1
            drain(conn)
            if self.conn_num == 1:
                conn.request.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nA"
                )
                first_done.wait(30)
                conn.request.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nPOISON"
                )
            else:
                conn.request.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nGOOD"
                )
            test_done.wait(30)
            conn.request.close()

        self.go([server_side], [client_side])
        self.assertTrue(self.conn_checked)


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

if __name__ == "__main__":
    unittest.main()
