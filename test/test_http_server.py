##!/usr/bin/env python

import itertools
import socket
import sys
import time
import unittest
from typing import List
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


class CapturingServerConnection(DummyServerConnection):
    def __init__(self) -> None:
        DummyServerConnection.__init__(self)
        self.written: List[bytes] = []

    def output(self, data: bytes) -> None:
        self.written.append(data)


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
    request_timeout = 30
    response_timeout = None
    shutting_down = False

    def __init__(self):
        self.loop = MagicMock()
        self.loop.schedule.return_value = MagicMock()
        self.events = []
        self.connections = set()

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

    def test_response_bad_content_length_output_syntax(self) -> None:
        for value in [b"+5", b"1_0", b" 0x10", b"abc", b"", b"\x0c5", b"5,5"]:
            with self.subTest(value=value):
                conn = CapturingServerConnection()
                exchange = HttpServerExchange(conn, b"GET", b"/", [], b"1.1")
                with self.assertRaises(thor.http.error.OutputSyntaxError):
                    exchange.response_start(b"200", b"OK", [(b"Content-Length", value)])
                self.assertEqual(conn.written, [])

    def test_response_bodiless_bad_content_length_output_syntax(self) -> None:
        # Validated even where the header would be stripped (204) or unused.
        for method, status in [(b"HEAD", b"200"), (b"GET", b"204"), (b"GET", b"304")]:
            with self.subTest(method=method, status=status):
                conn = CapturingServerConnection()
                exchange = HttpServerExchange(conn, method, b"/", [], b"1.1")
                with self.assertRaises(thor.http.error.OutputSyntaxError):
                    exchange.response_start(status, b"X", [(b"Content-Length", b"")])
                self.assertEqual(conn.written, [])

    def test_response_conflicting_content_length_output_syntax(self) -> None:
        self.check_invalid_response_output(
            b"OK", [(b"Content-Length", b"5"), (b"Content-Length", b"6")]
        )

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

    def check_bodiless_response(
        self,
        method,
        status,
        phrase,
        hdrs,
        body,
        req_hdrs=(),
        req_version=b"1.1",
        expect_open=True,
        persistent=True,
    ):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        exchange = HttpServerExchange(
            conn, method, b"/", list(req_hdrs), req_version, persistent
        )
        exchange.req_complete = True
        conn.exchanges.add(exchange)

        exchange.response_start(status, phrase, hdrs)
        exchange.response_body(body)
        exchange.response_done([])

        self.assertEqual(tcp_conn.tcp_connected, expect_open)
        return b"".join(tcp_conn.output)

    def test_204_response_has_no_framing(self):
        output = self.check_bodiless_response(
            b"GET", b"204", b"No Content", [], b"ignored"
        )
        self.assertNotIn(b"Transfer-Encoding", output)
        self.assertTrue(output.endswith(b"\r\n\r\n"))
        self.assertEqual(output.count(b"\r\n\r\n"), 1)

    def test_304_response_has_no_framing(self):
        output = self.check_bodiless_response(
            b"GET", b"304", b"Not Modified", [(b"ETag", b'"a"')], b""
        )
        self.assertNotIn(b"Transfer-Encoding", output)
        self.assertTrue(output.endswith(b"\r\n\r\n"))
        self.assertEqual(output.count(b"\r\n\r\n"), 1)

    def test_head_response_without_content_length_has_no_framing(self):
        output = self.check_bodiless_response(b"HEAD", b"200", b"OK", [], b"hello")
        self.assertNotIn(b"Transfer-Encoding", output)
        self.assertNotIn(b"hello", output)
        self.assertTrue(output.endswith(b"\r\n\r\n"))
        self.assertEqual(output.count(b"\r\n\r\n"), 1)

    def test_head_response_keeps_content_length(self):
        output = self.check_bodiless_response(
            b"HEAD", b"200", b"OK", [(b"Content-Length", b"5")], b"hello"
        )
        self.assertIn(b"Content-Length: 5\r\n", output)
        self.assertNotIn(b"hello", output)
        self.assertTrue(output.endswith(b"\r\n\r\n"))

    def test_204_response_drops_content_length(self):
        output = self.check_bodiless_response(
            b"GET", b"204", b"No Content", [(b"Content-Length", b"5")], b""
        )
        self.assertNotIn(b"Content-Length", output)

    def test_1xx_final_response_drops_content_length(self):
        output = self.check_bodiless_response(
            b"GET", b"101", b"Switching", [(b"Content-Length", b"5")], b""
        )
        self.assertNotIn(b"Content-Length", output)
        self.assertNotIn(b"Transfer-Encoding", output)

    def test_304_response_keeps_content_length(self):
        output = self.check_bodiless_response(
            b"GET", b"304", b"Not Modified", [(b"Content-Length", b"5")], b""
        )
        self.assertIn(b"Content-Length: 5\r\n", output)

    def test_http10_bodiless_response_closes(self):
        for status, phrase, method in [
            (b"200", b"OK", b"HEAD"),
            (b"204", b"No Content", b"GET"),
        ]:
            output = self.check_bodiless_response(
                method,
                status,
                phrase,
                [],
                b"",
                req_hdrs=[(b"Host", b"example.com")],
                req_version=b"1.0",
                expect_open=False,
                persistent=False,
            )
            self.assertIn(b"Connection: close\r\n", output)
            self.assertNotIn(b"Transfer-Encoding", output)

    def test_http10_bodiless_response_keep_alive(self):
        output = self.check_bodiless_response(
            b"HEAD",
            b"200",
            b"OK",
            [(b"Content-Length", b"5")],
            b"",
            req_hdrs=[(b"Host", b"example.com"), (b"Connection", b"Keep-Alive")],
            req_version=b"1.0",
        )
        self.assertIn(b"Connection: keep-alive\r\n", output)

    def test_bodiless_responses_do_not_corrupt_next_response(self):
        def server_side(server):
            def check(exchange):
                @on(exchange, "request_done")
                def on_request_done(trailers):
                    if exchange.method == b"GET" and exchange.uri == b"/204":
                        exchange.response_start(b"204", b"No Content", [])
                    elif exchange.method == b"HEAD":
                        exchange.response_start(b"200", b"OK", [])
                        exchange.response_body(b"nope")
                    else:
                        exchange.response_start(
                            b"200", b"OK", [(b"Content-Length", b"5")]
                        )
                        exchange.response_body(b"final")
                    exchange.response_done([])

            server.on("exchange", check)

        def client_side(client_conn, test_host, test_port):
            req = b"%s %s HTTP/1.1\r\nHost: %s:%i\r\n\r\n"
            client_conn.sendall(
                req % (b"GET", b"/204", test_host, test_port)
                + req % (b"HEAD", b"/", test_host, test_port)
                + req % (b"GET", b"/", test_host, test_port)
            )
            res = b""
            client_conn.settimeout(2)
            try:
                while not res.endswith(b"final"):
                    chunk = client_conn.recv(1024)
                    if not chunk:
                        break
                    res += chunk
            except socket.timeout:
                pass
            self.res = res
            self.loop.stop()

        self.res = b""
        self.go([server_side], [client_side])
        self.assertEqual(self.res.count(b"HTTP/1.1 "), 3)
        self.assertNotIn(b"Transfer-Encoding", self.res)
        self.assertNotIn(b"nope", self.res)
        self.assertTrue(self.res.startswith(b"HTTP/1.1 204 No Content\r\n"))
        self.assertTrue(
            self.res.endswith(
                b"Content-Length: 5\r\n\r\nfinal"
            )
        )

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

    def check_bad_chunked_request(self, body):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)

        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n" + body
        )

        self.assertFalse(tcp_conn.tcp_connected)
        self.assertIn(b"HTTP/1.1 400 Bad Request", b"".join(tcp_conn.output))

    def test_bad_chunk_size(self):
        self.check_bad_chunked_request(b"ZZZ\r\n")

    def test_bad_chunk_terminator(self):
        self.check_bad_chunked_request(b"3\r\nabcXY0\r\n\r\n")

    def test_bad_chunk_size_real_connection(self):
        def server_side(server):
            pass

        def client_side(client_conn, test_host, test_port):
            client_conn.sendall(
                b"POST / HTTP/1.1\r\nHost: %s:%i\r\n"
                b"Transfer-Encoding: chunked\r\n\r\nZZZ\r\n" % (test_host, test_port)
            )
            res = client_conn.recv(1024)
            self.assertIn(b"HTTP/1.1 400 Bad Request", res)
            self.loop.stop()

        self.go([server_side], [client_side])

    def test_parse_error_server_statuses(self):
        bad_request = (b"400", b"Bad Request")
        for err in [
            thor.http.error.ChunkError,
            thor.http.error.ChunkTerminatorError,
            thor.http.error.ExtraDataError,
        ]:
            with self.subTest(err.__name__):
                self.assertEqual(err.server_status, bad_request)

    def test_default_server_status(self):
        self.assertEqual(
            thor.http.error.HttpError.server_status, (b"500", b"Internal Server Error")
        )

    def start_bad_chunk_exchange(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        exchange = [args[0] for event, args in server.events if event == "exchange"][0]
        return tcp_conn, conn, exchange

    def test_bad_chunk_during_response(self):
        tcp_conn, conn, exchange = self.start_bad_chunk_exchange()
        exchange.response_start(b"200", b"OK", [])
        exchange.response_body(b"hello")

        conn.handle_input(b"ZZZ\r\n")

        self.assertFalse(tcp_conn.tcp_connected)
        output = b"".join(tcp_conn.output)
        self.assertIn(b"HTTP/1.1 200 OK", output)
        self.assertNotIn(b"400 Bad Request", output)

    def test_bad_chunk_after_response_complete(self):
        tcp_conn, conn, exchange = self.start_bad_chunk_exchange()
        exchange.response_start(b"200", b"OK", [])
        exchange.response_body(b"hello")
        exchange.response_done([])

        conn.handle_input(b"ZZZ\r\n")

        self.assertFalse(tcp_conn.tcp_connected)
        output = b"".join(tcp_conn.output)
        self.assertEqual(output.count(b"HTTP/1.1 "), 1)
        self.assertNotIn(b"400 Bad Request", output)

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

        exchanges = [args[0] for event, args in server.events if event == "exchange"]
        self.assertEqual(len(exchanges), 1)
        self.assertTrue(tcp_conn.tcp_connected)
        self.respond(exchanges[0], b"one")
        self.assertFalse(tcp_conn.tcp_connected)
        output = b"".join(tcp_conn.output)
        self.assertIn(b"HTTP/1.1 400 Bad Request", output)
        self.assertIn(b"Too many messages to parse", output)

    def pipelined_conn(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        conn.handle_input(b"GET /one HTTP/1.1\r\nHost: example.com\r\n\r\n")
        exchanges = [args[0] for event, args in server.events if event == "exchange"]
        self.assertEqual(len(exchanges), 1)
        return tcp_conn, conn, exchanges[0]

    def test_pipelined_error_waits_for_response_in_progress(self):
        tcp_conn, conn, first = self.pipelined_conn()
        first.response_start(b"200", b"OK", [])
        first.response_body(b"one")

        conn.handle_input(b"GET /two HTTP/1.2\r\nHost: example.com\r\n\r\n")

        self.assertTrue(tcp_conn.tcp_connected)
        self.assertNotIn(b"505", b"".join(tcp_conn.output))

        first.response_done([])

        output = b"".join(tcp_conn.output)
        self.assertLess(output.index(b"HTTP/1.1 200 OK"), output.index(b"HTTP/1.1 505"))
        self.assertIn(b"one", output.split(b"HTTP/1.1 505")[0])
        self.assertFalse(tcp_conn.tcp_connected)

    def respond(self, exchange, body, **kwargs):
        exchange.response_start(
            b"200", b"OK", [(b"Content-Length", b"%i" % len(body))]
        )
        exchange.response_body(body)
        exchange.response_done([], **kwargs)

    def test_pipelined_error_waits_for_unanswered_exchange(self):
        tcp_conn, conn, first = self.pipelined_conn()

        conn.handle_input(b"GET /two HTTP/1.2\r\nHost: example.com\r\n\r\n")

        self.assertTrue(tcp_conn.tcp_connected)
        self.assertEqual(tcp_conn.output, [])
        self.respond(first, b"one")

        output = b"".join(tcp_conn.output)
        self.assertLess(output.index(b"one"), output.index(b"HTTP/1.1 505"))
        self.assertFalse(tcp_conn.tcp_connected)

    def pipelined_two_conn(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        conn.handle_input(
            b"GET /one HTTP/1.1\r\nHost: example.com\r\n\r\n"
            b"GET /two HTTP/1.1\r\nHost: example.com\r\n\r\n"
        )
        exchanges = [args[0] for event, args in server.events if event == "exchange"]
        self.assertEqual(len(exchanges), 2)
        server.loop.schedule.reset_mock()
        self.fake_server = server
        return tcp_conn, conn, exchanges

    def test_pipelined_error_follows_all_earlier_responses(self):
        tcp_conn, conn, (first, second) = self.pipelined_two_conn()

        conn.handle_input(b"GET /three HTTP/1.2\r\nHost: example.com\r\n\r\n")
        self.assertEqual(tcp_conn.output, [])

        self.respond(first, b"one")
        self.assertTrue(tcp_conn.tcp_connected)
        self.assertNotIn(b"505", b"".join(tcp_conn.output))

        self.respond(second, b"two")

        output = b"".join(tcp_conn.output)
        self.assertLess(output.index(b"one"), output.index(b"two"))
        self.assertLess(output.index(b"two"), output.index(b"HTTP/1.1 505"))
        self.assertEqual(output.count(b"HTTP/1.1 "), 3)
        self.assertFalse(tcp_conn.tcp_connected)
        self.fake_server.loop.schedule.assert_not_called()  # no stale idle timer

    def test_pipelined_error_write_failure_closes(self):
        tcp_conn, conn, (first, second) = self.pipelined_two_conn()
        conn.handle_input(b"GET /three HTTP/1.2\r\nHost: example.com\r\n\r\n")
        self.respond(first, b"one")

        def fail(data):
            raise BufferError("full")

        second.response_start(b"200", b"OK", [(b"Content-Length", b"3")])
        second.response_body(b"two")
        tcp_conn.write = fail
        second.response_done([])  # the error write fails; nothing escapes

        self.assertFalse(tcp_conn.tcp_connected)
        self.assertIsNone(conn.tcp_conn)

    def test_pipelined_error_waits_for_second_response_in_progress(self):
        tcp_conn, conn, (first, second) = self.pipelined_two_conn()
        first.response_start(b"200", b"OK", [(b"Content-Length", b"3")])
        first.response_body(b"one")

        conn.handle_input(b"GET /three HTTP/1.2\r\nHost: example.com\r\n\r\n")
        first.response_done([])

        self.assertTrue(tcp_conn.tcp_connected)
        self.assertNotIn(b"505", b"".join(tcp_conn.output))

        self.respond(second, b"two")

        output = b"".join(tcp_conn.output)
        self.assertLess(output.index(b"two"), output.index(b"HTTP/1.1 505"))
        self.assertFalse(tcp_conn.tcp_connected)

    def start_pipelined_post(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        conn.handle_input(
            b"GET /one HTTP/1.1\r\nHost: example.com\r\n\r\n"
            b"POST /two HTTP/1.1\r\nHost: example.com\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n"
        )
        exchanges = [args[0] for event, args in server.events if event == "exchange"]
        self.assertEqual(len(exchanges), 2)
        return tcp_conn, conn, exchanges

    def test_body_error_waits_for_earlier_exchange(self):
        tcp_conn, conn, (first, second) = self.start_pipelined_post()

        conn.handle_input(b"ZZZ\r\n")

        self.assertTrue(tcp_conn.tcp_connected)
        self.assertEqual(tcp_conn.output, [])
        self.respond(first, b"one")

        output = b"".join(tcp_conn.output)
        self.assertLess(output.index(b"one"), output.index(b"HTTP/1.1 400"))
        self.assertFalse(tcp_conn.tcp_connected)

    def test_body_error_in_flight_response_started_closes(self):
        # pins behaviour preserved from main, not the fix
        tcp_conn, conn, (first, second) = self.start_pipelined_post()
        self.respond(first, b"one")
        second.response_start(b"200", b"OK", [])
        second.response_body(b"two")

        conn.handle_input(b"ZZZ\r\n")

        self.assertFalse(tcp_conn.tcp_connected)
        self.assertNotIn(b"400", b"".join(tcp_conn.output))

    def test_request_done_only_app_gets_error_on_body_error(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        done = []

        def handle(event, *args):
            if event == "exchange":
                args[0].on("request_done", lambda trailers: done.append(1))

        server.emit = handle
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\nZZZ\r\n"
        )

        self.assertEqual(done, [])
        self.assertFalse(tcp_conn.tcp_connected)
        self.assertIn(b"HTTP/1.1 400 Bad Request", b"".join(tcp_conn.output))

    def test_pipelined_error_drops_when_response_closes(self):
        tcp_conn, conn, (first, second) = self.pipelined_two_conn()
        conn.handle_input(b"GET /three HTTP/1.2\r\nHost: example.com\r\n\r\n")

        self.respond(first, b"one", close=True)

        self.assertFalse(tcp_conn.tcp_connected)
        self.assertNotIn(b"505", b"".join(tcp_conn.output))

    def test_pipelined_error_discarded_if_connection_closes(self):
        # pins behaviour preserved from main, not the fix
        tcp_conn, conn, first = self.pipelined_conn()
        first.response_start(b"200", b"OK", [])

        conn.handle_input(b"GET /two HTTP/1.2\r\nHost: example.com\r\n\r\n")
        conn.conn_closed()
        output_len = len(tcp_conn.output)
        first.response_done([])

        self.assertEqual(len(tcp_conn.output), output_len)
        self.assertNotIn(b"505", b"".join(tcp_conn.output))

    def test_pipelined_error_not_sent_after_closing_response(self):
        # pins behaviour preserved from main, not the fix
        tcp_conn, conn, first = self.pipelined_conn()
        first.response_start(b"200", b"OK", [(b"Content-Length", b"0")])

        conn.handle_input(b"GET /two HTTP/1.2\r\nHost: example.com\r\n\r\n")
        first.response_done([], close=True)

        self.assertFalse(tcp_conn.tcp_connected)
        self.assertNotIn(b"505", b"".join(tcp_conn.output))

    def test_trailer_error_in_flight_exchange_responds_and_closes(self):
        # pins behaviour preserved from main, not the fix
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)

        conn.handle_input(
            b"POST / HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"3\r\nabc\r\n0\r\nbad trailer\r\n\r\n"
        )

        self.assertFalse(tcp_conn.tcp_connected)
        self.assertIn(b"HTTP/1.1 400 Bad Request", b"".join(tcp_conn.output))

    def check_bad_request(self, request, exchanges=0):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        conn.handle_input(request)
        seen = [event for event, args in server.events if event == "exchange"]
        self.assertEqual(len(seen), exchanges)
        self.assertFalse(tcp_conn.tcp_connected)
        output = b"".join(tcp_conn.output)
        self.assertIn(b"HTTP/1.1 400 Bad Request", output)

    def test_error_response_framing(self):
        "Error responses are counted and close, whatever the request's version."
        for request in [
            b"GET / HTTP/1.0\r\n\r\n",  # no Host
            b"GET / HTTP/1.1\r\n\r\n",
        ]:
            with self.subTest(request):
                tcp_conn = FakeTcpConnection()
                server = FakeServer()
                conn = HttpServerConnection(tcp_conn, server)
                conn.handle_input(request)
                output = b"".join(tcp_conn.output)
                self.assertIn(b"HTTP/1.1 400 Bad Request", output)
                self.assertIn(b"Connection: close\r\n", output)
                self.assertNotIn(b"Transfer-Encoding", output)
                headers, body = output.split(b"\r\n\r\n", 1)
                self.assertIn(b"Content-Length: %d\r\n" % len(body), headers)
                self.assertFalse(tcp_conn.tcp_connected)

    def check_listener_runtime_error(self, event):
        """
        A RuntimeError raised by a request listener must propagate to the caller
        of handle_input, not be turned into a 400 response. The body arrives in
        the same read as the headers.
        """

        class ListeningServer(FakeServer):
            def emit(self, event_name, *args):
                super().emit(event_name, *args)
                if event_name == "exchange":

                    def boom(*boom_args):
                        raise RuntimeError("listener bug")

                    args[0].on(event, boom)

        tcp_conn = FakeTcpConnection()
        conn = HttpServerConnection(tcp_conn, ListeningServer())
        with self.assertRaisesRegex(RuntimeError, "listener bug"):
            conn.handle_input(
                b"POST /one HTTP/1.1\r\n"
                b"Host: example.com\r\n"
                b"Content-Length: 2\r\n"
                b"\r\n"
                b"hi"
            )
        self.assertTrue(tcp_conn.tcp_connected)
        self.assertEqual(tcp_conn.output, [])

    def test_request_body_listener_runtime_error(self):
        self.check_listener_runtime_error("request_body")

    def test_request_done_listener_runtime_error(self):
        self.check_listener_runtime_error("request_done")

    def test_request_te_identity(self):
        self.check_bad_request(
            b"POST /one HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Transfer-Encoding: identity\r\n"
            b"\r\n"
            b"GET /two HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"\r\n"
        )

    def test_request_te_chunked_not_final(self):
        self.check_bad_request(
            b"POST /one HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Transfer-Encoding: chunked, identity\r\n"
            b"\r\n"
        )

    def test_request_te_and_cl(self):
        self.check_bad_request(
            b"POST /one HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Content-Length: 4\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"0\r\n\r\n"
        )

    def test_request_bare_cr(self):
        self.check_bad_request(
            b"POST /one HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"X-Foo: a\rTransfer-Encoding: chunked\r\n"
            b"\r\n"
        )

    def test_request_bad_chunk_size(self):
        for size in [b"0x5", b"+5", b" 5", b"5_0"]:
            with self.subTest(size=size):
                self.check_bad_request(
                    b"POST /one HTTP/1.1\r\n"
                    b"Host: example.com\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n" + size + b"\r\nhello\r\n0\r\n\r\n",
                    exchanges=1,
                )

    def test_request_te_chunked(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        conn.handle_input(
            b"POST /one HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Transfer-Encoding: identity, chunked\r\n"
            b"\r\n"
            b"0\r\n\r\n"
        )
        exchanges = [event for event, args in server.events if event == "exchange"]
        self.assertEqual(len(exchanges), 1)
        self.assertTrue(tcp_conn.tcp_connected)

    def check_missing_host_rejected(self, request):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)

        conn.handle_input(request)

        exchanges = [event for event, args in server.events if event == "exchange"]
        self.assertEqual(exchanges, [])
        self.assertFalse(tcp_conn.tcp_connected)
        output = b"".join(tcp_conn.output)
        self.assertIn(b"HTTP/1.1 400 Bad Request", output)
        self.assertIn(b"Host header required", output)

    def test_missing_host_http11_rejected(self):
        self.check_missing_host_rejected(b"GET / HTTP/1.1\r\n\r\n")

    def test_missing_host_http10_rejected(self):
        self.check_missing_host_rejected(b"GET / HTTP/1.0\r\n\r\n")

    def test_missing_host_after_valid_request_pipelined(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)

        def handle_exchange(exchange):
            @on(exchange, "request_start")
            def on_start(method, uri, headers):
                exchange.response_start(b"200", b"OK", [(b"Content-Length", b"0")])
                exchange.response_done([])

        server.emit = lambda event, *args: (
            handle_exchange(args[0]) if event == "exchange" else None
        )

        conn.handle_input(
            b"GET /one HTTP/1.1\r\nHost: example.com\r\n\r\nGET /two HTTP/1.1\r\n\r\n"
        )

        output = b"".join(tcp_conn.output)
        self.assertIn(b"HTTP/1.1 200 OK", output)
        self.assertIn(b"HTTP/1.1 400 Bad Request", output)
        self.assertLess(output.index(b"200 OK"), output.index(b"400 Bad Request"))
        self.assertFalse(tcp_conn.tcp_connected)
        self.assertIsNone(conn._idler)  # pylint: disable=protected-access

    def test_idle_timer_waits_for_exchange_queue_to_drain(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        server.loop.schedule.reset_mock()
        first = HttpServerExchange(conn, b"GET", b"/one", [], b"1.1")
        second = HttpServerExchange(conn, b"GET", b"/two", [], b"1.1")
        first.req_complete = True
        second.req_complete = True
        conn.exchanges.add(first)
        conn.exchanges.add(second)

        first.response_start(b"200", b"OK", [(b"Content-Length", b"0")])
        first.response_done([])

        server.loop.schedule.assert_not_called()

        second.response_start(b"200", b"OK", [(b"Content-Length", b"0")])
        second.response_done([])

        server.loop.schedule.assert_called_once_with(
            server.idle_timeout, conn.close_conn
        )

    def make_timer_conn(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        timers = []

        def schedule(delay, callback, *args):
            timer = MagicMock()
            timer.delay = delay
            timer.callback = callback
            timers.append(timer)
            return timer

        server.loop.schedule.side_effect = schedule
        conn = HttpServerConnection(tcp_conn, server)
        timers.clear()
        return tcp_conn, server, conn, timers

    def test_request_timer_restarts_on_data_and_stops_when_complete(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 10\r\n\r\n"
        )
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0].delay, server.request_timeout)
        self.assertEqual(timers[0].callback, conn.close_conn)
        timers[0].delete.assert_not_called()

        conn.handle_input(b"012")
        self.assertEqual(len(timers), 2)
        timers[0].delete.assert_called_once()
        timers[1].delete.assert_not_called()

        conn.handle_input(b"3456789")
        timers[1].delete.assert_called_once()
        self.assertEqual(len(timers), 2)
        self.assertTrue(tcp_conn.tcp_connected)

    def test_request_timer_expiry_closes_connection(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 10\r\n\r\n"
        )
        timers[0].callback()
        self.assertFalse(tcp_conn.tcp_connected)

    def test_request_timer_not_used_without_body(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        self.assertEqual(timers, [])

    def test_request_timer_disabled(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.request_timeout = None
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 10\r\n\r\n"
        )
        conn.handle_input(b"012")
        self.assertEqual(timers, [])

    def test_request_timer_paused_while_request_body_paused(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        tcp_conn.pause = lambda paused: None
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 10\r\n\r\n"
        )
        conn.req_body_pause(True)
        timers[0].delete.assert_called_once()
        conn.req_body_pause(False)
        self.assertEqual(len(timers), 2)

    def test_request_timer_not_rearmed_when_paused_during_input(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        tcp_conn.pause = lambda paused: None

        def handle_exchange(exchange):
            @on(exchange, "request_body")
            def on_body(chunk):
                conn.req_body_pause(True)

        server.emit = lambda event, *args: (
            handle_exchange(args[0]) if event == "exchange" else None
        )
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 10\r\n\r\n012"
        )
        self.assertTrue(all(t.delete.called for t in timers))
        conn.req_body_pause(False)
        self.assertFalse(timers[-1].delete.called)

    def test_request_timer_not_rearmed_after_close_during_input(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()

        def handle_exchange(exchange):
            @on(exchange, "request_start")
            def on_start(method, uri, headers):
                exchange.response_start(b"413", b"Content Too Large", [])
                exchange.response_done([], close=True)

        server.emit = lambda event, *args: (
            handle_exchange(args[0]) if event == "exchange" else None
        )
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 10\r\n\r\n"
        )
        self.assertIsNone(conn.tcp_conn)
        self.assertTrue(all(t.delete.called for t in timers))

    def test_request_timeout_stalled_body(self):
        def server_side(server):
            server.request_timeout = 0.2

        def client_side(client_conn, test_host, test_port):
            client_conn.sendall(
                b"POST / HTTP/1.1\r\nHost: %s:%i\r\nContent-Length: 10\r\n\r\n01"
                % (test_host, test_port)
            )
            self.assertEqual(client_conn.recv(1024), b"")
            self.loop.stop()

        self.go([server_side], [client_side])

    def test_response_timer_off_by_default(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        self.assertEqual(timers, [])

    def test_response_timer_bodiless_request(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.response_timeout = 5
        exchanges = []
        server.emit = lambda event, *args: exchanges.append(args[0])
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0].delay, 5)
        self.assertEqual(timers[0].callback, conn.close_conn)
        timers[0].delete.assert_not_called()

        exchanges[0].response_start(b"200", b"OK", [(b"Content-Length", b"0")])
        timers[0].delete.assert_not_called()  # until the response is complete
        exchanges[0].response_done([])
        timers[0].delete.assert_called_once()
        self.assertTrue(tcp_conn.tcp_connected)

    def test_response_timer_expiry_closes_connection(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.response_timeout = 5
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        timers[0].callback()
        self.assertFalse(tcp_conn.tcp_connected)
        self.assertTrue(all(t.delete.called for t in timers))

    def test_response_timer_waits_for_request_body(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.response_timeout = 5
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 4\r\n\r\n01"
        )
        self.assertEqual([t.delay for t in timers], [server.request_timeout])
        conn.handle_input(b"23")
        self.assertEqual([t.delay for t in timers], [server.request_timeout, 5])
        timers[0].delete.assert_called_once()
        timers[1].delete.assert_not_called()

    def test_response_timer_pipelined_requests_timed_in_turn(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.response_timeout = 5
        exchanges = []
        server.emit = lambda event, *args: exchanges.append(args[0])
        conn.handle_input(
            b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\n"
            b"GET /2 HTTP/1.1\r\nHost: example.com\r\n\r\n"
        )
        self.assertEqual(len(timers), 1)  # the second isn't charged yet
        exchanges[0].response_start(b"200", b"OK", [(b"Content-Length", b"0")])
        exchanges[0].response_done([])
        timers[0].delete.assert_called_once()
        self.assertEqual(len(timers), 2)
        timers[1].delete.assert_not_called()
        exchanges[1].response_start(b"200", b"OK", [(b"Content-Length", b"0")])
        exchanges[1].response_done([])
        timers[1].delete.assert_called_once()

    def test_response_timer_not_left_on_closed_connection(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.response_timeout = 5
        exchanges = []
        server.emit = lambda event, *args: exchanges.append(args[0])
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        exchanges[0].response_start(b"500", b"Error", [])
        exchanges[0].response_done([], close=True)
        self.assertIsNone(conn.tcp_conn)
        self.assertTrue(all(t.delete.called for t in timers))

    def test_response_timer_cleared_when_connection_drops(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.response_timeout = 5
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        conn.conn_closed()
        self.assertTrue(all(t.delete.called for t in timers))

    def test_request_timer_covers_partial_headers(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        exchanges = []
        server.emit = lambda event, *args: exchanges.append(args[0])
        conn.handle_input(b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\nGET /2 HT")
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0].delay, server.request_timeout)
        self.assertEqual(timers[0].callback, conn.close_conn)

        conn.handle_input(b"TP/1.1\r\nHost: exa")
        self.assertEqual(len(timers), 2)
        timers[0].delete.assert_called_once()
        timers[1].delete.assert_not_called()

        conn.handle_input(b"mple.com\r\n\r\n")  # headers complete; no body
        timers[1].delete.assert_called_once()
        self.assertEqual(len(timers), 2)
        self.assertEqual(len(exchanges), 2)

    def test_request_timer_partial_headers_then_body(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        conn.handle_input(b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-")
        conn.handle_input(b"Length: 4\r\n\r\n")
        # the body timer takes over from the header timer
        self.assertEqual(len(timers), 2)
        timers[0].delete.assert_called_once()
        timers[1].delete.assert_not_called()
        conn.handle_input(b"0123")
        timers[1].delete.assert_called_once()

    def test_request_timer_partial_headers_after_body(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 4\r\n\r\n"
            b"0123GET /2 HT"
        )
        self.assertEqual(sum(1 for t in timers if not t.delete.called), 1)

    def test_request_timer_partial_headers_paused(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        tcp_conn.pause = lambda paused: None
        conn.handle_input(b"GET / HTTP/1.1\r\nHo")
        conn.req_body_pause(True)
        timers[0].delete.assert_called_once()
        conn.req_body_pause(False)
        self.assertEqual(len(timers), 2)
        timers[1].delete.assert_not_called()

    def test_request_timer_partial_headers_not_left_on_closed_connection(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        conn.handle_input(b"GET / HTTP/1.1\r\nHo")
        conn.conn_closed()
        self.assertTrue(all(t.delete.called for t in timers))

    def test_request_timer_stopped_after_header_error(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        conn.handle_input(b"GET / HTTP/1.1\r\nHo")
        conn.handle_input(b"st: example.com\r\nBad Header\r\n\r\nGET / HTTP/1.1\r\nHo")
        self.assertFalse(tcp_conn.tcp_connected)
        self.assertTrue(all(t.delete.called for t in timers))

    def test_request_timer_stopped_after_deferred_header_error(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        conn.handle_input(b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\nGET /2 HT")
        self.assertEqual(sum(1 for t in timers if not t.delete.called), 1)
        conn.handle_input(b"TP/1.1\r\nHost: x\r\nBad Header\r\n\r\n")
        self.assertTrue(tcp_conn.tcp_connected)  # error waits for /1's response
        self.assertTrue(all(t.delete.called for t in timers))

    def make_idle_conn(self, sync_paths=None):
        "A timer conn whose app answers inside request_start for sync_paths."
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.idle_timeout = 7  # distinct from the other timeouts
        exchanges = []

        def handle_exchange(exchange):
            exchanges.append(exchange)

            @on(exchange, "request_start")
            def on_start(method, uri, headers):
                if sync_paths is None or uri in sync_paths:
                    self.respond(exchange, b"")

        server.emit = lambda event, *args: (
            handle_exchange(args[0]) if event == "exchange" else None
        )
        return tcp_conn, server, conn, timers, exchanges

    def idle_timers(self, server, timers):
        "The idle timers that haven't been deleted."
        return [
            t for t in timers if t.delay == server.idle_timeout and not t.delete.called
        ]

    def test_idle_timer_armed_after_synchronous_response(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        self.assertTrue(tcp_conn.tcp_connected)
        self.assertEqual(len([t for t in timers if t.delay == 7]), 1)
        live = self.idle_timers(server, timers)
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].callback, conn.close_conn)
        self.assertIs(conn._idler, live[0])  # pylint: disable=protected-access

    def test_idle_timer_armed_after_response_following_request(self):
        tcp_conn, server, conn, timers, exchanges = self.make_idle_conn(sync_paths=[])
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        self.assertEqual(self.idle_timers(server, timers), [])
        self.respond(exchanges[0], b"")
        self.assertEqual(len(self.idle_timers(server, timers)), 1)
        self.assertTrue(tcp_conn.tcp_connected)

    def test_idle_timer_armed_when_response_precedes_request_body(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 4\r\n\r\n"
        )
        conn.handle_input(b"01")
        self.assertEqual(self.idle_timers(server, timers), [])
        conn.handle_input(b"23")
        self.assertEqual(len(self.idle_timers(server, timers)), 1)
        self.assertEqual(len([t for t in timers if t.delay == 7]), 1)
        self.assertEqual([t for t in timers if not t.delete.called], timers[-1:])
        self.assertTrue(tcp_conn.tcp_connected)

    def test_idle_timer_armed_when_response_follows_request_body(self):
        tcp_conn, server, conn, timers, exchanges = self.make_idle_conn(sync_paths=[])
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 4\r\n\r\n0123"
        )
        self.assertEqual(self.idle_timers(server, timers), [])
        self.respond(exchanges[0], b"")
        self.assertEqual(len(self.idle_timers(server, timers)), 1)

    def test_idle_timer_waits_for_last_pipelined_request(self):
        tcp_conn, server, conn, timers, exchanges = self.make_idle_conn(
            sync_paths=[b"/1"]
        )
        conn.handle_input(
            b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\n"
            b"GET /2 HTTP/1.1\r\nHost: example.com\r\n\r\n"
        )
        self.assertEqual(self.idle_timers(server, timers), [])
        self.respond(exchanges[1], b"")
        self.assertEqual(len(self.idle_timers(server, timers)), 1)

    def test_idle_timer_pipelined_synchronous_responses(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(
            b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\n"
            b"GET /2 HTTP/1.1\r\nHost: example.com\r\n\r\n"
        )
        self.assertEqual(len(self.idle_timers(server, timers)), 1)
        self.assertTrue(tcp_conn.tcp_connected)

    def test_idle_timer_http10_keep_alive(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(
            b"GET / HTTP/1.0\r\nHost: example.com\r\nConnection: keep-alive\r\n\r\n"
        )
        self.assertTrue(tcp_conn.tcp_connected)
        self.assertEqual(len(self.idle_timers(server, timers)), 1)

    def test_idle_timer_not_armed_for_http10_close(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(b"GET / HTTP/1.0\r\nHost: example.com\r\n\r\n")
        self.assertFalse(tcp_conn.tcp_connected)
        self.assertEqual(self.idle_timers(server, timers), [])
        self.assertIsNone(conn._idler)  # pylint: disable=protected-access

    def test_idle_connection_closed_when_shutting_down(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        server.shutting_down = True
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        self.assertFalse(tcp_conn.tcp_connected)
        self.assertEqual(self.idle_timers(server, timers), [])

    def test_idle_connection_closed_when_shutting_down_after_request_body(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        server.shutting_down = True
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 4\r\n\r\n01"
        )
        self.assertTrue(tcp_conn.tcp_connected)  # the request isn't finished
        conn.handle_input(b"23")
        self.assertFalse(tcp_conn.tcp_connected)
        self.assertEqual(self.idle_timers(server, timers), [])

    def test_idle_timer_not_armed_when_response_closes(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.idle_timeout = 7
        exchanges = []
        server.emit = lambda event, *args: exchanges.append(args[0])
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        self.respond(exchanges[0], b"", close=True)
        self.assertFalse(tcp_conn.tcp_connected)
        self.assertEqual(self.idle_timers(server, timers), [])

    def test_idle_timer_deleted_when_connection_closes(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        self.assertEqual(len(self.idle_timers(server, timers)), 1)
        conn.close_conn()
        self.assertEqual(self.idle_timers(server, timers), [])
        self.assertIsNone(conn._idler)  # pylint: disable=protected-access

    def test_idle_timer_deleted_when_connection_drops(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        conn.conn_closed()
        self.assertEqual(self.idle_timers(server, timers), [])
        self.assertIsNone(conn._idler)  # pylint: disable=protected-access

    def test_idle_timer_removed_by_next_request(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        conn.handle_input(b"GET /2 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        self.assertEqual(len(self.idle_timers(server, timers)), 1)
        self.assertEqual(len([t for t in timers if t.delay == 7]), 2)

    def test_idle_timeout_after_synchronous_response(self):
        def server_side(server):
            server.idle_timeout = 0.3

            def handle_exchange(exchange):
                @on(exchange, "request_start")
                def on_start(method, uri, headers):
                    self.respond(exchange, b"OK")

            server.on("exchange", handle_exchange)

        def client_side(client_conn, test_host, test_port):
            client_conn.settimeout(10)
            client_conn.sendall(
                b"GET / HTTP/1.1\r\nHost: %s:%i\r\n\r\n" % (test_host, test_port)
            )
            self.assertIn(b"200 OK", client_conn.recv(1024))
            self.assertEqual(client_conn.recv(1024), b"")  # closed when idle
            self.loop.stop()

        self.go([server_side], [client_side])

    def live_delays(self, timers):
        "The delays of the timers that haven't been deleted."
        return sorted(t.delay for t in timers if not t.delete.called)

    def test_idle_timer_is_total_deadline_for_next_header_block(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        idler = conn._idler  # pylint: disable=protected-access
        self.assertEqual(self.live_delays(timers), [server.idle_timeout])
        both = [server.idle_timeout, server.request_timeout]
        for piece in [b"GET /2 HTTP/1.1\r\nHo", b"st: example.com\r\n"]:
            conn.handle_input(piece)
            self.assertEqual(self.live_delays(timers), both)
            self.assertIs(conn._idler, idler)  # pylint: disable=protected-access
            idler.delete.assert_not_called()
        conn.handle_input(b"\r\n")
        idler.delete.assert_called_once()
        self.assertEqual(self.live_delays(timers), [server.idle_timeout])
        self.assertTrue(tcp_conn.tcp_connected)

    def test_idle_timer_cuts_trickled_header_block(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        conn.handle_input(b"GET /2 HTTP/1.1\r\nHo")
        conn._idler.callback()  # pylint: disable=protected-access
        self.assertFalse(tcp_conn.tcp_connected)
        self.assertEqual(self.live_delays(timers), [])

    def test_request_timer_cuts_stalled_header_block_first(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        conn.handle_input(b"GET /2 HTTP/1.1\r\nHo")
        (req_timer,) = [t for t in timers if t.delay == server.request_timeout]
        req_timer.callback()
        self.assertFalse(tcp_conn.tcp_connected)
        self.assertEqual(self.live_delays(timers), [])

    def test_idle_timer_runs_with_partial_request_in_same_read(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(
            b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\nGET /2 HTTP/1.1\r\nHo"
        )
        self.assertEqual(
            self.live_delays(timers), [server.idle_timeout, server.request_timeout]
        )
        conn.handle_input(b"st: example.com\r\n\r\n")
        self.assertEqual(self.live_delays(timers), [server.idle_timeout])

    def test_idle_timer_not_running_during_request_body(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn(sync_paths=[])
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 4\r\n\r\n0"
        )
        self.assertEqual(self.live_delays(timers), [server.request_timeout])
        conn.handle_input(b"1")
        self.assertEqual(self.live_delays(timers), [server.request_timeout])

    def test_idle_timer_stays_when_request_timeout_disabled(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        server.request_timeout = None
        conn.handle_input(b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        conn.handle_input(b"GET /2 HTTP/1.1\r\nHo")
        self.assertEqual(self.live_delays(timers), [server.idle_timeout])

    def test_idle_timer_after_partial_request_behind_pending_exchange(self):
        tcp_conn, server, conn, timers, exchanges = self.make_idle_conn(sync_paths=[])
        conn.handle_input(
            b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\nGET /2 HTTP/1.1\r\nHo"
        )
        self.assertEqual(self.live_delays(timers), [server.request_timeout])
        self.respond(exchanges[0], b"")
        self.assertEqual(
            self.live_delays(timers), [server.idle_timeout, server.request_timeout]
        )

    def test_idle_timer_not_reset_by_blank_lines(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        conn.handle_input(b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        conn.handle_input(b"\r\n")
        self.assertEqual(len([t for t in timers if t.delay == 7]), 1)
        self.assertEqual(self.live_delays(timers), [server.idle_timeout])

    def test_shutdown_closes_idle_connection_with_partial_request(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        server.shutting_down = True
        conn.handle_input(
            b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\nGET /2 HTTP/1.1\r\nHo"
        )
        self.assertFalse(tcp_conn.tcp_connected)
        self.assertEqual(self.live_delays(timers), [])

    def test_idle_connection_waits_for_pipelined_requests_when_shutting_down(self):
        tcp_conn, server, conn, timers, exchanges = self.make_idle_conn()
        server.shutting_down = True
        conn.handle_input(
            b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\n"
            b"GET /2 HTTP/1.1\r\nHost: example.com\r\n\r\n"
        )
        self.assertEqual(len(exchanges), 2)
        self.assertEqual(b"".join(tcp_conn.output).count(b"HTTP/1.1 200 OK"), 2)
        self.assertFalse(tcp_conn.tcp_connected)

    def test_idle_timer_disabled(self):
        for value in [None, 0]:
            with self.subTest(value=value):
                tcp_conn = FakeTcpConnection()
                server = FakeServer()
                server.idle_timeout = value
                conn = HttpServerConnection(tcp_conn, server)
                exchanges = []
                server.emit = lambda event, *args: exchanges.append(args[0])
                conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
                self.respond(exchanges[0], b"")
                server.loop.schedule.assert_not_called()
                self.assertTrue(tcp_conn.tcp_connected)

    def test_idle_timer_runs_for_paused_request_with_partial_next_request(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn(sync_paths=[])
        tcp_conn.pause = lambda paused: None

        def handle_exchange(exchange):
            @on(exchange, "request_body")
            def on_body(chunk):
                conn.req_body_pause(True)

            @on(exchange, "request_done")
            def on_done(trailers):
                self.respond(exchange, b"")

        server.emit = lambda event, *args: (
            handle_exchange(args[0]) if event == "exchange" else None
        )
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 4\r\n\r\n"
            b"abcdGET /2 HT"
        )
        self.assertEqual(self.live_delays(timers), [server.idle_timeout])
        conn.req_body_pause(False)
        self.assertEqual(
            self.live_delays(timers), [server.idle_timeout, server.request_timeout]
        )
        conn._idler.callback()  # pylint: disable=protected-access
        self.assertFalse(tcp_conn.tcp_connected)
        self.assertEqual(self.live_delays(timers), [])

    def test_idle_timer_runs_on_unpause_without_request_timeout(self):
        tcp_conn, server, conn, timers, _ = self.make_idle_conn()
        tcp_conn.pause = lambda paused: None
        server.request_timeout = None
        conn.handle_input(b"GET /1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        conn.handle_input(b"GET /2 HTTP/1.1\r\nHo")
        conn.req_body_pause(True)
        self.assertEqual(self.live_delays(timers), [server.idle_timeout])
        conn.req_body_pause(False)
        self.assertEqual(self.live_delays(timers), [server.idle_timeout])
        conn._idler.callback()  # pylint: disable=protected-access
        self.assertFalse(tcp_conn.tcp_connected)

    def check_timer_invariants(self, server, conn, timers, history):
        "What an open connection's timers must look like at rest."
        live = [t for t in timers if not t.delete.called]
        idle = [t for t in live if t.delay == server.idle_timeout]
        msg = repr(history)
        if conn.exchanges:  # a request or response is in progress
            self.assertEqual(idle, [], msg)
            return
        # idle, or still receiving the next header block
        self.assertEqual(len(idle), 1, msg)
        self.assertIs(conn._idler, idle[0], msg)  # pylint: disable=protected-access

    def run_timer_sequence(self, request_timeout, requests, history):
        tcp_conn = FakeTcpConnection()
        tcp_conn.pause = lambda paused: None
        server = FakeServer()
        server.idle_timeout = 7
        server.request_timeout = request_timeout
        timers = []

        def schedule(delay, callback, *args):
            timer = MagicMock()
            timer.delay = delay
            timer.callback = callback
            timers.append(timer)
            return timer

        server.loop.schedule.side_effect = schedule
        conn = HttpServerConnection(tcp_conn, server)
        pending = []

        def handle_exchange(exchange):
            @on(exchange, "request_start")
            def on_start(method, uri, headers):
                if uri == b"/sync":
                    self.respond(exchange, b"")
                else:
                    pending.append(exchange)

        server.emit = lambda event, *args: (
            handle_exchange(args[0]) if event == "exchange" else None
        )
        self.check_timer_invariants(server, conn, timers, ())
        for i, op in enumerate(history):
            if op == "answer":
                if not pending:
                    continue
                self.respond(pending.pop(0), b"")
            elif op in ("pause", "unpause"):
                conn.req_body_pause(op == "pause")
            else:
                conn.handle_input(requests[op])
            if not tcp_conn.tcp_connected:
                return
            self.check_timer_invariants(server, conn, timers, history[: i + 1])

    def test_timers_stay_consistent_over_event_sequences(self):
        requests = {
            "sync": b"GET /sync HTTP/1.1\r\nHost: x\r\n\r\n",
            "async": b"GET /async HTTP/1.1\r\nHost: x\r\n\r\n",
            "partial": b"GET /sync HTTP/1.1\r\nHo",
            "rest": b"st: x\r\n\r\n",
            "post": b"POST /sync HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\n01",
            "body": b"23",
        }
        ops = list(requests) + ["answer", "pause", "unpause"]
        for request_timeout in [30, None]:
            for length in range(1, 4):
                for history in itertools.product(ops, repeat=length):
                    self.run_timer_sequence(request_timeout, requests, history)

    def test_idle_timeout_cuts_trickled_header_block(self):
        def server_side(server):
            server.idle_timeout = 0.5
            server.request_timeout = 5  # a trickle never trips this

            def handle_exchange(exchange):
                @on(exchange, "request_start")
                def on_start(method, uri, headers):
                    self.respond(exchange, b"OK")

            server.on("exchange", handle_exchange)

        def client_side(client_conn, test_host, test_port):
            client_conn.settimeout(5)
            host = b"Host: %s:%i\r\n" % (test_host, test_port)
            client_conn.sendall(b"GET /1 HTTP/1.1\r\n" + host + b"\r\n")
            self.assertIn(b"200 OK", client_conn.recv(1024))
            client_conn.settimeout(0.2)  # paces the trickle
            pieces = [b"GET /2 HTTP/1.1\r\n", host] + [b"X-A: 1\r\n"] * 24
            closed = False
            for piece in pieces:
                try:
                    client_conn.sendall(piece)
                    closed = client_conn.recv(1024) == b""
                except socket.timeout:
                    pass
                except OSError:  # reset
                    closed = True
                if closed:
                    break
            self.assertTrue(closed)
            self.loop.stop()

        self.go([server_side], [client_side])

    def test_idle_timeout_not_applied_to_trickled_request_body(self):
        def server_side(server):
            server.idle_timeout = 0.5
            server.request_timeout = 5

            def handle_exchange(exchange):
                @on(exchange, "request_done")
                def on_done(trailers):
                    self.respond(exchange, b"OK")

            server.on("exchange", handle_exchange)

        def client_side(client_conn, test_host, test_port):
            client_conn.settimeout(10)
            client_conn.sendall(
                b"POST / HTTP/1.1\r\nHost: %s:%i\r\nContent-Length: 6\r\n\r\n"
                % (test_host, test_port)
            )
            for byte in b"012345":  # slower overall than idle_timeout
                time.sleep(0.2)
                client_conn.sendall(bytes([byte]))
            self.assertIn(b"200 OK", client_conn.recv(1024))
            self.assertEqual(client_conn.recv(1024), b"")  # idle at last
            self.loop.stop()

        self.go([server_side], [client_side])

    def test_response_timeout_unanswered_request(self):
        def server_side(server):
            server.response_timeout = 0.2

        def client_side(client_conn, test_host, test_port):
            client_conn.settimeout(10)
            client_conn.sendall(
                b"GET / HTTP/1.1\r\nHost: %s:%i\r\n\r\n" % (test_host, test_port)
            )
            self.assertEqual(client_conn.recv(1024), b"")
            self.loop.stop()

        self.go([server_side], [client_side])

    def test_request_timeout_partial_headers_behind_pending_exchange(self):
        def server_side(server):
            server.request_timeout = 0.2

        def client_side(client_conn, test_host, test_port):
            client_conn.settimeout(10)
            client_conn.sendall(
                b"GET /1 HTTP/1.1\r\nHost: %s:%i\r\n\r\nGET /2 HTTP/1.1\r\nHo"
                % (test_host, test_port)
            )
            self.assertEqual(client_conn.recv(1024), b"")
            self.loop.stop()

        self.go([server_side], [client_side])

    def test_request_timer_ignores_stray_blank_line(self):
        for request in [
            b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n",
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 2\r\n\r\nab",
        ]:
            for tail in [[b"\r\n"], [b"\n"], [b"", b"\r\n"]]:
                tcp_conn, server, conn, timers = self.make_timer_conn()
                for chunk in [request + tail[0]] + tail[1:]:
                    conn.handle_input(chunk)
                self.assertTrue(all(t.delete.called for t in timers), (request, tail))

    def test_request_timer_one_timer_for_byte_by_byte_headers(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        request = b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n"
        for i in range(len(request) - 1):
            conn.handle_input(request[i : i + 1])
            self.assertEqual(sum(1 for t in timers if not t.delete.called), 1)
        conn.handle_input(request[-1:])
        self.assertTrue(all(t.delete.called for t in timers))

    def test_request_timer_covers_wait_for_100_continue(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.response_timeout = 5
        conn.handle_input(
            b"POST / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 4\r\n"
            b"Expect: 100-continue\r\n\r\n"
        )
        self.assertEqual([t.delay for t in timers], [server.request_timeout])
        timers[0].callback()
        self.assertFalse(tcp_conn.tcp_connected)

    def test_response_timer_not_extended_by_response_start(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.response_timeout = 5
        exchanges = []
        server.emit = lambda event, *args: exchanges.append(args[0])
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        exchanges[0].response_start(b"200", b"OK", [])
        exchanges[0].response_body(b"partial")
        self.assertEqual(len(timers), 1)
        timers[0].delete.assert_not_called()
        timers[0].callback()
        self.assertFalse(tcp_conn.tcp_connected)

    def test_response_timer_waits_for_request_start_when_output_paused(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.response_timeout = 5
        conn.res_body_pause(True)
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
        self.assertEqual(timers, [])
        conn.res_body_pause(False)
        self.assertEqual([t.delay for t in timers], [5])
        self.assertEqual(timers[0].callback, conn.close_conn)

    def test_timeouts_disabled_by_negative_values(self):
        tcp_conn, server, conn, timers = self.make_timer_conn()
        server.request_timeout = -1
        server.response_timeout = -1
        conn.handle_input(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\nGET /2 HT")
        conn.handle_input(b"TP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\n01")
        self.assertEqual(timers, [])

    def test_response_timeout_cuts_streaming_response(self):
        def server_side(server):
            server.response_timeout = 0.2

            @on(server)
            def exchange(exchange):
                @on(exchange)
                def request_start(method, uri, headers):
                    exchange.response_start(b"200", b"OK", [])
                    exchange.response_body(b"abc")

        def client_side(client_conn, test_host, test_port):
            client_conn.settimeout(10)
            client_conn.sendall(
                b"GET / HTTP/1.1\r\nHost: %s:%i\r\n\r\n" % (test_host, test_port)
            )
            data = b""
            while True:
                chunk = client_conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            self.assertTrue(data.startswith(b"HTTP/1.1 200 OK"))
            self.assertNotIn(b"0\r\n\r\n", data)
            self.loop.stop()

        self.go([server_side], [client_side])

    def test_response_done_close_closes_tcp(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        exchange = HttpServerExchange(conn, b"POST", b"/", [], b"1.1")
        conn.exchanges.add(exchange)

        exchange.response_start(b"413", b"Content Too Large", [])
        exchange.response_body(b"too big")
        exchange.response_done([], close=True)

        self.assertFalse(tcp_conn.tcp_connected)
        self.assertIsNone(conn.tcp_conn)

    def test_close_conn_forgets_a_connection_that_is_already_gone(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        tcp_conn.tcp_connected = False

        conn.close_conn()

        self.assertIsNone(conn.tcp_conn)

    def test_response_done_default_keeps_connection(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        exchange = HttpServerExchange(conn, b"GET", b"/", [], b"1.1")
        exchange.req_complete = True
        conn.exchanges.add(exchange)

        exchange.response_start(b"200", b"OK", [(b"Content-Length", b"0")])
        exchange.response_done([])

        self.assertTrue(tcp_conn.tcp_connected)

    def check_connection_handling(self, request, closes, conn_header):
        "Send a GET request through a fake connection and check its fate."
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        seen = []

        def handle_exchange(exchange):
            seen.append(exchange.uri)

            @on(exchange, "request_start")
            def start(method, uri, headers):
                exchange.response_start(b"200", b"OK", [(b"Content-Length", b"3")])
                exchange.response_body(b"foo")
                exchange.response_done([])

        server.emit = lambda event, *args: (
            handle_exchange(args[0]) if event == "exchange" else None
        )

        conn.handle_input(request + b"GET /two HTTP/1.1\r\nHost: example.com\r\n\r\n")

        output = b"".join(tcp_conn.output)
        self.assertEqual(tcp_conn.tcp_connected, not closes)
        self.assertEqual(seen, [b"/one"] if closes else [b"/one", b"/two"])
        if conn_header is None:
            self.assertNotIn(b"Connection:", output)
        else:
            self.assertIn(b"Connection: " + conn_header + b"\r\n", output)

    def test_http11_connection_close(self):
        self.check_connection_handling(
            b"GET /one HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n",
            True,
            b"close",
        )

    def test_http11_default_persistent(self):
        self.check_connection_handling(
            b"GET /one HTTP/1.1\r\nHost: example.com\r\n\r\n", False, None
        )

    def test_http10_default_closes(self):
        self.check_connection_handling(
            b"GET /one HTTP/1.0\r\nHost: example.com\r\n\r\n", True, b"close"
        )

    def test_http10_keep_alive_without_length_closes(self):
        tcp_conn = FakeTcpConnection()
        conn = HttpServerConnection(tcp_conn, FakeServer())

        def handle_exchange(exchange):
            @on(exchange, "request_start")
            def start(method, uri, headers):
                exchange.response_start(b"200", b"OK", [])
                exchange.response_body(b"foo")
                exchange.response_done([])

        conn.server.emit = lambda event, *args: (
            handle_exchange(args[0]) if event == "exchange" else None
        )
        conn.handle_input(
            b"GET / HTTP/1.0\r\nHost: example.com\r\n"
            b"Connection: keep-alive\r\n\r\n"
        )

        self.assertFalse(tcp_conn.tcp_connected)
        self.assertIn(b"Connection: close\r\n", b"".join(tcp_conn.output))

    def test_connection_token_list_close_wins(self):
        self.check_connection_handling(
            b"GET /one HTTP/1.1\r\nHost: example.com\r\n"
            b"Connection: Keep-Alive, Close\r\n\r\n",
            True,
            b"close",
        )

    def test_post_connection_close_ignores_trailing_bytes(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        seen = []

        def handle_exchange(exchange):
            seen.append(exchange.uri)

            @on(exchange, "request_done")
            def done(trailers):
                exchange.response_start(b"200", b"OK", [(b"Content-Length", b"0")])
                exchange.response_done([])

        server.emit = lambda event, *args: (
            handle_exchange(args[0]) if event == "exchange" else None
        )
        conn.handle_input(
            b"POST /one HTTP/1.1\r\nHost: example.com\r\n"
            b"Connection: close\r\nContent-Length: 5\r\n\r\n12345"
            b"GET /two HTTP/1.1\r\nHost: example.com\r\n\r\n"
        )

        self.assertEqual(seen, [b"/one"])
        self.assertFalse(tcp_conn.tcp_connected)
        output = b"".join(tcp_conn.output)
        self.assertEqual(output.count(b"HTTP/1.1 200"), 1)
        self.assertNotIn(b"400", output)

    def test_http10_keep_alive(self):
        self.check_connection_handling(
            b"GET /one HTTP/1.0\r\nHost: example.com\r\n"
            b"Connection: keep-alive\r\n\r\n",
            False,
            b"keep-alive",
        )

    def test_short_counted_response_closes_connection(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        exchange = HttpServerExchange(conn, b"GET", b"/", [], b"1.1")
        exchange.req_complete = True
        conn.exchanges.add(exchange)

        exchange.response_start(b"200", b"OK", [(b"Content-Length", b"5")])
        exchange.response_body(b"abc")
        exchange.response_done([])

        self.assertFalse(tcp_conn.tcp_connected)

    def check_bodiless_pipeline(self, method, status, phrase):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)

        def handle_exchange(exchange):
            @on(exchange, "request_done")
            def on_done(trailers):
                if exchange.uri == b"/one":
                    exchange.response_start(
                        status, phrase, [(b"Content-Length", b"10")]
                    )
                else:
                    exchange.response_start(b"200", b"OK", [(b"Content-Length", b"2")])
                    exchange.response_body(b"ok")
                exchange.response_done([])

        server.emit = lambda event, *args: (
            handle_exchange(args[0]) if event == "exchange" else None
        )

        conn.handle_input(
            b"%s /one HTTP/1.1\r\nHost: example.com\r\n\r\n"
            b"GET /two HTTP/1.1\r\nHost: example.com\r\n\r\n" % method
        )

        self.assertTrue(tcp_conn.tcp_connected)
        output = b"".join(tcp_conn.output)
        self.assertIn(b"HTTP/1.1 %s %s" % (status, phrase), output)
        self.assertTrue(output.endswith(b"ok"), output)

    def test_head_response_with_content_length_keeps_connection(self):
        self.check_bodiless_pipeline(b"HEAD", b"200", b"OK")

    def test_head_response_body_is_not_sent(self):
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        exchange = HttpServerExchange(conn, b"HEAD", b"/", [], b"1.1")
        exchange.req_complete = True
        conn.exchanges.add(exchange)

        exchange.response_start(b"200", b"OK", [(b"Content-Length", b"3")])
        exchange.response_body(b"abcdefgh")
        exchange.response_done([])

        self.assertNotIn(b"abcdefgh", b"".join(tcp_conn.output))
        self.assertTrue(tcp_conn.tcp_connected)

    def test_304_response_with_content_length_keeps_connection(self):
        self.check_bodiless_pipeline(b"GET", b"304", b"Not Modified")

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

    def make_pipeline_conn(self, on_start):
        """
        A connection with output paused and three pipelined requests queued.
        on_start(exchange, index) runs when the application is told about a
        request; the uris of those requests are collected in started.
        """
        tcp_conn = FakeTcpConnection()
        server = FakeServer()
        conn = HttpServerConnection(tcp_conn, server)
        exchanges = []
        started = []

        def handle_exchange(exchange):
            index = len(exchanges)
            exchanges.append(exchange)

            @on(exchange, "request_start")
            def request_start(method, uri, headers):
                started.append(uri)
                on_start(exchange, index)

        server.emit = lambda event, *args: (
            handle_exchange(args[0]) if event == "exchange" else None
        )
        conn.res_body_pause(True)
        for i in range(3):
            conn.handle_input(b"GET /%i HTTP/1.1\r\nHost: example.com\r\n\r\n" % i)
        self.assertEqual(started, [])
        self.assertEqual(len(conn.exchanges), 3)
        return tcp_conn, conn, exchanges, started

    @staticmethod
    def answer(exchange):
        exchange.response_start(
            b"200", b"OK", [(b"Content-Length", b"0"), (b"X-Uri", exchange.uri)]
        )
        exchange.response_done([])

    def test_drain_exchange_queue_synchronous_handlers(self):
        tcp_conn, conn, exchanges, started = self.make_pipeline_conn(
            lambda exchange, index: self.answer(exchange)
        )
        conn.res_body_pause(False)
        self.assertEqual(started, [b"/0", b"/1", b"/2"])
        self.assertEqual(list(conn.exchanges), [])
        output = b"".join(tcp_conn.output)
        self.assertEqual(output.count(b"HTTP/1.1 200 OK"), 3)
        positions = [output.index(b"X-Uri: /%i" % i) for i in range(3)]
        self.assertEqual(positions, sorted(positions))

    def test_drain_exchange_queue_mixed_handlers(self):
        def on_start(exchange, index):
            if index != 1:
                self.answer(exchange)

        tcp_conn, conn, exchanges, started = self.make_pipeline_conn(on_start)
        conn.res_body_pause(False)
        self.assertEqual(started, [b"/0", b"/1", b"/2"])
        self.assertEqual(list(conn.exchanges), [exchanges[1]])
        self.answer(exchanges[1])
        self.assertEqual(list(conn.exchanges), [])

    def test_drain_exchange_queue_stops_when_connection_closed(self):
        def on_start(exchange, index):
            exchange.response_start(b"200", b"OK", [(b"Content-Length", b"0")])
            exchange.response_done([], close=True)

        tcp_conn, conn, exchanges, started = self.make_pipeline_conn(on_start)
        conn.res_body_pause(False)
        self.assertEqual(started, [b"/0"])
        self.assertFalse(tcp_conn.tcp_connected)

    def test_drain_exchange_queue_stops_when_connection_lost(self):
        tcp_conn, conn, exchanges, started = self.make_pipeline_conn(
            lambda exchange, index: conn.conn_closed()
        )
        conn.res_body_pause(False)
        self.assertEqual(started, [b"/0"])

    def test_drain_exchange_queue_skips_exchanges_answered_meanwhile(self):
        def on_start(exchange, index):
            if index == 0:
                self.answer(exchanges[1])  # answered without being started
            self.answer(exchange)

        tcp_conn, conn, exchanges, started = self.make_pipeline_conn(on_start)
        conn.res_body_pause(False)
        self.assertEqual(started, [b"/0", b"/2"])
        self.assertEqual(list(conn.exchanges), [])

    def test_drain_exchange_queue_stops_when_output_pauses_again(self):
        def on_start(exchange, index):
            self.answer(exchange)
            conn.res_body_pause(True)  # e.g., the response filled the buffer

        tcp_conn, conn, exchanges, started = self.make_pipeline_conn(on_start)
        conn.res_body_pause(False)
        self.assertEqual(started, [b"/0"])
        self.assertEqual(list(conn.exchanges), exchanges[1:])
        conn.res_body_pause(False)
        self.assertEqual(started, [b"/0", b"/1"])
        conn.res_body_pause(False)
        self.assertEqual(started, [b"/0", b"/1", b"/2"])
        self.assertEqual(list(conn.exchanges), [])


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
