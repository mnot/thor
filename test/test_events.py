#!/usr/bin/env python

import sys
import unittest

from thor.events import EventEmitter, on

class TestEventEmitter(unittest.TestCase):
    def setUp(self):
        class Thing(EventEmitter):
            def __init__(self):
                EventEmitter.__init__(self)
                self.foo_count = 0
                self.bar_count = 0
                self.rem1_count = 0
                self.rem2_count = 0
                self.on('foo', self.handle_foo)
                self.once('bar', self.handle_bar)
                self.on('baz', self.handle_baz)
                self.on('rem1', self.handle_rem1)
                self.on('rem1', self.handle_rem1a)
                self.on('rem2', self.handle_rem2)
                self.on('rem2', self.handle_rem2a)

            def handle_foo(self):
                self.foo_count += 1

            def handle_bar(self):
                self.bar_count += 1

            def handle_baz(self):
                raise Exception("Baz wasn't removed.")

            def handle_rem1(self):
                self.rem1_count += 1
                self.removeListeners()
                self.emit('foo')

            def handle_rem1a(self):
                self.rem1_count += 1

            def handle_rem2(self):
                self.rem2_count += 1
                self.removeListener('rem2', self.handle_rem2a)

            def handle_rem2a(self):
                self.rem2_count += 1

        self.t = Thing()

    def test_basic(self):
        self.assertEqual(self.t.foo_count, 0)
        self.t.emit('foo')
        self.assertEqual(self.t.foo_count, 1)
        self.t.emit('foo')
        self.assertEqual(self.t.foo_count, 2)

    def test_once(self):
        self.assertEqual(self.t.bar_count, 0)
        self.t.emit('bar')
        self.assertEqual(self.t.bar_count, 1)
        self.t.emit('bar')
        self.assertEqual(self.t.bar_count, 1)

    def test_removeListener(self):
        self.t.removeListener('foo', self.t.handle_foo)
        self.t.emit('foo')
        self.assertEqual(self.t.foo_count, 0)

    def test_removeListeners_named(self):
        self.t.removeListeners('baz')
        self.t.emit('baz')

    def test_removeListeners_named_multiple(self):
        self.t.removeListeners('baz', 'foo')
        self.t.emit('baz')
        self.t.emit('foo')
        self.assertEqual(self.t.foo_count, 0)

    def test_removeListeners_all(self):
        self.t.emit('foo')
        self.t.removeListeners()
        self.t.emit('foo')
        self.assertEqual(self.t.foo_count, 1)
        self.t.emit('baz')

    def test_sink(self):
        class TestSink:
            def __init__(self):
                self.bam_count = 0
            def bam(self):
                self.bam_count += 1
        s = TestSink()
        self.t.sink(s)
        self.assertEqual(s.bam_count, 0)
        self.t.emit('bam')
        self.assertEqual(s.bam_count, 1)
        self.assertEqual(self.t.foo_count, 0)
        self.t.emit('foo')
        self.assertEqual(self.t.foo_count, 1)

    def test_on_named(self):
        self.t.boom_count = 0
        @on(self.t, 'boom')
        def do():
            self.t.boom_count += 1
        self.assertEqual(self.t.boom_count, 0)
        self.t.emit('boom')
        self.assertEqual(self.t.boom_count, 1)

    def test_on_default(self):
        self.t.boom_count = 0
        @on(self.t)
        def boom():
            self.t.boom_count += 1
        self.assertEqual(self.t.boom_count, 0)
        self.t.emit('boom')
        self.assertEqual(self.t.boom_count, 1)


    def test_removeListeners_recursion(self):
        """
        All event listeners are called for a given
        event, even if one of the previous listeners
        calls removeListeners().
        """
        self.assertEqual(self.t.rem1_count, 0)
        self.t.emit('rem1')
        self.assertEqual(self.t.foo_count, 0)
        self.assertEqual(self.t.rem1_count, 2)

    def test_removeListener_recursion(self):
        """
        Removing a later listener specifically for
        a given event causes it not to be run.
        """
        self.assertEqual(self.t.rem2_count, 0)
        self.t.emit('rem2')
        self.assertEqual(self.t.rem2_count, 1)

if __name__ == '__main__':
    unittest.main()
