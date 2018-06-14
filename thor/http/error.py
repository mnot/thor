#!/usr/bin/env python

"""
Thor HTTP Errors
"""

from typing import Tuple # pylint: disable=unused-import


class HttpError(Exception):
    desc = u"Unknown Error"
    server_status = None # type: Tuple[bytes, bytes]  # status this produces in a server
    server_recoverable = False # whether a server can recover the connection
    client_recoverable = False # whether a client can recover the connection

    def __init__(self, detail: str = None) -> None:
        Exception.__init__(self)
        self.detail = detail

    def __repr__(self) -> str:
        status = [self.__class__.__module__ + "." + self.__class__.__name__]
        return "<%s at %#x>" % (", ".join(status), id(self))

# General parsing errors

class ChunkError(HttpError):
    desc = u"Chunked encoding error"

class DuplicateCLError(HttpError):
    desc = u"Duplicate Content-Length header"
    server_status = (b"400", b"Bad Request")
    client_recoverable = True

class MalformedCLError(HttpError):
    desc = u"Malformed Content-Length header"
    server_status = (b"400", b"Bad Request")

class ExtraDataError(HttpError):
    desc = u"Extra data was sent after this message was supposed to end"

class StartLineError(HttpError):
    desc = u"The start line of the message couldn't be parsed"

class HttpVersionError(HttpError):
    desc = u"Unrecognised HTTP version"
    server_status = (b"505", b"HTTP Version Not Supported")

class ReadTimeoutError(HttpError):
    desc = u"Read Timeout"

class TransferCodeError(HttpError):
    desc = u"Unknown request transfer coding"
    server_status = (b"501", b"Not Implemented")

class HeaderSpaceError(HttpError):
    desc = u"Whitespace at the end of a header field-name"
    server_status = (b"400", b"Bad Request")
    client_recoverable = True

class TopLineSpaceError(HttpError):
    desc = u"Whitespace after top line, before first header"
    server_status = (b"400", b"Bad Request")
    client_recoverable = True

class TooManyMsgsError(HttpError):
    desc = u"Too many messages to parse"
    server_status = (b"400", b"Bad Request")

# client-specific errors

class UrlError(HttpError):
    desc = u"Unsupported or invalid URI"
    server_status = (b"400", b"Bad Request")

class LengthRequiredError(HttpError):
    desc = u"Content-Length required"
    server_status = (b"411", b"Length Required")
    client_recoverable = True

class ConnectError(HttpError):
    desc = u"Connection error"
    server_status = (b"504", b"Gateway Timeout")

# server-specific errors

class HostRequiredError(HttpError):
    desc = u"Host header required"
    server_recoverable = True
