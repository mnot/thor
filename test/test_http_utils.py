#!/usr/bin/env python

import sys
import unittest

from thor.http.common import header_names, header_dict, get_header

class TestHttpUtilsBytes(unittest.TestCase):
    hdrs = [
        (b'A', b'a1'),
        (b'B', b'b1'),
        (b'a', b'a2'),
        (b'C', b'c1'),
        (b'b', b'b2'),
        (b'A', b'a3, a4'),
        (b'D', b'"d1, d1"'),
    ]

    def test_header_names(self):
        hdrs_n = header_names(self.hdrs)
        self.assertEqual(hdrs_n, set([b'a', b'b', b'c', b'd']))

    def test_header_dict(self):
        hdrs_d = header_dict(self.hdrs)
        self.assertEqual(hdrs_d[b'a'], [b'a1', b'a2', b'a3', b'a4'])
        self.assertEqual(hdrs_d[b'b'], [b'b1', b'b2'])
        self.assertEqual(hdrs_d[b'c'], [b'c1'])

    def test_header_dict_omit(self):
        hdrs_d = header_dict(self.hdrs, b'b')
        self.assertEqual(hdrs_d[b'a'], [b'a1', b'a2', b'a3', b'a4'])
        self.assertTrue(b'b' not in list(hdrs_d))
        self.assertTrue(b'B' not in list(hdrs_d))
        self.assertEqual(hdrs_d[b'c'], [b'c1'])

    def test_get_header(self):
        self.assertEqual(get_header(self.hdrs, b'a'), [b'a1', b'a2', b'a3', b'a4'])
        self.assertEqual(get_header(self.hdrs, b'b'), [b'b1', b'b2'])
        self.assertEqual(get_header(self.hdrs, b'c'), [b'c1'])

if __name__ == '__main__':
    unittest.main()
