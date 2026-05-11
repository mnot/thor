#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import unittest

from framework import DummyHttpParser
from thor.http.common import Delimiters, States

import thor.http.error as error


def wire(data):
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


class TestHttpParser(unittest.TestCase):
    def setUp(self):
        self.parser = DummyHttpParser()

    def checkSingleMsg(self, inputs, body, expected_err=None, close=False):
        """
        Check a single HTTP message.
        """
        assert type(inputs) == type([])
        for chunk in inputs:
            chunk = chunk % {b"body": body, b"body_len": len(body)}
            self.parser.handle_input(wire(chunk))
        states = self.parser.test_states

        if not expected_err:
            self.assertFalse("ERROR" in states, self.parser.test_err)
            self.assertTrue(states.count("START") == 1, states)
            self.assertTrue(states.index("START") < states.index("BODY"))
            if close:
                self.assertEqual(self.parser._input_delimit, Delimiters.CLOSE)
            else:
                self.assertTrue(states.index("END") + 1 == len(states))
            self.assertEqual(
                body,
                self.parser.test_body,
                f"{body[:20]} not equal to {self.parser.test_body[:20]}",
            )
        else:
            self.assertTrue("ERROR" in states, states)
            self.assertEqual(self.parser.test_err.__class__, expected_err)

    def checkMultiMsg(self, inputs, body, count):
        """
        Check pipelined messages. Assumes the same body for each (for now).
        """
        for chunk in inputs:
            chunk = chunk % {b"body": body, b"body_len": len(body)}
            self.parser.handle_input(wire(chunk))
        states = self.parser.test_states
        self.assertFalse("ERROR" in self.parser.test_states, self.parser.test_err)
        self.parser.check(self, {"states": ["START", "BODY", "END"] * count})

    def test_hdrs(self):
        body = b"12345678901234567890"
        self.checkSingleMsg(
            [
                b"""\
http/1.1 200 OK
Content-Type: text/plain
Foo: bar
Content-Length: %(body_len)i
Foo: baz, bam

%(body)s"""
            ],
            body,
        )
        self.parser.check(
            self,
            {
                "hdrs": [
                    (b"Content-Type", b" text/plain"),
                    (b"Foo", b" bar"),
                    (b"Content-Length", b" %i" % len(body)),
                    (b"Foo", b" baz, bam"),
                ]
            },
        )

    def test_hdrs_nocolon(self):
        body = b"12345678901234567890"
        self.checkSingleMsg(
            [
                b"""\
http/1.1 200 OK
Content-Type: text/plain
Foo bar
Content-Length: %(body_len)i

%(body)s"""
            ],
            body,
            error.HeaderNoColonError,
        )

    def test_hdrs_nocolon_permissive(self):
        body = b"12345678901234567890"
        self.parser.careful = False
        self.parser.handle_input(
            wire(
                b"""\
http/1.1 200 OK
Content-Type: text/plain
Foo bar
Content-Length: %i

%s"""
                % (len(body), body)
            )
        )
        self.assertIsInstance(self.parser.test_err, error.HeaderNoColonError)
        self.assertEqual(self.parser.test_body, body)
        headers = [k for k, v in self.parser.test_hdrs]
        self.assertEqual(headers, [b"Content-Type", b"Content-Length"])

    def test_incomplete_field_section_too_large(self):
        self.parser.max_input_field_section_length = 16
        self.parser.handle_input(b"http/1.1 200 OK\r\nToo-Long")
        self.assertIsInstance(self.parser.test_err, error.FieldSectionTooLargeError)
        self.assertEqual(self.parser._input_buffer, [])

    def test_complete_field_section_too_large(self):
        self.parser.max_input_field_section_length = 16
        self.parser.handle_input(
            b"http/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        )
        self.assertIsInstance(self.parser.test_err, error.FieldSectionTooLargeError)

    def test_too_many_fields(self):
        self.parser.max_input_fields = 2
        self.parser.handle_input(
            b"http/1.1 200 OK\r\n"
            b"One: 1\r\n"
            b"Two: 2\r\n"
            b"Three: 3\r\n"
            b"Content-Length: 0\r\n"
            b"\r\n"
        )
        self.assertIsInstance(self.parser.test_err, error.TooManyFieldsError)

    def test_field_section_size_limit_is_not_permissive(self):
        self.parser.careful = False
        self.parser.max_input_field_section_length = 16
        self.parser.handle_input(b"http/1.1 200 OK\r\nToo-Long")
        self.assertIsInstance(self.parser.test_err, error.FieldSectionTooLargeError)
        self.assertEqual(self.parser.test_states, ["ERROR"])

    def test_hdr_case(self):
        body = b"12345678901234567890"
        self.checkSingleMsg(
            [
                b"""\
http/1.1 200 OK
Content-Type: text/plain
content-LENGTH: %(body_len)i

%(body)s"""
            ],
            body,
        )

    def test_hdrs_whitespace_before_colon(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length : %(body_len)i

%(body)s"""
            ],
            body,
            error.HeaderSpaceError,
        )

    def test_hdrs_fold(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Foo: bar
     baz
Content-Length: %(body_len)i

%(body)s"""
            ],
            body,
            error.ObsoleteFoldError,
        )

    def test_hdrs_fold_permissive(self):
        body = b"lorum ipsum whatever goes after that."
        self.parser.careful = False
        self.parser.handle_input(
            wire(
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Foo: bar
     baz
Content-Length: %i

%s"""
                % (len(body), body)
            )
        )
        self.assertIsInstance(self.parser.test_err, error.ObsoleteFoldError)
        foo_val = [v for k, v in self.parser.test_hdrs if k == b"Foo"][-1]
        self.assertEqual(foo_val, b" bar baz")
        headers = [k for k, v in self.parser.test_hdrs]
        self.assertEqual(headers, [b"Content-Type", b"Foo", b"Content-Length"])

    def test_bare_lf_headers(self):
        body = b"abc123"
        self.parser.handle_input(
            b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 6

abc123"""
        )
        self.assertIsInstance(self.parser.test_err, error.HeaderLineEndingError)
        self.assertEqual(self.parser.test_states, ["ERROR"])

    def test_bare_lf_headers_permissive(self):
        body = b"abc123"
        self.parser.careful = False
        self.parser.handle_input(
            b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 6

abc123"""
        )
        self.assertIsInstance(self.parser.test_err, error.HeaderLineEndingError)
        self.assertEqual(self.parser.test_body, body)
        self.assertEqual(self.parser.test_states[-1], "END")

    def test_hdrs_noname(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
: bar
Content-Length: %(body_len)i

%(body)s"""
            ],
            body,
            error.HeaderNameError,
        )

    def test_hdrs_noname_permissive(self):
        body = b"lorum ipsum whatever goes after that."
        self.parser.careful = False
        self.parser.handle_input(
            wire(
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
: bar
Content-Length: %i

%s"""
                % (len(body), body)
            )
        )
        self.assertIsInstance(self.parser.test_err, error.HeaderNameError)
        headers = [k for k, v in self.parser.test_hdrs]
        self.assertEqual(headers, [b"Content-Type", b"Content-Length"])

    def test_hdrs_utf8(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg(
            [
                """\
HTTP/1.1 200 OK
Content-Type: text/plain
Foo: ედუარდ შევარდნაძე
Content-Length: %(body_len)i

%(body)s""".encode(
                    "utf-8"
                )
            ],
            body,
        )
        foo_val = [v for k, v in self.parser.test_hdrs if k == b"Foo"][-1]
        self.assertEqual(foo_val.decode("utf-8"), " ედუარდ შევარდნაძე")

    def test_hdrs_null(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg(
            [
                """\
HTTP/1.1 200 OK
Content-Type: text/plain
Foo: \0
Content-Length: %(body_len)i

%(body)s""".encode(
                    "utf-8"
                )
            ],
            body,
            error.HeaderValueError,
        )

    def test_hdrs_null_permissive(self):
        body = b"lorum ipsum whatever goes after that."
        self.parser.careful = False
        self.parser.handle_input(
            wire(
                """\
HTTP/1.1 200 OK
Content-Type: text/plain
Foo: \0
Content-Length: %i

""".encode(
                    "utf-8"
                )
                % len(body)
                + body
            )
        )
        self.assertIsInstance(self.parser.test_err, error.HeaderValueError)
        foo_val = [v for k, v in self.parser.test_hdrs if k == b"Foo"][-1]
        self.assertEqual(foo_val, b" \0")

    def test_cl_delimit_11(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s"""
            ],
            body,
        )

    def test_cl_delimit_10(self):
        body = b"abcdefghijklmnopqrstuvwxyz"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.0 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s"""
            ],
            body,
        )

    def test_close_delimit(self):
        body = b"abcdefghijklmnopqrstuvwxyz"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.0 200 OK
Content-Type: text/plain

%(body)s"""
            ],
            body,
            close=True,
        )

    def test_extra_line(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg(
            [
                b"""\

HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s"""
            ],
            body,
        )

    def test_extra_lines(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg(
            [
                b"""\



HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s"""
            ],
            body,
        )

    def test_telnet_client(self):
        body = "lorum ipsum whatever goes after that."
        self.checkSingleMsg(
            [
                a.encode("ascii")
                for a in f"""


HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: {len(body)}

{body}"""
            ],
            body.encode("ascii"),
        )

    def test_naughty_first_header(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
    Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s"""
            ],
            body,
            error.TopLineSpaceError,
        )

    def test_cl_header_case(self):
        body = b"12345678901234567890"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
content-LENGTH: %(body_len)i

%(body)s"""
            ],
            body,
        )

    def test_chunk_delimit(self):
        body = b"aaabbbcccdddeeefffggghhhiii"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x\r
%(body)s\r
0\r
\r
"""
            ],
            body,
        )

    def test_chunk_exact(self):
        body = b"aaabbbcccdddeeefffggghhhiii"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

""",
                b"""\
%(body_len)x\r
%(body)s\r
""",
                b"""\
0\r
\r
""",
            ],
            body,
        )

    def test_chunk_split(self):
        body = b"aaabbbcccdddeeefffggghhhiii"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

