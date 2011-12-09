#!/usr/bin/env python

from distutils.core import setup
import thor

setup(
  name = 'thor',
  version = thor.__version__,
  description = 'Simple Event-Driven IO for Python',
  author = 'Mark Nottingham',
  author_email = 'mnot@mnot.net',
  url = 'http://github.com/mnot/thor/',
  download_url = \
    'http://github.com/mnot/thor/tarball/thor-%s' % thor.__version__,
  packages = ['thor', 'thor.http'],
  provides = ['thor'],
  long_description=open("README.rst").read(),
  classifiers = [
    'Development Status :: 4 - Beta',
    'Intended Audience :: Developers',
    'License :: OSI Approved :: MIT License',
    'Programming Language :: Python',
    'Operating System :: POSIX',
    'Topic :: Internet :: WWW/HTTP',
    'Topic :: Internet :: Proxy Servers',
    'Topic :: Internet :: WWW/HTTP :: HTTP Servers',
    'Topic :: Software Development :: Libraries :: Python Modules',
  ]
)