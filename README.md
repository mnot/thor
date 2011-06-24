
# Thor - Easy Evented Intermediation

## About Thor

Thor is a Python library for evented IO.

There are many such libraries for Python already available. Thor focuses on making it easy to build high-performance HTTP intermediaries like proxies, load balancers, content transformation engines and service aggregators. Of course, you can use it just as a client or server too.

It aims to be as fast as possible, to implement the protocols correctly, and to be simple. You can help meet these goals by contributing issues, patches and tests.

Thor's EventEmitter API is influenced by^H^H^H copied from NodeJS; if you're familiar with Node, it shouldn't be too hard to use Thor. However, Thor is nothing like Twisted; this is considered a feature.

Currently, Thor has an event loop as well as TCP and HTTP APIs (client and server). New APIs (e.g., UDP, DNS) and capabilities (e.g., TLS) should be arriving soon.


## Requirements

Thor just needs Python 2.6 or greater; see <http://python.org/>. Currently, it  will run on most Posix platforms; specifically, those that offer one of poll,  epoll or kqueue.


## Installation

If you have setuptools, you can install from the repository:

> easy_install thor

or using pip:

> pip install thor

Otherwise, download a tarball and install using:

> python setup.py install


## Using Thor

The [documentation](thor/tree/master/doc/) is a good starting point; see also the docstrings for the various modules, as well as the tests, to give an idea of how to use Thor. Examples will be forthcoming soon.


## Support and Contributions

See <http://github.com/mnot/thor/> to give feedback, view and report [issues](thor/issues), and  contribute code.

All input is welcome, particularly code contributions via a Github pull request, and test cases are the cherry on top. 


## Why Thor?

Thor is not only "a hammer-wielding god associated with thunder, lightning,  storms, oak trees, strength, destruction, fertility, healing, and the  protection of mankind", he's also my Norwegian Forest Cat.

Thor (the software program) grew out of nbhttp, which itself came from earlier work on evented Python in redbot and tarawa. 

Thor (the cat) now rules our house with a firm but benevolent paw. He gets sick if we give him any milk, though.

![Thor, the cat](http://www.mnot.net/lib/thor.jpg)

# License

Copyright (c) 2005-2011 Mark Nottingham

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
