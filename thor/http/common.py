#!/usr/bin/env python

"""
Thor shared HTTP infrastructure

This module contains utility functions and a base class
for the parsing portions of the HTTP client and server.
"""

# pylint: disable=raise-missing-from

import ipaddress
import re
from abc import ABCMeta, abstractmethod
from collections import defaultdict
from enum import Enum
from typing import Dict, Iterable, List, NoReturn, Optional, Set, Tuple

from thor.http import error
from thor.types import RawHeaderListType

LINESEP = b"\r\n"
NEWLINE = ord("\n")
RETURN = ord("\r")
_BAD_OUTPUT_HEADER_NAME = set(b'()<>@,;:\\"/[]?={} \t\r\n')
_BAD_OUTPUT_HEADER_VALUE = {RETURN, NEWLINE}
_DIGITS = re.compile(rb"[0-9]+")
_TOKEN = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")  # RFC 9110 section 5.6.2
_HEX_DIGITS = re.compile(rb"[0-9A-Fa-f]+")
# RFC 3986 host and optional port, split for check_host: a bracketed
# IP-literal or a reg-name (an IPv4 address is one), then the port.
_HOST = re.compile(
    rb"(\[[^\]]*\]|(?:[A-Za-z0-9\-._~!$&'()*+,;=]|%[0-9A-Fa-f]{2})+)(?::([0-9]*))?"
)
_IP_FUTURE = re.compile(rb"[vV][0-9A-Fa-f]+\.[A-Za-z0-9\-._~!$&'()*+,;=:]+")
_MAX_PORT = 65535
_MAX_OUTPUT_CL_DIGITS = 20  # enough for any 64-bit length


class Delimiters(Enum):
    CLOSE = "close"
    COUNTED = "counted"
    CHUNKED = "chunked"
    NOBODY = "nobody"
    NONE = "none"


class States(Enum):
    WAITING = 1
    HEADERS_DONE = 2
    ERROR = 3
    QUIET = 4


class _ParseError(Exception):
    "Raised inside the parser once an error has been reported, to drop the rest of a read."


idempotent_methods = [b"GET", b"HEAD", b"PUT", b"DELETE", b"OPTIONS", b"TRACE"]
safe_methods = [b"GET", b"HEAD", b"OPTIONS", b"TRACE"]
no_body_status = [b"204", b"304"]
hop_by_hop_hdrs = [
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailers",
    b"transfer-encoding",
    b"upgrade",
    b"proxy-connection",
]


def check_method(method: bytes) -> Optional[str]:
    "Why method isn't a token (RFC 9110 section 9.1), or None if it is."
    if not method:
        return "empty method"
    if not _TOKEN.fullmatch(method):
        return f"method {method[:32]!r} is not a token"
    return None


def check_request_target(target: bytes) -> Optional[str]:
    """
    Why target can't go in a request-line as it is (RFC 9112 section 3.2
    allows no whitespace or control characters), or None if it can.
    """
    if not target:
        return "empty request-target"
    for byte in target:
        if byte <= 0x20 or byte == 0x7F:
            return f"request-target contains {bytes([byte])!r}"
    return None


def check_host(values: List[bytes]) -> Optional[str]:
    """
    Why a request's Host field lines (RFC 9112 section 3.2: exactly one,
    holding a host and optional port) can't be accepted, or None if they can.
    """
    if len(values) > 1:
        return "more than one Host field line"
    value = values[0].strip(b" \t")
    if not value:
        return "empty Host"
    match = _HOST.fullmatch(value)
    if not match or not _valid_ip_literal(match.group(1)):
        return f"Host {value[:64]!r} is not a host and optional port"
    port = match.group(2)
    if port and (len(port) > 5 or int(port) > _MAX_PORT):
        return f"Host {value[:64]!r} has a port above {_MAX_PORT}"
    return None


