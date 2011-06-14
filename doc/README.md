
# Thor - Simple Event-Driven IO for Python

## About Thor

Thor is a Python library for evented IO, with a focus on enabling
high-performance HTTP intermediaries.

Thor's goals are to be as fast as possible, to implement the protocols
correctly, and to be simple. You can help meet these goals by contributing
issues, patches and tests.

Thor's EventEmitter API is influenced by^H^H^H copied from NodeJS; if
you're familiar with Node, it shouldn't be too hard to use Thor. However, Thor
is nothing like Twisted; this is considered a feature.

Currently, Thor has an event loop as well as TCP and HTTP APIs (client and
server). New APIs (e.g., UDP, DNS) and capabilities (e.g., TLS) should be
arriving soon.


## API Reference

* [Events](doc/events.md) - Emitting and listening for events
* [The Loop](doc/loop.md) - The event loop itself
* [TCP](doc/tcp.md) - Network connections
* [HTTP](doc/http.md) - HyperText Transfer Protocol