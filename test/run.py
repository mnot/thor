#!/usr/bin/env python

import sys
import unittest

sys.path.insert(0, '..')

from test_events import TestEventEmitter
from test_http_parser import TestHttpParser
from test_http_client import TestHttpClient
from test_http_server import TestHttpServer
from test_http_utils import TestHttpUtils
from test_loop import TestLoop
from test_tcp_client import TestTcpClientConnect
from test_tcp_server import TestTcpServer
from test_udp import TestUdpEndpoint

unittest.main()