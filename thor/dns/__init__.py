#!/usr/bin/env python

from multiprocessing import Pool
import socket
from typing import Callable, Union

timeout = 3
pool_size = 5


def lookup(host: bytes, cb: Callable[..., None]) -> None:
#    try:
#        cb(_lookup(host))
#    except Exception as why:
#        _error(why)
    _pool.apply_async(_lookup, (host,), callback=cb, error_callback=cb)


def _lookup(host: bytes) -> str:
    return socket.gethostbyname(host.decode('idna'))


_pool = Pool(processes=pool_size)
