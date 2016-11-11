#!/usr/bin/env python

import errno
import os
import socket
import sys
import tempfile
import time as systime
import unittest

from framework import make_fifo

import thor.loop


class IOStopper(thor.loop.EventSource):
    def __init__(self, testcase, loop):
        thor.loop.EventSource.__init__(self, loop)
        self.testcase = testcase
        self.r_fd, self.w_fd = make_fifo('tmp_fifo')
        self.on('fd_writable', self.write)
        self.register_fd(self.w_fd, 'fd_writable')

    def write(self):
        self.testcase.assertTrue(self._loop.running)
        self._loop.stop()
        os.close(self.r_fd)
        os.close(self.w_fd)
        os.unlink('tmp_fifo')


class TestLoop(unittest.TestCase):

    def setUp(self):
        self.loop = thor.loop.make()
        self.i = 0

    def increment_counter(self):
        self.i += 1

    def test_start_event(self):
        self.loop.on('start', self.increment_counter)
        self.loop.schedule(1, self.loop.stop)
        self.loop.run()
        self.assertEqual(self.i, 1)

    def test_stop_event(self):
        self.loop.on('stop', self.increment_counter)
        self.loop.schedule(1, self.loop.stop)
        self.loop.run()
        self.assertEqual(self.i, 1)

    def test_run(self):
        def check_running():
            self.assertTrue(self.loop.running)
        self.loop.schedule(0, check_running)
        self.loop.schedule(1, self.loop.stop)
        self.loop.run()

    def test_scheduled_stop(self):
        self.loop.schedule(1, self.loop.stop)
        self.loop.run()
        self.assertFalse(self.loop.running)

    def test_io_stop(self):
        r = IOStopper(self, self.loop)
        self.loop.run()
        self.assertFalse(self.loop.running)

    def test_run_stop_run(self):
        def check_running():
            self.assertTrue(self.loop.running)
        self.loop.schedule(0, check_running)
        self.loop.schedule(1, self.loop.stop)
        self.loop.run()
        self.assertFalse(self.loop.running)
        self.loop.schedule(0, check_running)
        self.loop.schedule(1, self.loop.stop)
        self.loop.run()

    def test_schedule(self):
        run_time = 3 # how long to run for
        def check_time(start_time):
            now = systime.time()
            self.assertTrue(
                now - run_time - start_time <= self.loop.precision,
                "now: %s run_time: %s start_time: %s precision: %s" % (
                    now, run_time, start_time, self.loop.precision
                )
            )
            self.loop.stop()
        self.loop.schedule(run_time, check_time, systime.time())
        self.loop.run()

    def test_schedule_delete(self):
        def not_good():
            assert Exception, "this event should not have happened."
        e = self.loop.schedule(2, not_good)
        self.loop.schedule(1, e.delete)
        self.loop.schedule(3, self.loop.stop)
        self.loop.run()

    def test_time(self):
        run_time = 2
        def check_time():
            self.assertTrue(
                abs(systime.time() - self.loop.time()) <= self.loop.precision
            )
            self.loop.stop()
        self.loop.schedule(run_time, check_time)
        self.loop.run()


class TestEventSource(unittest.TestCase):

    def setUp(self):
        self.loop = thor.loop.make()
        self.es = thor.loop.EventSource(self.loop)
        self.events_seen = []
        self.r_fd, self.w_fd = make_fifo('tmp_fifo')

    def tearDown(self):
        os.close(self.r_fd)
        os.close(self.w_fd)
        os.unlink('tmp_fifo')

    def test_EventSource_register(self):
        self.es.register_fd(self.r_fd)
        self.assertTrue(self.r_fd in list(self.loop._fd_targets))

    def test_EventSource_unregister(self):
        self.es.register_fd(self.r_fd)
        self.assertTrue(self.r_fd in list(self.loop._fd_targets))
        self.es.unregister_fd()
        self.assertFalse(self.r_fd in list(self.loop._fd_targets))

    def test_EventSource_event_del(self):
        self.es.register_fd(self.r_fd, 'fd_readable')
        self.es.on('fd_readable', self.readable_check)
        self.es.event_del('fd_readable')
        os.write(self.w_fd, b'foo')
        self.loop._run_fd_events()
        self.assertFalse('fd_readable' in self.events_seen)

    def test_EventSource_readable(self):
        self.es.register_fd(self.r_fd, 'fd_readable')
        self.es.on('fd_readable', self.readable_check)
        os.write(self.w_fd, b"foo")
        self.loop._run_fd_events()
        self.assertTrue('fd_readable' in self.events_seen)

    def test_EventSource_not_readable(self):
        self.es.register_fd(self.r_fd, 'fd_readable')
        self.es.on('fd_readable', self.readable_check)
        self.loop._run_fd_events()
        self.assertFalse('fd_readable' in self.events_seen)

    def readable_check(self, check=b"foo"):
        data = os.read(self.r_fd, 5)
        self.assertEqual(data, check)
        self.events_seen.append('fd_readable')

#    def test_EventSource_close(self):
#        self.es.register_fd(self.fd, 'close')
#        self.es.on('close', self.close_check)
#        self.fd.close()
#        self.loop._run_fd_events()
#        self.assertTrue('close' in self.events_seen)
#
#    def close_check(self):
#        self.events_seen.append('close')

if __name__ == '__main__':
    unittest.main()

