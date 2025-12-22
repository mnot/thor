from __future__ import annotations
import socket
from typing import Callable, Union, TYPE_CHECKING

from thor.dns import lookup, DnsResultList
from thor.tcp import TcpClient, TcpConnection
from thor.tls import TlsClient
from thor.http.common import OriginType

if TYPE_CHECKING:
    from .client import HttpClient

class HttpConnectionInitiate:  # pylint: disable=too-few-public-methods
    """
    Creates a new TCP connection to an origin.
    """

    tcp_client_class = TcpClient
    tls_client_class = TlsClient

    def __init__(
        self,
        client: HttpClient,
        origin: OriginType,
        handle_connect: Callable[[TcpConnection], None],
        handle_error: Callable[[str, int, str], None],
    ) -> None:
        self.client = client
        self.origin = origin
        self.handle_connect = handle_connect
        self.handle_error = handle_error
        self._attempts = 0
        self._dns_results: DnsResultList = []
        (_, host, port) = origin
        lookup(host.encode("idna"), port, socket.SOCK_STREAM, self._handle_dns)

    def _handle_dns(self, dns_results: Union[DnsResultList, Exception]) -> None:
        """
        Handle the DNS response.
        """
        if isinstance(dns_results, Exception):
            err_id = dns_results.args[0]
            err_str = dns_results.args[1]
            self.handle_error("gai", err_id, err_str)
        else:
            self._dns_results = dns_results
            self._initiate_connection()

    def _initiate_connection(self) -> None:
        """
        Attempt to open a connection.
        """
        dns_result = self._dns_results[self._attempts % len(self._dns_results)]
        (scheme, host, _) = self.origin
        tcp_client: Union[TcpClient, TlsClient]
        if scheme == "http":
            tcp_client = self.tcp_client_class(self.client.loop)
        elif scheme == "https":
            tcp_client = self.tls_client_class(self.client.loop)
        else:
            raise ValueError(f"unknown scheme {scheme}")
        tcp_client.check_ip = self.client.check_ip
        tcp_client.once("connect", self._handle_connect)
        tcp_client.once("connect_error", self._handle_connect_error)
        self._attempts += 1
        tcp_client.connect_dns(
            host.encode("idna"), dns_result, self.client.connect_timeout
        )

    def _handle_connect(self, tcp_conn: TcpConnection) -> None:
        """
        A connection succeeded.
        """
        self.client.conn_counts[self.origin] += 1
        self.handle_connect(tcp_conn)

    def _handle_connect_error(self, err_type: str, err_id: int, err_str: str) -> None:
        """
        A connection failed.
        """
        if err_type in ["access"]:
            self.handle_error(err_type, err_id, err_str)
        elif self._attempts > self.client.connect_attempts:
            self.handle_error("retry", self._attempts, "Too many connection attempts")
        else:
            self.client.loop.schedule(0, self._initiate_connection)
