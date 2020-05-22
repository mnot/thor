#!/usr/bin/env python

from concurrent.futures import ThreadPoolExecutor
import socket
from typing import Callable, Union

pool_size = 10


def lookup(host: bytes, cb: Callable[..., None]) -> None:
    f = _pool.submit(_lookup, host)
    cb(f.result())


def _lookup(host: bytes) -> Union[str, Exception]:
    try:
        return socket.gethostbyname(host.decode("idna"))
    except Exception as why:
        return why


_pool = ThreadPoolExecutor(max_workers=pool_size)
