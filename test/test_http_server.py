##!/usr/bin/env python

import socket
import sys
import time
import unittest
from unittest.mock import MagicMock

import framework

import thor
import thor.http.error
from thor.events import on
from thor.http import HttpServer, HttpServerExchange
from thor.http.server import HttpServerConnection


def wire(data):
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


class DummyServerConnection(framework.DummyHttpParser):
    def exchange_done(self, exchange):
        pass


class FakeTcpConnection:
    tcp_connected = True

    def __init__(self):
        self.output = []

    def write(self, data):
        self.output.append(data)

    def close(self):
        self.tcp_connected = False


class FakeServer:
    idle_timeout = 60
    shutting_down = False

    def __init__(self):
        self.loop = MagicMock()
        self.loop.schedule.return_value = MagicMock()
        self.events = []

    def emit(self, event, *args):
        self.events.append((event, args))


class TestHttpServer(framework.ClientServerTestCase):
    def create_server(self, server_side):
        server = HttpServer(framework.test_host, 0, loop=self.loop)
        test_port = server.tcp_server.sock.getsockname()[1]
        server_side(server)

        def stop():
            server.shutdown()

        return (stop, test_port)

    def create_client(self, test_host, test_port, client_side):
        def run_client(client_side1):
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect((test_host, test_port))
            client_side1(client, test_host, test_port)
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
            self.assertEqual(err_msg, expected.get("error", err_msg))
            self.loop.stop()

        @on(exchange)
        def request_start(method, uri, headers):
            self.assertEqual(method, expected.get("method", method))
            self.assertEqual(uri, expected.get("phrase", uri))

        exchange.tmp_req_body = b""

        @on(exchange)
        def request_body(chunk):
            exchange.tmp_req_body += chunk

        @on(exchange)
        def request_done(trailers):
            exchange.test_happened = True
            self.assertEqual(trailers, expected.get("req_trailers", trailers))
            self.assertEqual(
                exchange.tmp_req_body, expected.get("body", exchange.tmp_req_body)
            )
            self.loop.stop()

        @on(self.loop)
        def stop():
            self.assertTrue(exchange.test_happened)

    def check_invalid_response_output(self, status_phrase, headers):
        conn = DummyServerConnection()
        exchange = HttpServerExchange(conn, b"GET", b"/", [], b"1.1")
        with self.assertRaises(thor.http.error.OutputSyntaxError):
            exchange.response_start(b"200", status_phrase, headers)

    def test_response_bad_status_phrase_output_syntax(self):
        self.check_invalid_response_output(b"OK\r\nX-Injected: yes", [])

    def test_response_bad_header_name_output_syntax(self):
        self.check_invalid_response_output(b"OK", [(b"Bad Name", b"value")])

    def test_nonfinal_bad_status_phrase_output_syntax(self):
        conn = DummyServerConnection()
        exchange = HttpServerExchange(conn, b"GET", b"/", [], b"1.1")
        with self.assertRaises(thor.http.error.OutputSyntaxError):
            exchange.response_nonfinal(b"103", b"Early\r\nX-Injected: yes", [])

    def test_basic(self):
        def server_side(server):
            def check(exchange):
                self.check_exchange(exchange, {"method": b"GET", "uri": b"/"})

            server.on("exchange", check)

        def client_side(client_conn, test_host, test_port):
            client_conn.sendall(
                wire(
                    b"""\
GET / HTTP/1.1
Host: %s:%i

"""
                    % (test_host, test_port)
                )
            )
            time.sleep(0.1)
            client_conn.close()

        self.go([server_side], [client_side])

    def test_extraline(self):
        def server_side(server):
            def check(exchange):
                self.check_exchange(exchange, {"method": b"GET", "uri": b"/"})

            server.on("exchange", check)

        def client_side(client_conn, test_host, test_port):
            client_conn.sendall(
                wire(
                    b"""\

GET / HTTP/1.1
Host: %s:%i\r
\r
"""
                    % (test_host, test_port)
                )
            )
            time.sleep(0.1)
            client_conn.close()

        self.go([server_side], [client_side])

    def test_post(self):
        def server_side(server):
            def check(exchange):
                self.check_exchange(exchange, {"method": b"POST", "uri": b"/foo"})

            server.on("exchange", check)

        def client_side(client_conn, test_host, test_port):
            client_conn.sendall(
                wire(
                    b"""\
POST / HTTP/1.1
Host: %s:%i
Content-Type: text/plain
Content-Length: 5

12345"""
                    % (test_host, test_port)
                )
            )
            time.sleep(0.1)
            client_conn.close()

        self.go([server_side], [client_side])

    def test_post_extra_crlf(self):
        def server_side(server):
            def check(exchange):
                self.check_exchange(exchange, {"method": b"POST", "uri": b"/foo"})

            server.on("exchange", check)

        def client_side(client_conn, test_host, test_port):
            client_conn.sendall(
                wire(
                    b"""\
POST / HTTP/1.1
Host: %s:%i
Content-Type: text/plain
Content-Length: 5

12345
"""
                    % (test_host, test_port)
                )
            )
            time.sleep(0.1)
            client_conn.close()

        self.go([server_side], [client_side])

    def test_1xx_response(self):
        def server_side(server):
            def check(exchange):
                @on(exchange, "request_done")
                def on_request_done(trailers):
                    exchange.response_nonfinal(
                        b"103",
                        b"Early Hints",
                        [(b"Link", b"</style.css>; rel=preload; as=style")],
                    )
                    exchange.response_start(b"200", b"OK", [(b"Content-Length", b"5")])
                    exchange.response_body(b"hello")
                    exchange.response_done([])
                    self.exchange_handled = True

            server.on("exchange", check)

        def client_side(client_conn, test_host, test_port):
            client_conn.sendall(
                b"GET / HTTP/1.1\r\nHost: %s:%i\r\n\r\n" % (test_host, test_port)
            )
            res = b""
            client_conn.settimeout(2)
            try:
                while True:
                    chunk = client_conn.recv(1024)
                    if not chunk:
                        break
                    res += chunk
                    if b"hello" in res:
                        break
            except socket.timeout:
                pass
            self.res = res
            self.loop.stop()

        self.exchange_handled = False
        self.res = b""
        self.go([server_side], [client_side])
        self.assertTrue(self.exchange_handled)
        self.assertIn(b"HTTP/1.1 103 Early Hints", self.res)
        self.assertIn(b"HTTP/1.1 200 OK", self.res)
        self.assertIn(b"hello", self.res)

    def test_bad_http_version(self):
        def server_side(server):
            def check(exchange):
                self.fail(f"Unexpected exchange for bad version: {exchange!r}")

            server.on("exchange", check)

        def client_side(client_conn, test_host, test_port):
            client_conn.sendall(
                b"GET / HTTP/1.2\r\nHost: %s:%i\r\n\r\n" % (test_host, test_port)
            )
            res = client_conn.recv(1024)
            self.assertIn(b"HTTP/1.1 505 HTTP Version Not Supported", res)
            self.loop.stop()

        self.go([server_side], [client_side])

    def test_pipeline_queue_limit(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        conn.max_pipeline_requests = 1

        conn.handle_input(
            b"GET /one HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"\r\n"
            b"GET /two HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"\r\n"
        )

        self.assertFalse(tcp_conn.tcp_connected)
        exchanges = [event for event, args in server.events if event == "exchange"]
        self.assertEqual(len(exchanges), 1)
        output = b"".join(tcp_conn.output)
        self.assertIn(b"HTTP/1.1 400 Bad Request", output)
        self.assertIn(b"Too many messages to parse", output)

    def test_idle_timer_waits_for_exchange_queue_to_drain(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        server.loop.schedule.reset_mock()
        first = HttpServerExchange(conn, b"GET", b"/one", [], b"1.1")
        second = HttpServerExchange(conn, b"GET", b"/two", [], b"1.1")
        first.req_complete = True
        second.req_complete = True
        conn.ex_queue = [first, second]

        first.response_start(b"200", b"OK", [(b"Content-Length", b"0")])
        first.response_done([])

        server.loop.schedule.assert_not_called()

        second.response_start(b"200", b"OK", [(b"Content-Length", b"0")])
        second.response_done([])

        server.loop.schedule.assert_called_once_with(
            server.idle_timeout, conn.close_conn
        )

    def test_response_done_close_closes_tcp(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        exchange = HttpServerExchange(conn, b"POST", b"/", [], b"1.1")
        conn.ex_queue = [exchange]

        exchange.response_start(b"413", b"Content Too Large", [])
        exchange.response_body(b"too big")
        exchange.response_done([], close=True)

        self.assertFalse(tcp_conn.tcp_connected)
        self.assertIsNone(conn.tcp_conn)

    def test_response_done_default_keeps_connection(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        exchange = HttpServerExchange(conn, b"GET", b"/", [], b"1.1")
        exchange.req_complete = True
        conn.ex_queue = [exchange]

        exchange.response_start(b"200", b"OK", [(b"Content-Length", b"0")])
        exchange.response_done([])

        self.assertTrue(tcp_conn.tcp_connected)

    def test_early_response_drops_remaining_body(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)

        request_done_count = [0]

        def handle_exchange(exchange):
            @on(exchange, "request_body")
            def on_body(chunk):
                if not exchange.res_complete:
                    exchange.response_start(
                        b"413", b"Content Too Large", [(b"Content-Length", b"0")]
                    )
                    exchange.response_done([], close=True)

            @on(exchange, "request_done")
            def on_done(trailers):
                request_done_count[0] += 1

        server.emit = lambda event, *args: (
            handle_exchange(args[0]) if event == "exchange" else None
        )

        conn.handle_input(
            b"POST / HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Content-Length: 10\r\n"
            b"\r\n"
            b"0123456789"
        )

        self.assertEqual(request_done_count[0], 0)
        self.assertFalse(tcp_conn.tcp_connected)
        output = b"".join(tcp_conn.output)
        self.assertIn(b"HTTP/1.1 413 Content Too Large", output)

    def test_reentrancy(self):
        def server_side(server):
            def check(exchange):
                @on(exchange, "request_done")
                def on_request_done(trailers):
                    ex = exchange
                    ex.response_start(b"200", b"OK", [])
                    ex.response_body(b"done")
                    ex.response_done([])
                    self.exchange_handled = True
                    self.loop.stop()

            server.on("exchange", check)

        def client_side(client_conn, test_host, test_port):
            client_conn.sendall(
                wire(
                    b"""\
POST / HTTP/1.1
Host: %s:%i
Content-Length: 0

"""
                    % (test_host, test_port)
                )
            )
            time.sleep(0.1)
            client_conn.close()

        self.exchange_handled = False
        self.go([server_side], [client_side])
        self.assertTrue(self.exchange_handled)


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
#        def client_side(client_conn, test_host, test_port):
#            client_conn.sendall("""\
# GET / HTTP/1.1
# Host: %s:%i
#
# GET / HTTP/1.1
# Host: %s:%i
#
# """ % (
#    test_host, test_port,
#    test_host, test_port
# ))
#            time.sleep(0.1)
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
#    def test_startline_encoding(self):


if __name__ == "__main__":
    unittest.main()
