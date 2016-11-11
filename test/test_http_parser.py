#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import unittest

from framework import DummyHttpParser
from thor.http.common import Delimiters

import thor.http.error as error


class TestHttpParser(unittest.TestCase):

    def setUp(self):
        self.parser = DummyHttpParser()

    def checkSingleMsg(self, inputs, body, expected_err=None, close=False):
        """
        Check a single HTTP message.
        """
        assert type(inputs) == type([])
        for chunk in inputs:
            self.parser.handle_input(chunk % {
                b'body': body,
                b'body_len': len(body)
            })
        states = self.parser.test_states

        if not expected_err:
            self.assertFalse("ERROR" in states, self.parser.test_err)
            self.assertTrue(states.count('START') == 1, states)
            self.assertTrue(states.index('START') < states.index('BODY'))
            if close:
                self.assertEqual(self.parser._input_delimit, Delimiters.CLOSE)
            else:
                self.assertTrue(states.index('END') + 1 == len(states))
            self.assertEqual(body, self.parser.test_body, "%s not equal to %s" % (body[:20], self.parser.test_body[:20]))
        else:
            self.assertTrue("ERROR" in states, states)
            self.assertEqual(self.parser.test_err.__class__, expected_err)

    def checkMultiMsg(self, inputs, body, count):
        """
        Check pipelined messages. Assumes the same body for each (for now).
        """
        for chunk in inputs:
            self.parser.handle_input(chunk % {
                b'body': body,
                b'body_len': len(body)
            })
        states = self.parser.test_states
        self.assertFalse("ERROR" in self.parser.test_states, self.parser.test_err)
        self.parser.check(self, {'states': ['START', 'BODY', 'END'] * count})

    def test_hdrs(self):
        body = b"12345678901234567890"
        self.checkSingleMsg([b"""\
http/1.1 200 OK
Content-Type: text/plain
Foo: bar
Content-Length: %(body_len)i
Foo: baz, bam

%(body)s"""], body)
        self.parser.check(self, {
            'hdrs': [
                (b'Content-Type', b" text/plain"),
                (b'Foo', b" bar"),
                (b'Content-Length', b" %i" % len(body)),
                (b'Foo', b" baz, bam"),
            ]
        })

    def test_hdrs_nocolon(self):
        body = b"12345678901234567890"
        self.checkSingleMsg([b"""\
http/1.1 200 OK
Content-Type: text/plain
Foo bar
Content-Length: %(body_len)i

%(body)s"""], body)
        # FIXME: error?

    def test_hdr_case(self):
        body = b"12345678901234567890"
        self.checkSingleMsg([b"""\
http/1.1 200 OK
Content-Type: text/plain
content-LENGTH: %(body_len)i

%(body)s"""], body)

    def test_hdrs_whitespace_before_colon(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length : %(body_len)i

%(body)s"""], body, error.HeaderSpaceError)

    def test_hdrs_fold(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Foo: bar
     baz
Content-Length: %(body_len)i

%(body)s"""], body)
        foo_val = [v for k,v in self.parser.test_hdrs if k == b'Foo'][-1]
        self.assertEqual(foo_val, b" bar baz")
        headers = [k for k,v in self.parser.test_hdrs]
        self.assertEqual(headers, [b'Content-Type', b'Foo', b'Content-Length'])

    def test_hdrs_noname(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
: bar
Content-Length: %(body_len)i

%(body)s"""], body)
        headers = [k for k,v in self.parser.test_hdrs]
        self.assertEqual(headers, [b'Content-Type', b'', b'Content-Length'])


    def test_hdrs_utf8(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg([u"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Foo: ედუარდ შევარდნაძე
Content-Length: %(body_len)i

%(body)s""".encode('utf-8')], body)
        foo_val = [v for k,v in self.parser.test_hdrs if k == b'Foo'][-1]
        self.assertEqual(foo_val.decode('utf-8'), u" ედუარდ შევარდნაძე")

    def test_hdrs_null(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg([u"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Foo: \0
Content-Length: %(body_len)i

%(body)s""".encode('utf-8')], body)
        foo_val = [v for k,v in self.parser.test_hdrs if k == b'Foo'][-1]
        self.assertEqual(foo_val, b" \0")


    def test_cl_delimit_11(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s"""], body)

    def test_cl_delimit_10(self):
        body = b"abcdefghijklmnopqrstuvwxyz"
        self.checkSingleMsg([b"""\
HTTP/1.0 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s"""], body)

    def test_close_delimit(self):
        body = b"abcdefghijklmnopqrstuvwxyz"
        self.checkSingleMsg([b"""\
HTTP/1.0 200 OK
Content-Type: text/plain

%(body)s"""], body, close=True)

    def test_extra_line(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg([b"""\

HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s"""], body)

    def test_extra_lines(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg([b"""\



HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s"""], body)

    def test_telnet_client(self):
        body = "lorum ipsum whatever goes after that."
        self.checkSingleMsg([a.encode('ascii') for a in """\



HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s""" % {'body': body, 'body_len': len(body)}], body.encode('ascii'))


    def test_naughty_first_header(self):
        body = b"lorum ipsum whatever goes after that."
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
    Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s"""], body, error.TopLineSpaceError)

    def test_cl_header_case(self):
        body = b"12345678901234567890"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
content-LENGTH: %(body_len)i

%(body)s"""], body)

    def test_chunk_delimit(self):
        body = b"aaabbbcccdddeeefffggghhhiii"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x\r
%(body)s\r
0\r
\r
"""], body)

    def test_chunk_exact(self):
        body = b"aaabbbcccdddeeefffggghhhiii"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

""", b"""\
%(body_len)x\r
%(body)s\r
""", b"""\
0\r
\r
"""], body)

    def test_chunk_split(self):
        body = b"aaabbbcccdddeeefffggghhhiii"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

