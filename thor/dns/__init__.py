#!/usr/bin/env python

from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
import socket
from typing import Callable, Union, Tuple, List

pool_size = 10

DnsResult = Tuple[socket.AddressFamily, socket.SocketKind, int, str, Tuple[str, int]]
DnsResultList = List[DnsResult]


def lookup(host: bytes, port: int, proto: int, cb: Callable[..., None]) -> None:
    f = _pool.submit(_lookup, host, port, proto)

    def done(ff: Future) -> None:
        cb(ff.result())

    f.add_done_callback(done)


def _lookup(host: bytes, port: int, socktype: int) -> Union[DnsResultList, Exception]:
    try:
        return socket.getaddrinfo(host, port, type=socktype)  # type: ignore
    except Exception as why:
        return why


def pickDnsResult(results: DnsResultList) -> DnsResult:
    table = defaultdict(list)
    for result in results:
        table[result[0]].append(result)

    if socket.has_ipv6 and socket.AF_INET6 in table:
        return table[socket.AF_INET6][0]
    else:
        return table[socket.AF_INET][0]


_pool = ThreadPoolExecutor(max_workers=pool_size)
