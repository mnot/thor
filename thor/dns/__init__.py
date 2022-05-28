#!/usr/bin/env python

from concurrent.futures import Future, ThreadPoolExecutor
from itertools import cycle, islice
import socket
from typing import Callable, Union, Tuple, List, Iterable, Any

pool_size = 10

Address = Union[Tuple[str, int], Tuple[str, int, int, int]]
DnsResult = Tuple[
    socket.AddressFamily,
    socket.SocketKind,
    int,
    str,
    Address,
]
DnsResultList = List[DnsResult]


def lookup(host: bytes, port: int, proto: int, cb: Callable[..., None]) -> None:
    f = _pool.submit(_lookup, host, port, proto)

    def done(ff: Future) -> None:
        cb(ff.result())

    f.add_done_callback(done)


def _lookup(host: bytes, port: int, socktype: int) -> Union[DnsResultList, Exception]:
    family = 0
    if not socket.has_ipv6:
        family = socket.AF_INET

    try:
        results = socket.getaddrinfo(host, port, type=socktype, family=family)
    except Exception as why:
        return why
    return _sortDnsResults(results)


def _sortDnsResults(results: DnsResultList) -> DnsResultList:
    ipv4Results = []
    ipv6Results = []
    for result in results:
        if result[0] is socket.AF_INET:
            ipv4Results.append(result)
        if result[0] is socket.AF_INET6:
            ipv6Results.append(result)
    return list(_roundrobin(ipv6Results, ipv4Results))


def _roundrobin(*iterables: Iterable) -> Iterable[Any]:
    "roundrobin('ABC', 'D', 'EF') --> A D E B F C"
    # Recipe credited to George Sakkis
    num_active = len(iterables)
    nexts = cycle(iter(it).__next__ for it in iterables)
    while num_active:
        try:
            for next in nexts:
                yield next()
        except StopIteration:
            # Remove the iterator we just exhausted from the cycle.
            num_active -= 1
            nexts = cycle(islice(nexts, num_active))


_pool = ThreadPoolExecutor(max_workers=pool_size)
