Thor - Easy Evented Intermediation
==================================

About Thor
----------

Thor is a Python library for evented IO.

There are many such libraries for Python already available. Thor focuses
on making it easy to build high-performance HTTP intermediaries like
proxies, load balancers, content transformation engines and service
aggregators. Of course, you can use it just as a client or server too.

It aims to be as fast as possible, to implement the protocols correctly,
and to be simple. You can help meet these goals by contributing issues,
patches and tests.

Thor’s EventEmitter API is influenced by^H^H^H copied from NodeJS; if
you’re familiar with Node, it shouldn’t be too hard to use Thor.
However, Thor is nothing like Twisted; this is considered a feature.

Currently, Thor has an event loop as well as TCP, UDP and HTTP APIs
(client and server). New APIs (e.g., DNS) and capabilities should be
arriving soon, along with a framework for intermediation.

Requirements
------------

Thor just needs Python 2.6 or greater; see `http://python.org/`_.
Currently, it will run on most Posix platforms; specifically, those that
offer one of poll, epoll or kqueue.

Installation
------------

If you have setuptools, you can install from the repository:

    easy\_install thor

or using pip:

    pip install thor

Otherwise, download a tarball and install using:

    python setup.py install

Using Thor
----------

The `documentation`_ is a good starting point; see also the docstrings
for the various modules, as well as the tests, to give an idea of how to
use Thor.

For example, a very simple HTTP server looks like this::

    import thor
    def test_handler(exch):
        @thor.events.on(exch)
        def request_start(*args):
            exch.response_start(200, "OK", [('Content-Type', 'text/plain')])
            exch.response_body('Hello, world!')
            exch.response_done([])

    if __name__ == "__main__":
        demo_server = thor.HttpServer('127.0.0.1', 8000)
        demo_server.on('exchange', test_handler)
        thor.run()

Support and Contributions
-------------------------

See `http://github.com/mnot/thor/`_ to give feedback, view and report
`issues`_, and contribute code.

All input is welcome, particularly code contributions via a Github pull
request, and test cases are the cherry on top.

Contributions need to certify that the person who wrote it (or otherwise
has the right to pass it on) is able to do so as Open Source. The rules
are pretty simple: if you can certify the below::

    Developer's Certificate of Origin 1.1

    By making a contribution to this project, I certify that:

    (a) The contribution was created in whole or in part by me and I
        have the right to submit it under the open source license
        indicated in the file; or

    (b) The contribution is based upon previous work that, to the best
        of my knowledge, is covered under an appropriate open source
        license and I have the right under that license to submit that
        work with modifications, whether created in whole or in part
        by me, under the same open source license (unless I am
        permitted to submit under a different license), as indicated
        in the file; or

    (c) The contribution was provided directly to me by some other
        person who certified (a), (b) or (c) and I have not modified
        it.

    (d) I understand and agree that this project and the contribution
        are public and that a record of the contribution (including 
        all personal information I submit with it, including my 
        sign-off) is maintained indefinitely and may be redistributed
        consistent with this project or the open source license(s) 
        involved.

then you just add a line to the pull request saying::

    Signed-off-by: Random J Developer <random@developer.example.org>

using your real name (sorry, no pseudonyms or anonymous contributions.)

Why Thor?
---------

Thor is not only “a hammer-wielding god associated with thunder,
lightning, storms, oak trees, strength, destruction, fertility, healing,
and the protection of mankind”, he’s also my Norwegian Forest Cat.

Thor (the software program) grew out of nbhttp, which itself came
from earlier work on evented Python in `redbot`_ and tarawa.

Thor (the cat) now rules our house with a firm but benevolent paw. He
gets sick if we give him any milk, though.

.. figure:: http://www.mnot.net/lib/thor.jpg
   :align: center
   :alt: Thor, the cat

   Thor, the cat

License
=======

Copyright (c) 2005–2011 Mark Nottingham

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

.. _`http://python.org/`: http://python.org/
.. _documentation: https://github.com/mnot/thor/tree/master/doc
.. _`http://github.com/mnot/thor/`: http://github.com/mnot/thor/
.. _issues: https://github.com/mnot/thor/issues
.. _redbot: http://redbot.org/
