#!/usr/bin/env python

import sys
import unittest

from thor.http.common import header_names, header_dict, get_header

hdrs = [
    ('A', 'a1'),
    ('B', 'b1'),
    ('a', 'a2'),
    ('C', 'c1'),
    ('b', 'b2'),
    ('A', 'a3, a4'),
    ('D', '"d1, d1"'),
]


class TestHttpUtils(unittest.TestCase):
    def test_header_names(self):
        hdrs_n = header_names(hdrs)
        self.assertEqual(hdrs_n, set(['a', 'b', 'c', 'd']))
    
    def test_header_dict(self):
        hdrs_d = header_dict(hdrs)
        self.assertEqual(hdrs_d['a'], ['a1', 'a2', 'a3', 'a4'])
        self.assertEqual(hdrs_d['b'], ['b1', 'b2'])
        self.assertEqual(hdrs_d['c'], ['c1'])

    def test_header_dict_omit(self):
        hdrs_d = header_dict(hdrs, 'b')
        self.assertEqual(hdrs_d['a'], ['a1', 'a2', 'a3', 'a4'])
        self.assertTrue('b' not in hdrs_d.keys())
        self.assertTrue('B' not in hdrs_d.keys())
        self.assertEqual(hdrs_d['c'], ['c1'])
        
    def test_get_header(self):
        self.assertEqual(get_header(hdrs, 'a'), ['a1', 'a2', 'a3', 'a4'])
        self.assertEqual(get_header(hdrs, 'b'), ['b1', 'b2'])
        self.assertEqual(get_header(hdrs, 'c'), ['c1'])

if __name__ == '__main__':
    unittest.main()