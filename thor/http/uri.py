from urllib.parse import urlsplit, urlunsplit
from string import ascii_letters, digits
from typing import Tuple

from thor.http.error import UrlError


def parse_uri(uri: bytes) -> Tuple[str, str, int, bytes, bytes]:
    """
    Given a uri, parse out the host, port, authority and request target.
    Returns None if there is an error, otherwise the origin.
    """
    try:
        (schemeb, authority, path, query, _) = urlsplit(uri)
    except UnicodeDecodeError:
        raise UrlError("URL has non-ascii characters")
    except ValueError as why:
        raise UrlError(why.args[0])
    try:
        scheme = schemeb.decode("utf-8").lower()
    except UnicodeDecodeError:
        raise UrlError("URL scheme has non-ascii characters")
    if scheme == "http":
        default_port = 80
    elif scheme == "https":
        default_port = 443
    else:
        raise UrlError(f"Unsupported URL scheme '{scheme}'")
    if b"@" in authority:
        authority = authority.split(b"@", 1)[1]
    portb = None
    ipv6_literal = False
    if authority.startswith(b"["):
        ipv6_literal = True
        try:
            delimiter = authority.index(b"]")
        except ValueError:
            raise UrlError("IPv6 URL missing ]")
        hostb = authority[1:delimiter]
        rest = authority[delimiter + 1 :]
        if rest.startswith(b":"):
            portb = rest[1:]
    elif b":" in authority:
        hostb, portb = authority.rsplit(b":", 1)
    else:
        hostb = authority
    if portb:
        try:
            port = int(portb.decode("utf-8", "replace"))
        except ValueError:
            raise UrlError(
                f"Non-integer port '{portb.decode('utf-8', 'replace')}' in URL"
            )
        if not 1 <= port <= 65535:
            raise UrlError(f"URL port {port} out of range")
    else:
        port = default_port
    try:
        host = hostb.decode("ascii", "strict")
    except UnicodeDecodeError:
        raise UrlError("URL host has non-ascii characters")
    if ipv6_literal:
        if not all(c in digits + ":abcdefABCDEF" for c in host):
            raise UrlError("URL IPv6 literal has disallowed character")
    else:
        if not all(c in ascii_letters + digits + ".-" for c in host):
            raise UrlError("URL hostname has disallowed character")
        labels = host.split(".")
        if any(len(l) == 0 for l in labels):
            raise UrlError("URL hostname has empty label")
        if any(len(l) > 63 for l in labels):
            raise UrlError("URL hostname label greater than 63 characters")
        #        if any(l[0].isdigit() for l in labels):
        #            self.input_error(UrlError("URL hostname label starts with digit"), False)
        #            raise ValueError
    if len(host) > 255:
        raise UrlError("URL hostname greater than 255 characters")
    if path == b"":
        path = b"/"
    req_target = urlunsplit((b"", b"", path, query, b""))
    return scheme, host, port, authority, req_target
