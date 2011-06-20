#!/usr/bin/env python

import errno
import os
import socket
import sys
import tempfile
import time as systime
import unittest

sys.path.insert(0, "../")
import thor.loop


class IOStopper(thor.loop.EventSource):
    def __init__(self, testcase, loop):
        thor.loop.EventSource.__init__(self, loop)
        self.testcase = testcase
        self.on('writable', self.write)
        self.register_fd(sys.stdout.fileno(), 'writable')
    
    def write(self):
        self.testcase.assertTrue(self._loop.running)
        self._loop.stop()


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
            self.assertAlmostEqual(
                systime.time() - run_time,
                start_time,
                places=1
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
            self.assertAlmostEqual(
                systime.time(), 
                self.loop.time(),
                places=1
            )
            self.loop.stop()
        self.loop.schedule(run_time, check_time)
        self.loop.run()


class testEventSource(unittest.TestCase):

    def setUp(self):
        self.loop = thor.loop.make()
        self.es = thor.loop.EventSource(self.loop)
        self.fd = sys.stderr
        self.events_seen = []

    def tearDown(self):
        self.fd.close()

    def test_EventSource_register(self):
        self.es.register_fd(self.fd.fileno())
        self.assertTrue(self.fd.fileno() in self.loop._fd_targets.keys())
    
    def test_EventSource_unregister(self):
        self.es.register_fd(self.fd.fileno())
        self.assertTrue(self.fd.fileno() in self.loop._fd_targets.keys())
        self.es.unregister_fd()
        self.assertFalse(self.fd.fileno() in self.loop._fd_targets.keys())
        
    def test_EventSource_event_del(self):
        self.es.register_fd(self.fd.fileno(), 'readable')
        self.es.on('readable', self.readable_check)
        fd2 = open(self.fd.name, 'w')
        fd2.write("foo")
        fd2.close()
        self.es.event_del('readable')
        self.loop._run_fd_events()
        self.assertFalse('readable' in self.events_seen)
        
    def test_EventSource_readable(self):
        self.es.register_fd(self.fd.fileno(), 'readable')
        self.es.on('readable', self.readable_check)
        fd2 = open(self.fd.name, 'w')
        fd2.write("foo")
        fd2.close()
        self.loop._run_fd_events()
        self.assertTrue('readable' in self.events_seen)

    def test_EventSource_not_readable(self):
        self.es.register_fd(self.fd.fileno(), 'readable')
        self.es.on('readable', self.readable_check)
        self.loop._run_fd_events()
        self.assertFalse('readable' in self.events_seen)

    def readable_check(self):
        data = self.fd.read()
        self.assertEquals(data, "foo")
        self.events_seen.append('readable')

#    def test_EventSource_close(self):
#        self.es.register_fd(self.fd.fileno(), 'close')
#        self.es.on('close', self.close_check)
#        self.fd.close()
#        self.loop._run_fd_events()
#        self.assertTrue('close' in self.events_seen)

    def close_check(self):
        self.events_seen.append('close')

        
        
if __name__ == '__main__':
    unittest.main()

