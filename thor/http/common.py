#!/usr/bin/env python

"""
Thor shared HTTP infrastructure

This module contains utility functions and a base class
for the parsing portions of the HTTP client and server.
"""

from collections import defaultdict
from enum import Enum
from typing import Callable, Dict, List, Set, Tuple, Union  # pylint: disable=unused-import

from thor.http import error

linesep = b"\r\n"
RawHeaderListType = List[Tuple[bytes, bytes]]
OriginType = Tuple[bytes, bytes, int]

NEWLINE = ord("\n")
RETURN = ord("\r")

class Delimiters(Enum):
    CLOSE = 'close'
    COUNTED = 'counted'
    CHUNKED = 'chunked'
    NOBODY = 'nobody'

class States(Enum):
    WAITING = 1
    HEADERS_DONE = 2
    ERROR = 3
    QUIET = 4

idempotent_methods = [b'GET', b'HEAD', b'PUT', b'DELETE', b'OPTIONS', b'TRACE']
safe_methods = [b'GET', b'HEAD', b'OPTIONS', b'TRACE']
no_body_status = [b'204', b'304']
hop_by_hop_hdrs = [b'connection', b'keep-alive', b'proxy-authenticate',
                   b'proxy-authorization', b'te', b'trailers',
                   b'transfer-encoding', b'upgrade', b'proxy-connection']



def header_names(hdr_tuples: RawHeaderListType)-> Set[bytes]:
    """
    Given a list of header tuples, return the set of the header names seen.
    """
    return set([n.lower() for n, v in hdr_tuples])

def header_dict(hdr_tuples: RawHeaderListType,
                omit: List[bytes] = None) -> Dict[bytes, List[bytes]]:
    """
    Given a list of header tuples, return a dictionary keyed upon the
    lower-cased header names.

    If omit is defined, each header listed (by lower-cased name) will not be
    returned in the dictionary.
    """
    out = defaultdict(list)  # type: Dict[bytes, List[bytes]]
    for (n, v) in hdr_tuples:
        n = n.lower()
        if n in (omit or []):
            continue
        out[n].extend([i.strip() for i in v.split(b",")])
    return out

def get_header(hdr_tuples: RawHeaderListType, name: bytes) -> List[bytes]:
    """
    Given a list of (name, value) header tuples and a header name (lowercase),
    return a list of all values for that header.

    This includes header lines with multiple values separated by a comma;
    such headers will be split into separate values. As a result, it is NOT
    safe to use this on headers whose values may include a comma (e.g.,
    Set-Cookie, or any value with a quoted string).
    """
    # TODO: support quoted strings
    return [v.strip() for v in sum(
        [l.split(b",") for l in [i[1] for i in hdr_tuples if i[0].lower() == name]], [])]