""",
                b"""\
%(body_len)x\r
%(body)s\r
0""",
                b"""\
\r
Foo: bar\r
\r
""",
            ],
            body,
        )

    def test_chunk_exact_offset(self):
        body = b"aaabbbcccdddeeefffggghhhiii"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

""",
                b"""\
%(body_len)x\r
%(body)s""",
                b"""\r
0\r
\r
""",
            ],
            body,
        )

    def test_chunk_more(self):
        body = b"1234567890"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

""",
                b"""\
%(body_len)x\r
%(body)s\r
%(body_len)x\r
%(body)s\r
0\r
\r
"""
                % {b"body": body, b"body_len": len(body)},
            ],
            body * 2,
        )

    def test_transfer_case(self):
        body = b"aaabbbcccdddeeefffggghhhiii"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: cHuNkEd

%(body_len)x\r
%(body)s\r
0\r
\r
"""
            ],
            body,
        )

    def test_big_chunk(self):
        body = b"aaabbbcccdddeeefffggghhhiii" * 1000000
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x\r
%(body)s\r
0\r
\r
"""
            ],
            body,
        )

    def xxxtest_small_chunks(self):
        num_chunks = 10000
        body = b"a" * num_chunks
        inputs = [
            b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

"""
        ]
        for i in range(num_chunks):
            inputs.append(
                b"""\
1\r
a\r
"""
            )
        inputs.append(
            b"""\
0\r
\r
"""
        )
        self.checkSingleMsg(inputs, body)

    def test_split_chunk(self):
        body = b"abcdefg123456"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x\r
