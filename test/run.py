#!/usr/bin/env python

from __future__ import absolute_import
import os
import sys
import unittest

base = os.path.dirname(os.path.realpath(__file__)).rsplit('/',1)[0]
sys.path.insert(0, base)

from test_events import TestEventEmitter
from test_http_parser import TestHttpParser
from test_http_client import TestHttpClient
from test_http_server import TestHttpServer
from test_http_utils import TestHttpUtilsBytes, TestHttpUtilsStrings
from test_loop import TestLoop
from test_tcp_client import TestTcpClientConnect
from test_tcp_server import TestTcpServer
from test_udp import TestUdpEndpoint

unittest.main()