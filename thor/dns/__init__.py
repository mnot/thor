#!/usr/bin/env python

from multiprocessing.pool import ThreadPool
import socket
from typing import Callable, Union

pool_size = 10

def lookup(host: bytes, cb: Callable[..., None]) -> None:
    _pool.apply_async(_lookup, (host,), callback=cb, error_callback=cb)


def _lookup(host: bytes) -> str:
    return socket.gethostbyname(host.decode('idna'))


_pool = ThreadPool(processes=pool_size)
