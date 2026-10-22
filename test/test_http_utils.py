#!/usr/bin/env python

import sys
import unittest

from thor.http.common import check_method, check_request_target, strip_hop_by_hop, header_names, header_dict, get_header


class TestHttpUtilsBytes(unittest.TestCase):
    hdrs = [
        (b"A", b"a1"),
        (b"B", b"b1"),
        (b"a", b"a2"),
        (b"C", b"c1"),
        (b"b", b"b2"),
        (b"A", b"a3, a4"),
        (b"D", b'"d1, d1"'),
    ]

    def test_header_names(self):
        hdrs_n = header_names(self.hdrs)
        self.assertEqual(hdrs_n, set([b"a", b"b", b"c", b"d"]))

    def test_header_dict(self):
        hdrs_d = header_dict(self.hdrs)
        self.assertEqual(hdrs_d[b"a"], [b"a1", b"a2", b"a3", b"a4"])
        self.assertEqual(hdrs_d[b"b"], [b"b1", b"b2"])
        self.assertEqual(hdrs_d[b"c"], [b"c1"])

    def test_header_dict_omit(self):
        hdrs_d = header_dict(self.hdrs, b"b")
        self.assertEqual(hdrs_d[b"a"], [b"a1", b"a2", b"a3", b"a4"])
        self.assertTrue(b"b" not in list(hdrs_d))
        self.assertTrue(b"B" not in list(hdrs_d))
        self.assertEqual(hdrs_d[b"c"], [b"c1"])

    def test_get_header(self):
        self.assertEqual(get_header(self.hdrs, b"a"), [b"a1", b"a2", b"a3", b"a4"])
        self.assertEqual(get_header(self.hdrs, b"b"), [b"b1", b"b2"])
        self.assertEqual(get_header(self.hdrs, b"c"), [b"c1"])


class TestRequestLineChecks(unittest.TestCase):
    def test_method(self):
        self.assertIsNone(check_method(b"GET"))
        self.assertIsNone(check_method(b"M-SEARCH"))
        for bad in (b"", b"GE T", b"G\x00ET", b"GET/", b"GET\r"):
            self.assertIsNotNone(check_method(bad), bad)

    def test_request_target(self):
        self.assertIsNone(check_request_target(b"/a?b=c%20d"))
        self.assertIsNone(check_request_target(b"*"))
        self.assertIsNone(check_request_target(b"/caf\xc3\xa9"))  # not ours to police
        for bad in (b"", b"/a b", b"/a\tb", b"/a\x01", b"/a\x7f"):
            self.assertIsNotNone(check_request_target(bad), bad)


class TestStripHopByHop(unittest.TestCase):
    def test_standard_nominated_and_extra_fields_removed(self):
        hdrs = [
            (b"Content-Type", b"text/plain"),
            (b"Connection", b"X-Hop, keep-alive"),
            (b"X-Hop", b"1"),
            (b"x-hop", b"2"),
            (b"Proxy-Connection", b"keep-alive"),
            (b"Transfer-Encoding", b"chunked"),
            (b"Host", b"example.com"),
            (b"X-Keep", b"yes"),
        ]
        self.assertEqual(
            strip_hop_by_hop(hdrs, also=[b"host"]),
            [(b"Content-Type", b"text/plain"), (b"X-Keep", b"yes")],
        )

    def test_nothing_to_strip(self):
        hdrs = [(b"Content-Length", b"0")]
        self.assertEqual(strip_hop_by_hop(hdrs), hdrs)


if __name__ == "__main__":
    unittest.main()
