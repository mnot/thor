#!/usr/bin/env python

"""
Thor HTTP Errors
"""

from typing import Optional, Tuple


class HttpError(Exception):
    desc = "Unknown Error"
    server_status: Tuple[bytes, bytes]  # status this produces in a server
    server_recoverable = False  # whether a server can recover the connection
    client_recoverable = False  # whether a client can recover the connection

    def __init__(self, detail: Optional[str] = None) -> None:
        Exception.__init__(self)
        self.detail = detail

    def __repr__(self) -> str:
        status = [self.__class__.__module__ + "." + self.__class__.__name__]
        return f"<{', '.join(status)} at {id(self):#x}>"


# General parsing errors


class ChunkError(HttpError):
    desc = "Chunked encoding error"


class DuplicateCLError(HttpError):
    desc = "Duplicate Content-Length header"
    server_status = (b"400", b"Bad Request")
    client_recoverable = True


class MalformedCLError(HttpError):
    desc = "Malformed Content-Length header"
    server_status = (b"400", b"Bad Request")


class ExtraDataError(HttpError):
    desc = "Extra data was sent after this message was supposed to end"


class StartLineError(HttpError):
    desc = "The start line of the message couldn't be parsed"


class HttpVersionError(HttpError):
    desc = "Unrecognised HTTP version"
    server_status = (b"505", b"HTTP Version Not Supported")


class ReadTimeoutError(HttpError):
    desc = "Read Timeout"


class TransferCodeError(HttpError):
    desc = "Unknown request transfer coding"
    server_status = (b"501", b"Not Implemented")


class HeaderSpaceError(HttpError):
    desc = "Whitespace at the end of a header field-name"
    server_status = (b"400", b"Bad Request")
    client_recoverable = True


class TopLineSpaceError(HttpError):
    desc = "Whitespace after top line, before first header"
    server_status = (b"400", b"Bad Request")
    client_recoverable = True


class TooManyMsgsError(HttpError):
    desc = "Too many messages to parse"
    server_status = (b"400", b"Bad Request")


# client-specific errors


class UrlError(HttpError):
    desc = "Unsupported or invalid URI"
    server_status = (b"400", b"Bad Request")


class LengthRequiredError(HttpError):
    desc = "Content-Length required"
    server_status = (b"411", b"Length Required")
    client_recoverable = True


class DnsError(HttpError):
    desc = "DNS Error"
    server_status = (b"502", b"Bad Gateway")


class ConnectError(HttpError):
    desc = "Connection error"
    server_status = (b"504", b"Gateway Timeout")


class AccessError(HttpError):
    desc = "Access Error"
    server_status = (b"403", b"Forbidden")


# server-specific errors


class HostRequiredError(HttpError):
    desc = "Host header required"
    server_recoverable = True
