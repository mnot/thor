#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import unittest
from typing import List, Tuple

from framework import DummyHttpParser
from thor.http.common import Delimiters, States

import thor.http.error as error


def wire(data):
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


class CapturingHttpParser(DummyHttpParser):
    def __init__(self) -> None:
        DummyHttpParser.__init__(self)
        self.written: List[bytes] = []

    def output(self, data: bytes) -> None:
        self.written.append(data)


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

    CHUNKED_MSGS = [
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n3;ext=1\r\nabc\r\n1\r\n!\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"4\r\nwxyz\r\n2\r\n\r\n\r\n0\r\nFoo: bar\r\n\r\n",
    ]

    def parse_split(self, msg, offsets):
        parser = DummyHttpParser()
        last = 0
        for offset in list(offsets) + [len(msg)]:
            parser.handle_input(msg[last:offset])
            last = offset
        return parser

    def check_same_as_unsplit(self, msg, offsets):
        expected = self.parse_split(msg, [])
        got = self.parse_split(msg, offsets)
        self.assertEqual(expected.test_err, None)
        self.assertEqual(got.test_err, None, offsets)
        self.assertEqual(expected.test_body, got.test_body, offsets)
        self.assertEqual(expected.test_trailers, got.test_trailers, offsets)
        self.assertEqual(
            expected.input_transfer_length, got.input_transfer_length, offsets
        )
        self.assertEqual(expected._input_state, got._input_state, offsets)
        self.assertEqual(got.test_states.count("END"), 1, offsets)
        self.assertEqual(got.test_states[-1], "END", offsets)

    def test_chunked_split_every_offset(self):
        for msg in self.CHUNKED_MSGS:
            for i in range(1, len(msg)):
                self.check_same_as_unsplit(msg, [i])

    def test_chunked_split_every_pair_of_offsets(self):
        for msg in self.CHUNKED_MSGS:
            for i in range(1, len(msg)):
                for j in range(i + 1, len(msg)):
                    self.check_same_as_unsplit(msg, [i, j])

    def test_chunked_split_byte_by_byte(self):
        for msg in self.CHUNKED_MSGS:
            self.check_same_as_unsplit(msg, range(1, len(msg)))

    def test_chunk_terminator_cr_split(self):
        self.parser.handle_input(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r"
        )
        self.parser.handle_input(b"\n0\r\n\r\n")
        self.assertEqual(self.parser.test_body, b"hello")
        self.assertEqual(self.parser.test_states[-1], "END")

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
        # chunk data is delivered before its terminator is checked
        self.assertEqual(self.parser.test_body, body)
        self.assertEqual(self.parser.test_states, ["START", "BODY", "ERROR"])
        self.assertEqual(self.parser._input_state, States.ERROR)

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

    def test_bare_cr_in_field(self):
        self.parser.handle_input(
            b"HTTP/1.1 200 OK\r\n"
            b"X-Foo: a\rTransfer-Encoding: chunked\r\n"
            b"Content-Length: 0\r\n"
            b"\r\n"
        )
        self.assertIsInstance(self.parser.test_err, error.BareCRError)
        self.assertEqual(self.parser.test_states, ["ERROR"])

    def test_bare_cr_in_field_permissive(self):
        self.parser.careful = False
        self.parser.handle_input(
            b"HTTP/1.1 200 OK\r\n"
            b"X-Foo: a\rTransfer-Encoding: chunked\r\n"
            b"Content-Length: 0\r\n"
            b"\r\n"
        )
        self.assertIsInstance(self.parser.test_err, error.BareCRError)
        self.assertEqual(
            self.parser.test_hdrs,
            [
                (b"X-Foo", b" a Transfer-Encoding: chunked"),
                (b"Content-Length", b" 0"),
            ],
        )

    def test_bare_cr_in_trailers(self):
        self.parser.handle_input(
            b"HTTP/1.1 200 OK\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"0\r\n"
            b"X-Foo: a\rX-Bar: b\r\n"
            b"\r\n"
        )
        self.assertIsInstance(self.parser.test_err, error.BareCRError)
        self.assertNotIn("END", self.parser.test_states)

    def test_cl_lenient_integers(self):
        for value in [b"1_0", b"+5", b"-0", b"0x5", b"5_", b"\x0c5", b"1.0"]:
            with self.subTest(value=value):
                parser = DummyHttpParser()
                parser.handle_input(
                    b"HTTP/1.1 200 OK\r\nContent-Length: %s\r\n\r\n0123456789" % value
                )
                self.assertIsInstance(parser.test_err, error.MalformedCLError)
                self.assertEqual(parser.test_states, ["ERROR"])

    def test_cl_digits_ok(self):
        self.parser.handle_input(
            b"HTTP/1.1 200 OK\r\nContent-Length:  \t010 \r\n\r\n0123456789"
        )
        self.assertEqual(self.parser.test_states, ["START", "BODY", "END"])
        self.assertEqual(self.parser.test_body, b"0123456789")

    def test_chunk_size_lenient_integers(self):
        for size in [b"0x5", b"+5", b"-5", b" 5", b"\t5", b"5 ", b"5_0", b"", b"5\x0c"]:
            with self.subTest(size=size):
                parser = DummyHttpParser()
                parser.handle_input(
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                    + size
                    + b"\r\nhello\r\n0\r\n\r\n"
                )
                self.assertIsInstance(parser.test_err, error.ChunkError)
                self.assertEqual(parser._input_state, States.ERROR)
                self.assertEqual(parser.test_body, b"")

    def test_chunk_size_hex_ok(self):
        self.parser.handle_input(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"0000A\r\n0123456789\r\n0\r\n\r\n"
        )
        self.assertEqual(self.parser.test_body, b"0123456789")
        self.assertEqual(self.parser.test_states[-1], "END")

    def test_chunk_ext_bws(self):
        self.parser.handle_input(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5 ;a=b\r\nhello\r\n0\r\n\r\n"
        )
        self.assertEqual(self.parser.test_body, b"hello")
        self.assertEqual(self.parser.test_states[-1], "END")

    def test_te_and_cl(self):
        self.parser.handle_input(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 5\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"5\r\nhello\r\n0\r\n\r\n"
        )
        self.assertIsInstance(self.parser.test_err, error.TransferEncodingCLError)
        self.assertEqual(self.parser.test_states, ["ERROR"])

    def test_te_and_cl_permissive(self):
        self.parser.careful = False
        self.parser.handle_input(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 5\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"5\r\nhello\r\n0\r\n\r\n"
        )
        self.assertIsNone(self.parser.test_err)
        self.assertEqual(self.parser.test_body, b"hello")
        self.assertEqual(self.parser.test_states[-1], "END")

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

    def check_invalid_output_content_length(
        self, hdr_tuples: List[Tuple[bytes, bytes]]
    ) -> None:
        writer = CapturingHttpParser()
        with self.assertRaises(error.OutputSyntaxError):
            writer.output_start(b"HTTP/1.1 200 OK", hdr_tuples, Delimiters.COUNTED)
        self.assertEqual(writer.written, [])

    def test_output_content_length_not_digits(self) -> None:
        for value in [
            b"+5",
            b"-5",
            b"1_0",
            b"0x10",
            b"1 0",
            b"",
            b"abc",
            b"1.5",
            b"\x0c5",
            b"5\x0b",
            b"\x0b5",
            b"5,5",
            b"5, 5",
            b"1" * 21,
            b"9" * 5000,
        ]:
            with self.subTest(value=value):
                self.check_invalid_output_content_length([(b"Content-Length", value)])

    def test_output_content_length_conflicting(self) -> None:
        self.check_invalid_output_content_length(
            [(b"Content-Length", b"5"), (b"Content-Length", b"6")]
        )
        self.check_invalid_output_content_length([(b"Content-Length", b"5, 6")])

    def test_output_content_length_repeated_same(self) -> None:
        writer = CapturingHttpParser()
        writer.output_start(
            b"HTTP/1.1 200 OK",
            [(b"Content-Length", b"5"), (b"Content-Length", b"5")],
            Delimiters.COUNTED,
        )
        writer.output_body(b"hello")
        with self.assertRaises(error.OutputError):
            writer.output_body(b"!")

    def test_output_content_length_matches_input(self) -> None:
        # whatever the output side accepts, the input parser must read back
        for value in [b"5", b" 5", b"5\t", b"\t 05 "]:
            writer = CapturingHttpParser()
            writer.output_start(
                b"HTTP/1.1 200 OK", [(b"Content-Length", value)], Delimiters.COUNTED
            )
            reader = DummyHttpParser()
            reader.handle_input(b"".join(writer.written) + b"hello")
            self.assertNotIn("ERROR", reader.test_states, value)

    def test_output_content_length_max_digits(self) -> None:
        writer = CapturingHttpParser()
        writer.output_start(
            b"HTTP/1.1 200 OK", [(b"Content-Length", b"1" * 20)], Delimiters.COUNTED
        )

    def test_output_content_length_surrounding_ows(self) -> None:
        writer = CapturingHttpParser()
        writer.output_start(
            b"HTTP/1.1 200 OK", [(b"Content-Length", b" 5 ")], Delimiters.COUNTED
        )
        with self.assertRaises(error.OutputError):
            writer.output_body(b"hello!")

    def test_output_trailer_value_crlf(self):
        self.parser.output_start(
            b"HTTP/1.1 200 OK",
            [(b"Transfer-Encoding", b"chunked")],
            Delimiters.CHUNKED,
        )
        with self.assertRaises(error.OutputSyntaxError):
            self.parser.output_end([(b"X-Test", b"ok\r\nX-Injected: yes")])

    def check_chunked_roundtrip(
        self, trailers: List[Tuple[bytes, bytes]], expected_tail: bytes
    ) -> None:
        writer = CapturingHttpParser()
        writer.output_start(
            b"HTTP/1.1 200 OK",
            [(b"Transfer-Encoding", b"chunked")],
            Delimiters.CHUNKED,
        )
        writer.output_body(b"hi")
        writer.output_end(trailers)
        wire_bytes = b"".join(writer.written)
        self.assertTrue(wire_bytes.endswith(expected_tail), wire_bytes)

        self.parser.handle_input(wire_bytes)
        self.assertEqual(self.parser.test_states, ["START", "BODY", "END"])
        self.assertEqual(self.parser.test_body, b"hi")
        self.assertEqual(
            self.parser.test_trailers, [(k, b" " + v) for k, v in trailers]
        )

    def test_output_end_trailers(self) -> None:
        self.check_chunked_roundtrip(
            [(b"X-T", b"1"), (b"X-U", b"2")], b"0\r\nX-T: 1\r\nX-U: 2\r\n\r\n"
        )

    def test_output_end_no_trailers(self) -> None:
        self.check_chunked_roundtrip([], b"2\r\nhi\r\n0\r\n\r\n")

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

    def test_output_body_too_long(self):
        sent = []
        self.parser.output = sent.append
        self.parser.output_start(
            b"HTTP/1.1 200 OK",
            [(b"Content-Length", b"3")],
            Delimiters.COUNTED,
        )
        sent.clear()
        with self.assertRaises(error.OutputError):
            self.parser.output_body(b"abcd")
        self.assertEqual(sent, [])

    def test_output_body_dropped_for_nobody(self):
        sent = []
        self.parser.output = sent.append
        self.parser.output_start(
            b"HTTP/1.1 200 OK",
            [(b"Content-Length", b"3")],
            Delimiters.NOBODY,
        )
        sent.clear()
        self.parser.output_body(b"abcdefgh")
        self.assertEqual(sent, [])
        self.assertFalse(self.parser.output_end([]))

    def test_output_end_short_counted_body(self):
        done = []
        self.parser.output_done = lambda: done.append(True)
        self.parser.output_start(
            b"HTTP/1.1 200 OK",
            [(b"Content-Length", b"5")],
            Delimiters.COUNTED,
        )
        self.parser.output_body(b"abc")
        self.assertTrue(self.parser.output_end([]))
        self.assertEqual(done, [])
        self.assertEqual(self.parser._output_state, States.WAITING)

    def test_output_end_complete_counted_body(self):
        done = []
        self.parser.output_done = lambda: done.append(True)
        self.parser.output_start(
            b"HTTP/1.1 200 OK",
            [(b"Content-Length", b"3")],
            Delimiters.COUNTED,
        )
        self.parser.output_body(b"abc")
        self.assertFalse(self.parser.output_end([]))
        self.assertEqual(done, [True])


#    def test_nobody_delimit(self):
#    def test_pipeline_nobody(self):
#    def test_chunked_then_length(self):
#    def test_length_then_chunked(self):


if __name__ == "__main__":
    unittest.main()
