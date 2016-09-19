#!/usr/bin/env python

"""
Thor HTTP Errors
"""

from __future__ import absolute_import

import types

class HttpError(Exception):
    desc = "Unknown Error"
    server_status = None # status this produces when it occurs in a server
    server_recoverable = False # whether a server can recover the connection
    client_recoverable = False # whether a client can recover the connection

    def __init__(self, detail=None):
        Exception.__init__(self)
#        if detail and type(detail) != str:
#            detail = unicode(detail, "utf-8", "replace")
        self.detail = detail

    def __repr__(self):
        status = [self.__class__.__module__ + "." + self.__class__.__name__]
        if self.detail:
            status.append(self.detail)
        return "<%s at %#x>" % (", ".join(status), id(self))

# General parsing errors

class ChunkError(HttpError):
    desc = "Chunked encoding error"

class DuplicateCLError(HttpError):
    desc = "Duplicate Content-Length header"
    server_status = ("400", "Bad Request")
    client_recoverable = True

class MalformedCLError(HttpError):
    desc = "Malformed Content-Length header"
    server_status = ("400", "Bad Request")

class ExtraDataError(HttpError):
    desc = "Extra data was sent after this message was supposed to end"

class HttpVersionError(HttpError):
    desc = "Unrecognised HTTP version"
    server_status = ("505", "HTTP Version Not Supported")

class StartLineEncodingError(HttpError):
    desc = "Disallowed characters in start-line"
    server_recoverable = True
    client_recoverable = True
    
class ReadTimeoutError(HttpError):
    desc = "Read Timeout"

class TransferCodeError(HttpError):
    desc = "Unknown request transfer coding"
    server_status = ("501", "Not Implemented")

class HeaderSpaceError(HttpError):
    desc = "Whitespace at the end of a header field-name"
    server_status = ("400", "Bad Request")
    client_recoverable = True
    
class TopLineSpaceError(HttpError):
    desc = "Whitespace after top line, before first header"
    server_status = ("400", "Bad Request")
    client_recoverable = True

class TooManyMsgsError(HttpError):
    desc = "Too many messages to parse"
    server_status = ("400", "Bad Request")

# client-specific errors

class UrlError(HttpError):
    desc = "Unsupported or invalid URI"
    server_status = ("400", "Bad Request")
    client_recoverable = True

class LengthRequiredError(HttpError):
    desc = "Content-Length required"
    server_status = ("411", "Length Required")
    client_recoverable = True

class ConnectError(HttpError):
    desc = "Connection error"
    server_status = ("504", "Gateway Timeout")

# server-specific errors

class HostRequiredError(HttpError):
    desc = "Host header required"
    server_recoverable = True