def _valid_ip_literal(host: bytes) -> bool:
    "Is host, if it is bracketed, a well-formed IPv6 address or IPvFuture?"
    if not host.startswith(b"["):
        return True
    inner = host[1:-1]
    if _IP_FUTURE.fullmatch(inner):
        return True
    if b"%" in inner:  # a zone identifier isn't part of a Host
        return False
    try:
        ipaddress.IPv6Address(inner.decode("ascii"))
    except ValueError:
        return False
    return True


def strip_hop_by_hop(
    hdr_tuples: RawHeaderListType, also: Iterable[bytes] = ()
) -> RawHeaderListType:
    """
    Return the header list without its hop-by-hop fields: the standard ones,
    any the list's own Connection field names (RFC 9110 section 7.6.1), and
    those in `also`. A sender mustn't nominate a field that frames the
    message, and Content-Length is kept whatever Connection says.
    """
    remove = set(hop_by_hop_hdrs)
    remove.update(name.lower() for name in also)
    for name, value in hdr_tuples:
        if name.lower() == b"connection":
            for token in value.split(b","):
                token = token.strip(b" \t").lower()
                if token and token != b"content-length":
                    remove.add(token)
    return [(name, value) for name, value in hdr_tuples if name.lower() not in remove]


def parse_uint(value: bytes, base: int = 10) -> int:
    """
    Strictly parse an unsigned integer (1*DIGIT or 1*HEXDIG, per `base`),
    raising ValueError for anything else (sign, underscore, 0x, whitespace).
    """
    if not (_HEX_DIGITS if base == 16 else _DIGITS).fullmatch(value):
        raise ValueError(value)
    return int(value, base)


def parse_content_length(value: bytes) -> int:
    "Strictly parse a Content-Length value, ignoring surrounding SP / HTAB."
    return parse_uint(value.strip(b" \t"))


def output_content_length(hdr_tuples: RawHeaderListType) -> Optional[int]:
    """
    Return the Content-Length of an outgoing message, or None if it has none.
    Raises OutputSyntaxError if a value isn't strictly valid (as on input), or
    if multiple Content-Length lines disagree.
    """
    lengths = set()
    for name, value in hdr_tuples:
        if name.strip().lower() != b"content-length":
            continue
        if len(value.strip(b" \t")) > _MAX_OUTPUT_CL_DIGITS:
            raise error.OutputSyntaxError(
                f"Content-Length longer than {_MAX_OUTPUT_CL_DIGITS} digits"
            )
        try:
            lengths.add(parse_content_length(value))
        except ValueError:
            raise error.OutputSyntaxError(
                f"Invalid Content-Length {value[:32]!r}"
            ) from None
    if len(lengths) > 1:
        raise error.OutputSyntaxError("Conflicting Content-Length values")
    return lengths.pop() if lengths else None


def header_names(hdr_tuples: RawHeaderListType) -> Set[bytes]:
    """
    Given a list of header tuples, return the set of the header names seen.
    """
    return {n.lower() for n, v in hdr_tuples}


def header_dict(
    hdr_tuples: RawHeaderListType, omit: Optional[List[bytes]] = None
) -> Dict[bytes, List[bytes]]:
    """
    Given a list of header tuples, return a dictionary keyed upon the
    lower-cased header names.

    If omit is defined, each header listed (by lower-cased name) will not be
    returned in the dictionary.
    """
    out: Dict[bytes, List[bytes]] = defaultdict(list)
    for name, val in hdr_tuples:
        name = name.lower()
        if name in (omit or []):
            continue
        out[name].extend([i.strip() for i in val.split(b",")])
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
    return [
        v.strip()
        for v in sum(
            [l.split(b",") for l in [i[1] for i in hdr_tuples if i[0].lower() == name]],
            [],
        )
    ]