abcdefg""",
                b"""\
123456\r
0\r
\r
""",
            ],
            body,
        )

    def test_split_chunk_length(self):
        body = b"do re mi so fa la ti do"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x""",
                b"""\
\r
%(body)s\r
0\r
\r
""",
            ],
            body,
        )

    def test_chunk_bad_syntax(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

ZZZZ\r
%(body)s\r
0\r
\r
"""
            ],
            body,
            error.ChunkError,
        )
        self.assertEqual(self.parser._input_state, States.ERROR)

    def test_chunk_size_line_too_long_is_terminal(self):
        self.parser.handle_input(
            b"HTTP/1.1 200 OK\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            + (b"1" * 513)
        )
        self.assertIsInstance(self.parser.test_err, error.ChunkError)
        self.assertEqual(self.parser._input_state, States.ERROR)
        self.assertEqual(self.parser._input_buffer, [])

    def test_chunk_bad_terminator(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x\r
%(body)sXX0\r
\r
"""
            ],
            body,
            error.ChunkTerminatorError,
        )

    def test_chunk_bad_terminator_permissive(self):
        body = b"abc123def456ghi789"
        self.parser.careful = False
        self.parser.handle_input(
            b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%x\r
%sXX0\r
\r
"""
            % (len(body), body)
        )
        self.assertIsInstance(self.parser.test_err, error.ChunkTerminatorError)
        self.assertEqual(self.parser.test_body, body)
        self.assertEqual(self.parser.test_states[-1], "END")

    def test_chunk_nonfinal(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked, foo

%(body)s"""
            ],
            body,
            close=True,
        )

    def test_cl_dup(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i
Content-Length: %(body_len)i

%(body)s"""
            ],
            body,
        )

    def test_cl_conflict(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 2
Content-Length: %(body_len)i

%(body)s"""
            ],
            body,
            error.DuplicateCLError,
        )

    def test_cl_bad_syntax(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 2abc

%(body)s"""
            ],
            body,
            error.MalformedCLError,
        )

    def test_chunk_ext(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x; myext=foobarbaz\r
%(body)s\r
0\r
\r
"""
            ],
            body,
        )

    def test_trailers(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x\r
%(body)s\r
0\r
Foo: bar
Baz: 1
\r
"""
            ],
            body,
        )
        self.assertEqual(
            self.parser.test_trailers, [(b"Foo", b" bar"), (b"Baz", b" 1")]
        )

    def test_trailer_field_section_too_large(self):
        self.parser.max_input_field_section_length = 16
        self.parser.handle_input(
            b"HTTP/1.1 200 OK\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"0\r\n"
            b"Too-Long"
        )
        self.assertIsInstance(self.parser.test_err, error.FieldSectionTooLargeError)
        self.assertEqual(self.parser._input_buffer, [])

    def test_pipeline_chunked(self):
        body = b"abc123def456ghi789"
        self.checkMultiMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x\r
%(body)s\r
0\r
\r
HTTP/1.1 404 Not Found
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x\r
%(body)s\r
0\r
\r
"""
            ],
            body,
            2,
        )

    def test_pipeline_cl(self):
        body = b"abc123def456ghi789"
        self.checkMultiMsg(
            [
                b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)sHTTP/1.1 404 Not Found
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s"""
            ],
            body,
            2,
        )

    def test_output_start_line_crlf(self):
        with self.assertRaises(error.OutputSyntaxError):
            self.parser.output_start(
                b"HTTP/1.1 200 OK\r\nX: injected",
                [],
                Delimiters.NOBODY,
            )

    def test_output_header_value_crlf(self):
        with self.assertRaises(error.OutputSyntaxError):
            self.parser.output_start(
                b"HTTP/1.1 200 OK",
                [(b"X-Test", b"ok\r\nX-Injected: yes")],
                Delimiters.NOBODY,
            )

    def test_output_trailer_value_crlf(self):
        self.parser.output_start(
            b"HTTP/1.1 200 OK",
            [(b"Transfer-Encoding", b"chunked")],
            Delimiters.CHUNKED,
        )
        with self.assertRaises(error.OutputSyntaxError):
            self.parser.output_end([(b"X-Test", b"ok\r\nX-Injected: yes")])

    def test_output_body_before_start(self):
        with self.assertRaises(error.OutputStateError):
            self.parser.output_body(b"body")

    def test_output_end_before_start(self):
        with self.assertRaises(error.OutputStateError):
            self.parser.output_end([])

    def test_output_start_twice(self):
        self.parser.output_start(
            b"HTTP/1.1 200 OK",
            [(b"Content-Length", b"0")],
            Delimiters.COUNTED,
        )
        with self.assertRaises(error.OutputStateError):
            self.parser.output_start(
                b"HTTP/1.1 200 OK",
                [(b"Content-Length", b"0")],
                Delimiters.COUNTED,
            )

    def test_output_body_after_nonfinal(self):
        self.parser.output_start(
            b"HTTP/1.1 103 Early Hints",
            [],
            Delimiters.NONE,
            is_final=False,
        )
        with self.assertRaises(error.OutputStateError):
            self.parser.output_body(b"body")


#    def test_nobody_delimit(self):
#    def test_pipeline_nobody(self):
#    def test_chunked_then_length(self):
#    def test_length_then_chunked(self):


if __name__ == "__main__":
    unittest.main()