class HttpMessageHandler:
    """
    This is a base class for something that has to parse and/or serialise
    HTTP messages, request or response.

    For parsing, it expects you to override input_start, input_body and
    input_end, and call handle_input when you get bytes from the network.

    For serialising, it expects you to override output.
    """

    careful = True # if False, don't fail on errors, but preserve them.
    default_state = None # type: States  # QUIET or WAITING

    def __init__(self) -> None:
        self.input_header_length = 0
        self.input_transfer_length = 0
        self._input_buffer = []  # type: List[bytes]
        self._input_state = self.default_state
        self._input_delimit = None  # type: Delimiters
        self._input_body_left = 0
        self._output_state = States.WAITING
        self._output_delimit = None  # type: Delimiters

    def __repr__(self) -> str:
        return "input %s output %s" % (self._input_state, self._output_state)

    # input-related methods

    def input_start(self, top_line: bytes, hdr_tuples: RawHeaderListType,
                    conn_tokens: List[bytes], transfer_codes: List[bytes],
                    content_length: int) -> Tuple[bool, bool]:
        """
        Take the top set of headers from the input stream, parse them
        and queue the request to be processed by the application.

        Returns booleans (allows_body, is_final) to indicate whether the message allows a
        body, and whether it's the final message (respectively).

        Can raise ValueError to indicate that there's a problem and parsing
        cannot continue.
        """
        raise NotImplementedError

    def input_body(self, chunk: bytes) -> None:
        """
        Process a body chunk from the wire.

        Chunk is a bytes.
        """
        raise NotImplementedError

    def input_end(self, trailers: RawHeaderListType) -> None:
        """
        Indicate that the response body is complete. Optionally can contain
        trailers.
        """
        raise NotImplementedError

    def input_error(self, err: error.HttpError) -> None:
        """
        Indicate an unrecoverable parsing problem with the input stream.
        """
        raise NotImplementedError

    def handle_input(self, inbytes: bytes) -> None:
        """
        Given a bytes representing a chunk of input, figure out what state
        we're in and handle it, making the appropriate calls.
        """
        if self._input_buffer:
            self._input_buffer.append(inbytes)
            inbytes = b"".join(self._input_buffer)
            self._input_buffer = []
        if self._input_state == States.WAITING:  # waiting for headers or trailers
            headers, rest = self._split_headers(inbytes)
            if headers is not None: # found one
                if self._parse_headers(headers):
                    try:
                        self.handle_input(rest)
                    except RuntimeError:
                        self.input_error(error.TooManyMsgsError())
                        # we can't recover from this, so we bail.
            else: # partial headers; store it and wait for more
                self._input_buffer.append(inbytes)
        elif self._input_state == States.QUIET:  # shouldn't be getting any data now.
            if inbytes.strip():
                self.input_error(error.ExtraDataError(inbytes.decode('utf-8', 'replace')))
        elif self._input_state == States.HEADERS_DONE:  # we found a complete header/trailer set
            try:
                body_handler = getattr(self, '_handle_%s' % self._input_delimit.value)
            except AttributeError:
                raise Exception("Unknown input delimiter %s" % \
                                 self._input_delimit)
            body_handler(inbytes)
        elif self._input_state == States.ERROR:  # something bad happened.
            pass # I'm silently ignoring input that I don't understand.
        else:
            raise Exception("Unknown state %s" % self._input_state)

    def _handle_nobody(self, inbytes: bytes) -> None:
        "Handle input that shouldn't have a body."
        self._input_state = self.default_state
        self.input_end([])
        self.handle_input(inbytes)

    def _handle_close(self, inbytes: bytes) -> None:
        "Handle input where the body is delimited by the connection closing."
        self.input_transfer_length += len(inbytes)
        self.input_body(inbytes)

    def _handle_chunked(self, inbytes: bytes) -> None:
        "Handle input where the body is delimited by chunked encoding."
        while inbytes:
            if self._input_body_left < 0: # new chunk
                inbytes = self._handle_chunk_new(inbytes)
            elif self._input_body_left > 0:
                # we're in the middle of reading a chunk
                inbytes = self._handle_chunk_body(inbytes)
            elif self._input_body_left == 0: # body is done
                self._handle_chunk_done(inbytes)
                break

    def _handle_chunk_new(self, inbytes: bytes) -> bytes:
        "Handle the start of a new body chunk."
        try:
            chunk_size, rest = inbytes.split(b"\r\n", 1)
        except ValueError:
            # don't have the whole chunk_size yet... wait a bit
            if len(inbytes) > 512:
                # OK, this is absurd...
                self.input_error(error.ChunkError(inbytes.decode('utf-8', 'replace')))
                # TODO: need testing around this; catching the right thing?
            else:
                self._input_buffer.append(inbytes)
            return b''
        # TODO: do we need to ignore blank lines?
        if b";" in chunk_size: # ignore chunk extensions
            chunk_size = chunk_size.split(b";", 1)[0]
        try:
            self._input_body_left = int(chunk_size, 16)
        except ValueError:
            self.input_error(error.ChunkError(chunk_size.decode('utf-8', 'replace')))
            return b''
        self.input_transfer_length += len(inbytes) - len(rest)
        return rest

    def _handle_chunk_body(self, inbytes: bytes) -> bytes:
        "Handle a continuing body chunk."
        got = len(inbytes)
        if self._input_body_left + 2 < got: # got more than the chunk
            this_chunk = self._input_body_left
            self.input_body(inbytes[:this_chunk])
            self.input_transfer_length += this_chunk + 2
            self._input_body_left = -1
            return inbytes[this_chunk + 2:] # +2 consumes the trailing CRLF
        if self._input_body_left + 2 == got:
            # got the whole chunk exactly (including CRLF)
            self.input_body(inbytes[:-2])
            self.input_transfer_length += self._input_body_left + 2
            self._input_body_left = -1
        elif self._input_body_left == got: # corner case
            self._input_buffer.append(inbytes)
        else: # got partial chunk
            self.input_body(inbytes)
            self.input_transfer_length += got
            self._input_body_left -= got
        return b''

    def _handle_chunk_done(self, inbytes: bytes) -> None:
        "Handle a finished body chunk."
        if inbytes[:2] == b"\r\n": # no trailer
            self._input_state = self.default_state
            self.input_end([])
            if len(inbytes) > 2:
                self.handle_input(inbytes[2:]) # 2 consumes the CRLF
        else:
            trailer_block, rest = self._split_headers(inbytes) # trailers
            if trailer_block is not None:
                self._input_state = self.default_state
                try:
                    trailers = self._parse_fields(trailer_block.splitlines())[0]
                except ValueError:
                    self._input_state = States.ERROR # TODO: need an explicit error
                    return
                else:
                    self.input_end(trailers)
                    self.handle_input(rest)
            else: # don't have full trailers yet
                self._input_buffer.append(inbytes)

    def _handle_counted(self, inbytes: bytes) -> None:
        "Handle input where the body is delimited by the Content-Length."
        if self._input_body_left <= len(inbytes): # got it all (and more?)
            self.input_transfer_length += self._input_body_left
            self.input_body(inbytes[:self._input_body_left])
            self.input_end([])
            self._input_state = self.default_state
            if inbytes[self._input_body_left:]:
                self.handle_input(inbytes[self._input_body_left:])
        else: # got some of it
            self.input_body(inbytes)
            self.input_transfer_length += len(inbytes)
            self._input_body_left -= len(inbytes)

    def _parse_fields(self, header_lines: List[bytes], gather_conn_info: bool = False) -> \
                      Tuple[RawHeaderListType, List[bytes], List[bytes], int]:
        """
        Given a list of raw header lines (without the top line,
        and without the trailing CRLFCRLF), return its header tuples.
        """

        hdr_tuples = []        # type: RawHeaderListType
        conn_tokens = []       # type: List[bytes]
        transfer_codes = []    # type: List[bytes]
        content_length = None  # type: int

        for line in header_lines:
            if line[:1] in [b" ", b"\t"]: # Fold LWS
                if hdr_tuples:
                    hdr_tuples[-1] = (
                        hdr_tuples[-1][0],
                        b"%s %s" % (hdr_tuples[-1][1], line.lstrip())
                    )
                    continue
                else: # top header starts with whitespace
                    self.input_error(error.TopLineSpaceError(line.decode('utf-8', 'replace')))
                    if self.careful:
                        raise ValueError
            try:
                fn, fv = line.split(b":", 1)
            except ValueError:
                continue # TODO: error on unparseable field?
            # TODO: a zero-length name isn't valid
            if fn[-1:] in [b" ", b"\t"]:
                self.input_error(error.HeaderSpaceError(fn.decode('utf-8', 'replace')))
                if self.careful:
                    raise ValueError
            hdr_tuples.append((fn, fv))

            if gather_conn_info:
                f_name = fn.strip().lower()
                f_val = fv.strip()

                # parse connection-related headers
                if f_name == b"connection":
                    conn_tokens += [
                        v.strip().lower() for v in f_val.split(b',')
                    ]
                elif f_name == b"transfer-encoding": # TODO: parameters? no...
                    transfer_codes += [v.strip().lower() for \
                                       v in f_val.split(b',')]
                elif f_name == b"content-length":
                    if content_length is not None:
                        try:
                            if int(f_val) == content_length:
                                # we have a duplicate, non-conflicting c-l.
                                continue
                        except ValueError:
                            pass
                        self.input_error(error.DuplicateCLError())
                        if self.careful:
                            raise ValueError
                    try:
                        content_length = int(f_val)
                        assert content_length >= 0
                    except (ValueError, AssertionError):
                        self.input_error(error.MalformedCLError(f_val.decode('utf-8', 'replace')))
                        if self.careful:
                            raise ValueError

        return hdr_tuples, conn_tokens, transfer_codes, content_length

    @staticmethod
    def _split_headers(inbytes: bytes) -> Tuple[bytes, bytes]:
        """
        Given a bytes, split out and return (headers, rest),
        consuming the whitespace between them.

        If there is not a complete header block, return None for headers.
        """

        pos = 0
        size = len(inbytes)
        while pos <= size:
            pos = inbytes.find(b"\n", pos)
            back = 0
            if pos == -1:
                return None, inbytes
            if pos > 0 and inbytes[pos - 1] == RETURN:
                back += 1
            pos += 1
            if pos < size:
                if inbytes[pos] == RETURN:
                    pos += 1
                    back += 1
                if pos < size and inbytes[pos] == NEWLINE:
                    return inbytes[:pos - back - 1], inbytes[pos + 1:]
        return None, inbytes

    def _parse_headers(self, inbytes: bytes) -> bool:
        """
        Given a bytes that we knows starts with a header block,
        parse the headers. Calls self.input_start to kick off processing.

        Returns True if no fatal problems are found.
        """
        self.input_header_length = len(inbytes)
        header_lines = inbytes.splitlines()

        # chop off the top line
        while True: # TODO: limit?
            try:
                top_line = header_lines.pop(0)
                if top_line.strip() != b"":
                    break
            except IndexError: # empty
                return True

        try:
            hdr_tuples, conn_tokens, transfer_codes, content_length \
                = self._parse_fields(header_lines, True)
        except ValueError: # returned empty because there was an error
            return False # throw away the rest

        # ignore content-length if transfer-encoding is present
        if transfer_codes != [] and content_length is not None:
            content_length = None

        try:
            allows_body, is_final = self.input_start(top_line, hdr_tuples, conn_tokens,
                                                     transfer_codes, content_length)
        except ValueError: # fatal parsing error of some kind; abort.
            return False # throw away the rest

        if not is_final:
            self._input_state = States.WAITING
        else:
            self._input_state = States.HEADERS_DONE
        if not allows_body:
            self._input_delimit = Delimiters.NOBODY
        elif transfer_codes:
            if transfer_codes[-1] == b'chunked':
                self._input_delimit = Delimiters.CHUNKED
                self._input_body_left = -1 # flag that we don't know
            else:
                self._input_delimit = Delimiters.CLOSE
        elif content_length is not None:
            self._input_delimit = Delimiters.COUNTED
            self._input_body_left = content_length
        else:
            self._input_delimit = Delimiters.CLOSE
        return True

    ### output-related methods

    def output(self, data: bytes) -> None:
        """
        Write something to whatever we're talking to. Should be overridden.
        """
        raise NotImplementedError

    def output_start(self, top_line: bytes, hdr_tuples: RawHeaderListType,
                     delimit: Delimiters) -> None:
        """
        Start outputting a HTTP message.
        """
        self._output_delimit = delimit
        out = [top_line]
        out.extend([b"%s: %s" % (k.strip(), v) for k, v in hdr_tuples])
        out.extend([b"", b""])
        self.output(linesep.join(out))
        self._output_state = States.HEADERS_DONE

    def output_body(self, chunk: bytes) -> None:
        """
        Output a part of a HTTP message. Takes bytes.
        """
        if not chunk or self._output_delimit is None:
            return
        if self._output_delimit == Delimiters.CHUNKED:
            chunk = b"%s\r\n%s\r\n" % (hex(len(chunk))[2:].encode('ascii'), chunk)
        self.output(chunk)
        # TODO: body counting
#        self._output_body_sent += len(chunk)
#        assert self._output_body_sent <= self._output_content_length, \
#            "Too many body bytes sent"

    def output_end(self, trailers: RawHeaderListType) -> bool:
        """
        Finish outputting a HTTP message, including trailers if appropriate.
        Return value incicates whether the connection should be closed.
        """
        if self._output_delimit == Delimiters.NOBODY:
            pass # didn't have a body at all.
        elif self._output_delimit == Delimiters.CHUNKED:
            self.output(b"0\r\n%s\r\n" % b"\r\n".join([
                b"%s: %s" % (k.strip(), v) for k, v in trailers
            ]))
        elif self._output_delimit == Delimiters.COUNTED:
            pass # TODO: double-check the length
        elif self._output_delimit == Delimiters.CLOSE:
            return True
        elif self._output_delimit is None:
            return True # encountered an error before we found a delimiter
        else:
            raise AssertionError("Unknown request delimiter %s" % self._output_delimit)
        self._output_state = States.WAITING
        return False
