import socket
from typing import Any, Callable, List, Tuple, Union

EventListener = Callable[..., Any]
ScheduledEventTuple = Tuple[float, EventListener]

RawHeaderListType = List[Tuple[bytes, bytes]]
OriginType = Tuple[str, str, int]

Address = Union[Tuple[str, int], Tuple[str, int, int, int]]
DnsResult = Tuple[
    socket.AddressFamily,  # pylint: disable=no-member
    socket.SocketKind,  # pylint: disable=no-member
    int,
    str,
    Address,
]
DnsResultList = List[DnsResult]
