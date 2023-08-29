# Thor

[![CI](https://github.com/mnot/thor/actions/workflows/ci.yml/badge.svg)](https://github.com/mnot/thor/actions/workflows/ci.yml)
[![Coverage Status](https://coveralls.io/repos/mnot/thor/badge.svg)](https://coveralls.io/r/mnot/thor)

## About Thor

Thor is yet another [Python 3](https://python.org/) library for evented IO.

There are many such libraries for Python already available. Thor focuses on making it easy to build
high-performance HTTP intermediaries like proxies, load balancers, content transformation engines
and service aggregators. Of course, you can use it just as a client or server too.

It aims to be as fast as possible, to implement the protocols correctly, and to be simple. You can
help meet these goals by contributing issues, patches and tests.

Thor’s EventEmitter API is influenced by^H^H^H copied from NodeJS; if you’re familiar with Node, it
shouldn’t be too hard to use Thor. However, Thor is nothing like Twisted; this is considered a
feature.

Currently, Thor has an event loop as well as TCP, UDP and HTTP APIs (client and server). New APIs
(e.g., DNS) and capabilities should be arriving soon, along with a framework for intermediation.

## Requirements

Thor just requires Python 3.8 or greater.

Currently, it will run on most Posix platforms; specifically, those that offer one of `poll`,
`epoll` or `kqueue`.

## Installation

If you have setuptools, you can install from the repository:

> easy\_install thor

or using pip:

> pip install thor

On some operating systems, that might be `pip3`. Otherwise, download a tarball and install using:

> python setup.py install

## Using Thor

The [documentation](https://github.com/mnot/thor/tree/master/doc) is a good starting point; see
also the docstrings for the various modules, as well as the tests, to give an idea of how to use
Thor.

For example, a very simple HTTP server looks like this:

```python
import thor, thor.http
def test_handler(exch):
    @thor.events.on(exch)
    def request_start(*args):
        exch.response_start(200, "OK", [('Content-Type', 'text/plain')])
        exch.response_body('Hello, world!')
        exch.response_done([])

if __name__ == "__main__":
    demo_server = thor.http.HttpServer('127.0.0.1', 8000)
    demo_server.on('exchange', test_handler)
    thor.run()
```

## Support and Contributions

See [Thor's GitHub](http://github.com/mnot/thor/) to give feedback, view and [report
issues](https://github.com/mnot/thor/issues), and contribute code.

All helpful input is welcome, particularly code contributions via a pull request (with test cases).

## Why Thor?

Thor is not only “a hammer-wielding god associated with thunder, lightning, storms, oak trees,
strength, destruction, fertility, healing, and the protection of mankind”, he’s also my Norwegian
Forest Cat.

Thor (the software program) grew out of nbhttp, which itself came from earlier work on evented
Python in [redbot](http://redbot.org/).

Thor (the cat) now rules our house with a firm but benevolent paw. He gets sick if we give him any
milk, though.

![Thor, the cat](https://www.mnot.net/lib/thor.jpg)