class HttpMessageHandler(metaclass=ABCMeta):
    """
    This is a base class for something that has to parse and/or serialise
    HTTP messages, request or response.

    For parsing, it expects you to override input_start, input_body and
    input_end, and call handle_input when you get bytes from the network.

    For serialising, it expects you to override output.
    """

    careful = True  # if False, don't fail on errors, but preserve them.
    max_input_field_section_length = 131072
    max_input_fields = 1000
    default_state: States  # QUIET or WAITING

    def __init__(self) -> None:
        self.input_header_length = 0
        self.input_transfer_length = 0
        self._input_buffer: List[bytes] = []
        self._input_scan_pos = -1  # last LF seen in a partial field section
        self._input_scan_lines = 0  # lines seen in it
        self._input_state = self.default_state
        self._input_delimit: Delimiters = Delimiters.NONE
        self._input_body_left = 0
        self._input_chunk_term: Optional[bytes] = None  # CRLF bytes seen so far
        self._output_state = States.WAITING
        self._output_delimit: Delimiters = Delimiters.NONE
        self._output_body_sent = 0
        self._output_content_length: Optional[int] = None

    def __repr__(self) -> str:
        return f"input {self._input_state} output {self._output_state}"

    # input-related methods

    @abstractmethod
    def input_start(
        self,
        top_line: bytes,
        hdr_tuples: RawHeaderListType,
        conn_tokens: List[bytes],
        transfer_codes: List[bytes],
        content_length: Optional[int],
    ) -> Tuple[bool, bool]:
        """
        Take the top set of headers from the input stream, parse them
        and queue the request to be processed by the application.

        Returns booleans (allows_body, is_final) to indicate whether the message allows a
        body, and whether it's the final message (respectively).

        Can raise ValueError to indicate that there's a problem and parsing
        cannot continue.
        """
        raise NotImplementedError

    @abstractmethod
    def input_body(self, chunk: bytes) -> None:
        """
        Process a body chunk from the wire.

        Chunk is a bytes.
        """
        raise NotImplementedError

    @abstractmethod
    def input_end(self, trailers: RawHeaderListType) -> None:
        """
        Indicate that the response body is complete. Optionally can contain
        trailers.
        """
        raise NotImplementedError

    @abstractmethod
    def input_error(self, err: error.HttpError) -> None:
        """
        Indicate an unrecoverable parsing problem with the input stream.
        """
        raise NotImplementedError

    def input_extra_data(self, inbytes: bytes) -> None:
        "Indicate that data was received after the message was supposed to end."
        self.input_error(error.ExtraDataError(inbytes.decode("utf-8", "replace")))

    def _fail(self, err: error.HttpError) -> None:
        """
        Report a parse error. In careful mode that ends the input; otherwise
        the caller carries on as best it can.
        """
        if self.careful:
            self._abort(err)
        self.input_error(err)

    def _abort(self, err: error.HttpError) -> NoReturn:
        "Report a fatal parse error and drop the rest of the input."
        self._input_state = (
            States.ERROR
        )  # first: a raising listener mustn't leave us WAITING
        self.input_error(err)
        raise _ParseError

    def handle_input(self, inbytes: bytes) -> None:
        """
        Given a bytes representing a chunk of input, figure out what state
        we're in and handle it, making the appropriate calls.
        """
        if self._input_buffer:
            self._input_buffer.append(inbytes)
            inbytes = b"".join(self._input_buffer)
            self._input_buffer = []
        else:
            self._input_scan_pos = -1
            self._input_scan_lines = 0
        try:
            # Each message's leftover is sliced off, so a single read holding
            # a great many messages costs O(messages * size) in copies. TCP
            # reads are capped at 16 KB; only callers passing huge buffers
            # (the old recursion limit used to bound them) would notice.
            rest: Optional[bytes] = inbytes
            while rest is not None:  # the state changed; keep going
                rest = self._handle_input_step(rest)
        except _ParseError:
            pass  # already reported; the rest of the read is dropped

    def _handle_input_step(self, inbytes: bytes) -> Optional[bytes]:
        """
        Handle input according to the current state. Returns None if more
        input is needed, or what's left (possibly nothing) once the state has
        changed and the rest of the read should be looked at again.
        """
        if self._input_state == States.WAITING:  # waiting for headers or trailers
            headers, rest = self._split_headers(inbytes)
            if headers is not None:  # found one
                self._parse_headers(headers)
                return rest
            # partial headers; store it and wait for more
            if len(inbytes) > self.max_input_field_section_length:
                self._abort(error.FieldSectionTooLargeError())
            if inbytes:
                self._input_buffer.append(inbytes)
        elif self._input_state == States.QUIET:  # shouldn't be getting any data now.
            if inbytes.strip():
                self.input_extra_data(inbytes)
        elif (
            self._input_state == States.HEADERS_DONE
        ):  # we found a complete header/trailer set
            try:
                body_handler = getattr(self, f"_handle_{self._input_delimit.value}")
            except AttributeError:
                raise RuntimeError(f"Unknown input delimiter {self._input_delimit}")
            return body_handler(inbytes)  # type: ignore[no-any-return]
        elif self._input_state == States.ERROR:  # something bad happened.
            pass  # I'm silently ignoring input that I don't understand.
        else:
            raise RuntimeError(f"Unknown state {self._input_state}")
        return None

    # Body handlers return what's left once the message ends, so that the
    # caller can look at it in the new state, or None if they need more.

    def _handle_nobody(self, inbytes: bytes) -> Optional[bytes]:
        "Handle input that shouldn't have a body."
        self._input_state = self.default_state
        self.input_end([])
        return inbytes

    def _handle_close(self, inbytes: bytes) -> None:
        "Handle input where the body is delimited by the connection closing; never ends here."
        self.input_transfer_length += len(inbytes)
        self.input_body(inbytes)

    def _handle_chunked(self, inbytes: bytes) -> Optional[bytes]:
        "Handle input where the body is delimited by chunked encoding."
        while inbytes:
            if self._input_chunk_term is not None:
                inbytes = self._handle_chunk_term(inbytes)
            elif self._input_body_left < 0:  # new chunk
                inbytes = self._handle_chunk_new(inbytes)
            elif self._input_body_left > 0:
                # we're in the middle of reading a chunk
                inbytes = self._handle_chunk_body(inbytes)
            elif self._input_body_left == 0:  # body is done
                return self._handle_chunk_done(inbytes)
        return None

    def _handle_chunk_new(self, inbytes: bytes) -> bytes:
        "Handle the start of a new body chunk."
        try:
            chunk_size, rest = inbytes.split(b"\r\n", 1)
        except ValueError:
            # don't have the whole chunk_size yet... wait a bit
            if len(inbytes) > 512:
                # OK, this is absurd...
                self._fail(error.ChunkError(inbytes.decode("utf-8", "replace")))
            else:
                self._input_buffer.append(inbytes)
            return b""
        if b";" in chunk_size:  # ignore chunk extensions
            chunk_size = chunk_size.split(b";", 1)[0].rstrip(b" \t")  # BWS
        try:
            self._input_body_left = parse_uint(chunk_size, 16)
        except ValueError:
            self._fail(error.ChunkError(chunk_size.decode("utf-8", "replace")))
            return b""
        self.input_transfer_length += len(inbytes) - len(rest)
        return rest

    def _handle_chunk_body(self, inbytes: bytes) -> bytes:
        "Handle a continuing body chunk."
        this_chunk = min(len(inbytes), self._input_body_left)
        # data is delivered before its terminator is checked
        self.input_body(inbytes[:this_chunk])
        self.input_transfer_length += this_chunk
        self._input_body_left -= this_chunk
        if self._input_body_left == 0:
            self._input_chunk_term = b""  # now expecting the chunk's CRLF
        return inbytes[this_chunk:]

    def _handle_chunk_term(self, inbytes: bytes) -> bytes:
        "Handle the CRLF after chunk data, which may be split across inputs."
        seen = self._input_chunk_term or b""
        needed = len(LINESEP) - len(seen)
        terminator = seen + inbytes[:needed]
        self.input_transfer_length += min(len(inbytes), needed)
        if len(terminator) < len(LINESEP):
            self._input_chunk_term = terminator
            return b""
        self._input_chunk_term = None
        if not self._check_chunk_terminator(terminator):
            return b""
        self._input_body_left = -1
        return inbytes[needed:]

    def _check_chunk_terminator(self, terminator: bytes) -> bool:
        "Flag a malformed chunk terminator, optionally continuing in permissive mode."
        if terminator == LINESEP:
            return True
        detail = terminator.decode("utf-8", "replace")
        self._fail(error.ChunkTerminatorError(detail))
        return True

    def _handle_chunk_done(self, inbytes: bytes) -> Optional[bytes]:
        "Handle a finished body chunk."
        if inbytes[:2] == b"\r\n":  # no trailer
            self._input_state = self.default_state
            self.input_end([])
            return inbytes[2:]  # 2 consumes the CRLF
        trailer_block, rest = self._split_headers(inbytes)  # trailers
        if trailer_block is not None:
            self._input_state = self.default_state
            trailers = self._parse_fields(self._split_lines(trailer_block))[0]
            self.input_end(trailers)
            return rest
        # don't have full trailers yet
        if len(inbytes) > self.max_input_field_section_length:
            self._abort(error.FieldSectionTooLargeError())
        self._input_buffer.append(inbytes)
        return None

    def _handle_counted(self, inbytes: bytes) -> Optional[bytes]:
        "Handle input where the body is delimited by the Content-Length."
        body_left = self._input_body_left
        if body_left <= len(inbytes):  # got it all (and more?)
            self.input_transfer_length += body_left
            self.input_body(inbytes[:body_left])
            self._input_state = self.default_state
            self.input_end([])
            return inbytes[body_left:]
        # got some of it
        self.input_body(inbytes)
        self.input_transfer_length += len(inbytes)
        self._input_body_left -= len(inbytes)
        return None

    def _parse_fields(
        self, header_lines: List[bytes], gather_conn_info: bool = False
    ) -> Tuple[RawHeaderListType, List[bytes], List[bytes], Optional[int]]:
        """
        Given a list of raw header lines (without the top line,
        and without the trailing CRLFCRLF), return its header tuples.
        """

        hdr_tuples: RawHeaderListType = []
        conn_tokens: List[bytes] = []
        transfer_codes: List[bytes] = []
        content_length: Optional[int] = None

        for line in header_lines:
            if line[:1] in [b" ", b"\t"]:  # Fold LWS
                if hdr_tuples:
                    self._fail(error.ObsoleteFoldError(line.decode("utf-8", "replace")))
                    hdr_tuples[-1] = (
                        hdr_tuples[-1][0],
                        b"%s %s" % (hdr_tuples[-1][1], line.lstrip()),
                    )
                    continue
                # top header starts with whitespace
                self._fail(error.TopLineSpaceError(line.decode("utf-8", "replace")))
            try:
                fn, fv = line.split(b":", 1)
            except ValueError:
                self._fail(error.HeaderNoColonError(line.decode("utf-8", "replace")))
                continue
            if not fn:
                self._fail(error.HeaderNameError("empty field-name"))
                continue
            if fn[-1:] in [b" ", b"\t"]:
                self._fail(error.HeaderSpaceError(fn.decode("utf-8", "replace")))
            if b"\0" in fv:
                self._fail(error.HeaderValueError("NUL in field-value"))
            hdr_tuples.append((fn, fv))

            if not gather_conn_info:
                continue

            f_name = fn.strip().lower()
            f_val = fv.strip()

            # parse connection-related headers
            if f_name == b"connection":
                conn_tokens += [v.strip().lower() for v in f_val.split(b",")]
            elif f_name == b"transfer-encoding":
                transfer_codes += [v.strip().lower() for v in f_val.split(b",")]
            elif f_name == b"content-length":
                if content_length is not None:
                    try:
                        if parse_content_length(fv) == content_length:
                            # we have a duplicate, non-conflicting c-l.
                            continue
                    except ValueError:
                        pass
                    self._fail(error.DuplicateCLError())
                try:
                    content_length = parse_content_length(fv)
                except ValueError:
                    self._fail(error.MalformedCLError(f_val.decode("utf-8", "replace")))

        return hdr_tuples, conn_tokens, transfer_codes, content_length

    def _split_lines(self, block: bytes) -> List[bytes]:
        """
        Split a field section into lines on CRLF or LF only. A bare CR is an
        error, or is replaced with SP if not careful.
        """
        lines = block.split(b"\n")
        for i, line in enumerate(lines):
            if line[-1:] == b"\r":
                line = line[:-1]
            if b"\r" in line:
                self._fail(error.BareCRError(line.decode("utf-8", "replace")))
                line = line.replace(b"\r", b" ")
            lines[i] = line
        return lines

    def _split_headers(self, inbytes: bytes) -> Tuple[Optional[bytes], bytes]:
        """
        Given a bytes, split out and return (headers, rest),
        consuming the whitespace between them.

        If there is not a complete header block, return None for headers;
        the next call (with the same bytes plus more) resumes scanning at
        the last line ending seen, rather than from the start.
        """

        start = self._input_scan_pos
        last_lf = start
        pos = max(start, 0)
        size = len(inbytes)
        while True:
            pos = inbytes.find(b"\n", pos)
            if pos == -1:
                break
            back = 0
            if pos > 0 and inbytes[pos - 1] == RETURN:
                back += 1
            elif pos != start:  # the LF we resumed at was reported last time
                self._fail(error.HeaderLineEndingError())
            if pos != start:
                self._input_scan_lines += 1
            last_lf = pos
            pos += 1
            if pos < size:
                if inbytes[pos] == RETURN:
                    pos += 1
                    back += 1
                if pos < size and inbytes[pos] == NEWLINE:
                    self._input_scan_pos = -1
                    self._input_scan_lines = 0
                    return inbytes[: pos - back - 1], inbytes[pos + 1 :]
        if self._input_scan_lines > self.max_input_fields + 1:
            self._abort(error.TooManyFieldsError())
        self._input_scan_pos = last_lf
        return None, inbytes

    def _parse_headers(self, inbytes: bytes) -> None:
        """
        Given a bytes that we knows starts with a header block,
        parse the headers. Calls self.input_start to kick off processing.

        Raises _ParseError if a fatal problem is found.
        """
        self.input_header_length = len(inbytes)
        if self.input_header_length > self.max_input_field_section_length:
            self._abort(error.FieldSectionTooLargeError())
        header_lines = self._split_lines(inbytes)
        if len(header_lines) > self.max_input_fields + 1:
            self._abort(error.TooManyFieldsError())

        # chop off the top line
        while True:
            try:
                top_line = header_lines.pop(0)
                if top_line.strip() != b"":
                    break
            except IndexError:  # empty
                return

        (
            hdr_tuples,
            conn_tokens,
            transfer_codes,
            content_length,
        ) = self._parse_fields(header_lines, True)

        # ignore content-length if transfer-encoding is present
        if transfer_codes != [] and content_length is not None:
            if self.careful:
                self._abort(error.TransferEncodingCLError())
            content_length = None

        try:
            allows_body, is_final = self.input_start(
                top_line, hdr_tuples, conn_tokens, transfer_codes, content_length
            )
        except ValueError:  # the subclass found a fatal problem and reported it
            self._input_state = States.ERROR
            raise _ParseError from None

        if not is_final:
            self._input_state = States.WAITING
        else:
            self._input_state = States.HEADERS_DONE
        if not allows_body:
            self._input_delimit = Delimiters.NOBODY
        elif transfer_codes:
            if transfer_codes[-1] == b"chunked":
                self._input_delimit = Delimiters.CHUNKED
                self._input_body_left = -1  # flag that we don't know
            else:
                self._input_delimit = Delimiters.CLOSE
        elif content_length is not None:
            self._input_delimit = Delimiters.COUNTED
            self._input_body_left = content_length
        else:
            self._input_delimit = Delimiters.CLOSE

    ### output-related methods

    @abstractmethod
    def output(self, data: bytes) -> None:
        """
        Write something to whatever we're talking to. Should be overridden.
        """
        raise NotImplementedError

    @abstractmethod
    def output_done(self) -> None:
        """
        The exchange won't output any more on the channel.
        """
        raise NotImplementedError

    def output_start(
        self,
        top_line: bytes,
        hdr_tuples: RawHeaderListType,
        delimit: Delimiters,
        is_final: bool = True,
    ) -> None:
        """
        Start outputting a HTTP message.
        """
        if self._output_state != States.WAITING:
            raise error.OutputStateError("HTTP output already started")
        self._check_output_line(top_line, "start line")
        self._check_output_headers(hdr_tuples)
        content_length = output_content_length(hdr_tuples)
        self._output_delimit = delimit
        self._output_body_sent = 0
        self._output_content_length = None
        if delimit == Delimiters.COUNTED:
            self._output_content_length = content_length
        out = [top_line]
        out.extend([b"%s: %s" % (k.strip(), v) for k, v in hdr_tuples])
        out.extend([b"", b""])
        self.output(LINESEP.join(out))
        if is_final:
            self._output_state = States.HEADERS_DONE
        else:
            self._output_state = States.WAITING

    def output_body(self, chunk: bytes) -> None:
        """
        Output a part of a HTTP message. Takes bytes.
        """
        if not chunk:
            return
        if self._output_state != States.HEADERS_DONE:
            raise error.OutputStateError("HTTP output body before final headers")
        if self._output_delimit in (Delimiters.NONE, Delimiters.NOBODY):
            return
        if (
            self._output_delimit == Delimiters.COUNTED
            and self._output_content_length is not None
            and self._output_body_sent + len(chunk) > self._output_content_length
        ):
            raise error.OutputError("Too many body bytes sent")
        self._output_body_sent += len(chunk)
        if self._output_delimit == Delimiters.CHUNKED:
            chunk = b"%s\r\n%s\r\n" % (hex(len(chunk))[2:].encode("ascii"), chunk)
        self.output(chunk)

    def output_end(self, trailers: RawHeaderListType) -> bool:
        """
        Finish outputting a HTTP message, including trailers if appropriate.
        Return value indicates whether the connection should be closed.
        """
        if self._output_state != States.HEADERS_DONE:
            raise error.OutputStateError("HTTP output end before final headers")
        if self._output_delimit == Delimiters.NOBODY:
            pass  # didn't have a body at all.
        elif self._output_delimit == Delimiters.CHUNKED:
            self._check_output_headers(trailers, "trailer")
            self.output(
                b"0\r\n%s\r\n"
                % b"".join([b"%s: %s\r\n" % (k.strip(), v) for k, v in trailers])
            )
        elif self._output_delimit == Delimiters.COUNTED:
            if (
                self._output_content_length is not None
                and self._output_body_sent < self._output_content_length
            ):
                self._output_state = States.WAITING
                return True  # incomplete body; the connection can't be reused.
        elif self._output_delimit == Delimiters.CLOSE:
            return True
        elif self._output_delimit is Delimiters.NONE:
            return True  # encountered an error before we found a delimiter
        else:
            raise AssertionError(f"Unknown request delimiter {self._output_delimit}")
        self._output_state = States.WAITING
        self.output_done()
        return False

    @staticmethod
    def _check_output_line(line: bytes, label: str) -> None:
        if RETURN in line or NEWLINE in line:
            raise error.OutputSyntaxError(f"CRLF in HTTP {label}")

    @classmethod
    def _check_output_headers(
        cls, hdr_tuples: RawHeaderListType, label: str = "header"
    ) -> None:
        for name, val in hdr_tuples:
            cls._check_output_header_name(name, label)
            cls._check_output_header_value(val, label)

    @staticmethod
    def _check_output_header_name(name: bytes, label: str) -> None:
        if not name:
            raise error.OutputSyntaxError(f"Empty HTTP {label} field name")
        if any(c < 33 or c > 126 or c in _BAD_OUTPUT_HEADER_NAME for c in name):
            detail = name.decode("utf-8", "replace")
            raise error.OutputSyntaxError(f"Invalid HTTP {label} field name {detail!r}")

    @staticmethod
    def _check_output_header_value(val: bytes, label: str) -> None:
        if any(c in _BAD_OUTPUT_HEADER_VALUE for c in val):
            raise error.OutputSyntaxError(f"CRLF in HTTP {label} field value")
