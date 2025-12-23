import sys
from typing import Callable
from thor.events import on
from thor.http.common import RawHeaderListType
from thor.http.error import HttpError
from thor.loop import stop, run, schedule
from .client import HttpClient


def test_client(
    request_uri: bytes, out: Callable, err: Callable
) -> None:  # pragma: no coverage
    "A simple demonstration of a client."
    cl = HttpClient()
    cl.connect_timeout = 5
    cl.careful = False
    ex = cl.exchange()

    @on(ex)
    def response_start(
        status: bytes, phrase: bytes, headers: RawHeaderListType
    ) -> None:
        "Print the response headers."
        out(b"HTTP/%s %s %s\n" % (ex.res_version, status, phrase))
        out(b"\n".join([b"%s:%s" % header for header in headers]))
        print()
        print()

    @on(ex)
    def error(err_msg: HttpError) -> None:
        if err_msg:
            err(f"\033[1;31m*** ERROR:\033[0;39m {err_msg.desc} ({err_msg.detail})\n")
        if not err_msg.client_recoverable:
            stop()

    ex.on("response_body", out)

    @on(ex)
    def response_done(trailers: RawHeaderListType) -> None:
        schedule(1, stop)

    ex.request_start(b"GET", request_uri, [])
    ex.request_done([])
    run()


if __name__ == "__main__":
    test_client(sys.argv[1].encode("ascii"), sys.stdout.buffer.write, sys.stderr.write)
