from __future__ import annotations
import socket
from typing import Callable, Union, TYPE_CHECKING

from thor.dns import lookup, DnsResultList
from thor.tcp import TcpClient, TcpConnection
from thor.tls import TlsClient
from thor.http.common import OriginType
from .connection import HttpClientConnection

if TYPE_CHECKING:
    from .client import HttpClient


def initiate_connection(
    client: HttpClient,
    origin: OriginType,
    handle_connect: Callable[[HttpClientConnection], None],
    handle_error: Callable[[str, int, str], None],
) -> None:
    """
    Creates a new TCP connection to an origin.
    """
    attempts = 0
    dns_results: DnsResultList = []

    def handle_dns(results: Union[DnsResultList, Exception]) -> None:
        nonlocal dns_results
        if isinstance(results, Exception):
            err_id = results.args[0]
            err_str = results.args[1]
            handle_error("gai", err_id, err_str)
        else:
            dns_results = results
            initiate_internal()

    def initiate_internal() -> None:
        nonlocal attempts
        dns_result = dns_results[attempts % len(dns_results)]
        (scheme, host, _) = origin
        if scheme == "http":
            tcp_client: Union[TcpClient, TlsClient] = TcpClient(client.loop)
        elif scheme == "https":
            tcp_client = TlsClient(client.loop)
        else:
            raise ValueError(f"unknown scheme {scheme}")
        tcp_client.check_ip = client.check_ip
        tcp_client.once("connect", handle_connect_cb)
        tcp_client.once("connect_error", handle_connect_error_cb)
        attempts += 1
        tcp_client.connect_dns(host.encode("idna"), dns_result, client.connect_timeout)

    def handle_connect_cb(tcp_conn: TcpConnection) -> None:
        client.conn_counts[origin] += 1
        conn = HttpClientConnection(client, origin, tcp_conn)
        handle_connect(conn)

    def handle_connect_error_cb(err_type: str, err_id: int, err_str: str) -> None:
        if err_type in ["access"]:
            handle_error(err_type, err_id, err_str)
        elif attempts > client.connect_attempts:
            handle_error("retry", attempts, "Too many connection attempts")
        else:
            client.loop.schedule(0, initiate_internal)

    (_, host, port) = origin
    lookup(host.encode("idna"), port, socket.SOCK_STREAM, handle_dns)