""", b"""\
%(body_len)x\r
%(body)s\r
0""", b"""\
\r
Foo: bar\r
\r
"""], body)

    def test_chunk_exact_offset(self):
        body = b"aaabbbcccdddeeefffggghhhiii"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

""", b"""\
%(body_len)x\r
%(body)s""", b"""\r
0\r
\r
"""], body)
    def test_chunk_more(self):
        body = b"1234567890"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

""", b"""\
%(body_len)x\r
%(body)s\r
%(body_len)x\r
%(body)s\r
0\r
\r
""" % {b'body': body, b'body_len': len(body)}], body * 2)


    def test_transfer_case(self):
        body = b"aaabbbcccdddeeefffggghhhiii"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: cHuNkEd

%(body_len)x\r
%(body)s\r
0\r
\r
"""], body)

    def test_big_chunk(self):
        body = b"aaabbbcccdddeeefffggghhhiii" * 1000000
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x\r
%(body)s\r
0\r
\r
"""], body)

    def xxxtest_small_chunks(self):
        num_chunks = 10000
        body = b"a" * num_chunks
        inputs = [b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

"""]
        for i in range(num_chunks):
            inputs.append(b"""\
1\r
a\r
""")
        inputs.append(b"""\
0\r
\r
""")
        self.checkSingleMsg(inputs, body)

    def test_split_chunk(self):
        body = b"abcdefg123456"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x\r
abcdefg""",
b"""\
123456\r
0\r
\r
"""], body)

    def test_split_chunk_length(self):
        body = b"do re mi so fa la ti do"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x""",
b"""\
\r
%(body)s\r
0\r
\r
"""], body)

    def test_chunk_bad_syntax(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

ZZZZ\r
%(body)s\r
0\r
\r
"""], body, error.ChunkError)

    def test_chunk_nonfinal(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked, foo

%(body)s"""], body, close=True)

    def test_cl_dup(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i
Content-Length: %(body_len)i

%(body)s"""], body)

    def test_cl_conflict(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 2
Content-Length: %(body_len)i

%(body)s"""], body, error.DuplicateCLError)

    def test_cl_bad_syntax(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: 2abc

%(body)s"""], body, error.MalformedCLError)

    def test_chunk_ext(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x; myext=foobarbaz\r
%(body)s\r
0\r
\r
"""], body)

    def test_trailers(self):
        body = b"abc123def456ghi789"
        self.checkSingleMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Transfer-Encoding: chunked

%(body_len)x\r
%(body)s\r
0\r
Foo: bar
Baz: 1
\r
"""], body)
        self.assertEqual(self.parser.test_trailers,
            [(b'Foo', b' bar'), (b'Baz', b' 1')]
        )

    def test_pipeline_chunked(self):
        body = b"abc123def456ghi789"
        self.checkMultiMsg([b"""\
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
"""], body, 2)

    def test_pipeline_cl(self):
        body = b"abc123def456ghi789"
        self.checkMultiMsg([b"""\
HTTP/1.1 200 OK
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)sHTTP/1.1 404 Not Found
Content-Type: text/plain
Content-Length: %(body_len)i

%(body)s"""], body, 2)

# TODO:
#    def test_nobody_delimit(self):
#    def test_pipeline_nobody(self):
#    def test_chunked_then_length(self):
#    def test_length_then_chunked(self):


if __name__ == '__main__':
    unittest.main()