#!/usr/bin/env python

from concurrent.futures import Future, ThreadPoolExecutor
from itertools import cycle, islice
import socket
from typing import Callable, Union, Tuple, List, Iterable, Any

POOL_SIZE = 10

Address = Union[Tuple[str, int], Tuple[str, int, int, int]]
DnsResult = Tuple[
    socket.AddressFamily,  # pylint: disable=no-member
    socket.SocketKind,  # pylint: disable=no-member
    int,
    str,
    Address,
]
DnsResultList = List[DnsResult]


def lookup(host: bytes, port: int, proto: int, cb: Callable[..., None]) -> None:
    job = _pool.submit(_lookup, host, port, proto)

    def done(ff: Future) -> None:
        cb(ff.result())

    job.add_done_callback(done)


def _lookup(host: bytes, port: int, socktype: int) -> Union[DnsResultList, Exception]:
    family = 0
    if not socket.has_ipv6:
        family = socket.AF_INET

    try:
        results = socket.getaddrinfo(host, port, type=socktype, family=family)
    except socket.error as why:
        return why
    return _sort_dns_results(results)


def _sort_dns_results(results: DnsResultList) -> DnsResultList:
    ipv4results = []
    ipv6results = []
    for result in results:
        if result[0] is socket.AF_INET:
            ipv4results.append(result)
        if result[0] is socket.AF_INET6:
            ipv6results.append(result)
    return list(_roundrobin(ipv6results, ipv4results))


def _roundrobin(*iterables: Iterable) -> Iterable[Any]:
    "roundrobin('ABC', 'D', 'EF') --> A D E B F C"
    # Recipe credited to George Sakkis
    num_active = len(iterables)
    nexts = cycle(iter(it).__next__ for it in iterables)
    while num_active:
        try:
            for nex in nexts:
                yield nex()
        except StopIteration:
            # Remove the iterator we just exhausted from the cycle.
            num_active -= 1
            nexts = cycle(islice(nexts, num_active))


_pool = ThreadPoolExecutor(max_workers=POOL_SIZE)
