#!/usr/bin/env python

import errno
import os
import select
import socket
import sys
import tempfile
import threading
import time as systime
import unittest

from framework import make_fifo

import thor.loop


class IOStopper(thor.loop.EventSource):
    def __init__(self, testcase, loop):
        thor.loop.EventSource.__init__(self, loop)
        self.testcase = testcase
        self.r_fd, self.w_fd = make_fifo(f"tmp_fifo_{testcase.id()}")
        self.on("fd_writable", self.write)
        self.register_fd(self.w_fd, "fd_writable")

    def write(self):
        self.testcase.assertTrue(self.loop.running)
        self.loop.stop()
        os.close(self.r_fd)
        os.close(self.w_fd)
        os.unlink(f"tmp_fifo_{self.testcase.id()}")


class TestLoop(unittest.TestCase):
    def setUp(self):
        self.loop = thor.loop.make()
        self.i = 0

    def increment_counter(self):
        self.i += 1

    def test_start_event(self):
        self.loop.on("start", self.increment_counter)
        self.loop.schedule(1, self.loop.stop)
        self.loop.run()
        self.assertEqual(self.i, 1)

    def test_stop_event(self):
        self.loop.on("stop", self.increment_counter)
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

    def test_make_rejects_nonpositive_precision(self):
        with self.assertRaisesRegex(ValueError, "precision"):
            thor.loop.make(0)
        with self.assertRaisesRegex(ValueError, "precision"):
            thor.loop.make(-1)

    def test_schedule(self):
        run_time = 3  # how long to run for

        def check_time(start_time):
            now = systime.time()
            self.assertTrue(
                now - run_time - start_time <= self.loop.precision,
                "now: %s run_time: %s start_time: %s precision: %s"
                % (now, run_time, start_time, self.loop.precision),
            )
            self.loop.stop()

        self.loop.schedule(run_time, check_time, systime.time())
        self.loop.run()

    def test_schedule_rejects_negative_delta(self):
        with self.assertRaisesRegex(ValueError, "delta"):
            self.loop.schedule(-1, self.increment_counter)

    def test_schedule_delete(self):
        def not_good():
            assert Exception, "this event should not have happened."

        e = self.loop.schedule(2, not_good)
        self.loop.schedule(1, e.delete)
        self.loop.schedule(3, self.loop.stop)
        self.loop.run()

    def test_schedule_does_not_reenter_due_events(self):
        order = []
        self.loop.running = True

        def future_event():
            order.append("future")

        def first_event():
            order.append("first-start")
            self.loop.schedule(1, future_event)
            order.append("first-end")

        def second_event():
            order.append("second")

        self.loop.schedule(0, first_event)
        self.loop.schedule(0, second_event)
        self.loop._run_scheduled_events()
        self.loop.running = False

        self.assertEqual(order, ["first-start", "first-end", "second"])

    def test_run_in_loop_runs_callback_in_loop_thread(self):
        # A callback handed off from another thread must execute in the loop
        # thread, not the caller's.
        seen = {}

        def record(value):
            seen["value"] = value
            seen["thread"] = threading.current_thread()
            self.loop.stop()

        def worker():
            self.loop.run_in_loop(record, 42)

        self.loop.schedule(0, lambda: threading.Thread(target=worker).start())
        self.loop.schedule(4, self.loop.stop)  # safety net
        self.loop.run()

        self.assertEqual(seen.get("value"), 42)
        self.assertIs(seen.get("thread"), threading.main_thread())

    def test_run_in_loop_preserves_order(self):
        order = []
        self.loop.run_in_loop(order.append, 1)
        self.loop.run_in_loop(order.append, 2)
        self.loop.run_in_loop(order.append, 3)
        self.loop._run_async_queue()
        self.assertEqual(order, [1, 2, 3])

    def test_run_in_loop_callback_enqueued_during_drain_waits(self):
        # A callback that enqueues more work should not starve the drain: the
        # newly-queued item runs on the next iteration, not this one.
        order = []

        def reentrant():
            order.append("a")
            self.loop.run_in_loop(order.append, "b")

        self.loop.run_in_loop(reentrant)
        self.loop._run_async_queue()
        self.assertEqual(order, ["a"])  # "b" deferred to next drain
        self.loop._run_async_queue()
        self.assertEqual(order, ["a", "b"])

    def test_stop_clears_async_queue(self):
        # Work queued from another thread around stop() must not survive into a
        # later run(); stop() drops it like it drops scheduled events.
        ran = []
        self.loop.run_in_loop(ran.append, "stale")
        self.loop.stop()
        self.assertEqual(len(self.loop._async_queue), 0)
        self.loop._run_async_queue()
        self.assertEqual(ran, [])

    @unittest.skipUnless(hasattr(select, "epoll"), "epoll only")
    def test_epoll_register_fd_recovers_from_stale_target(self):
        # Simulate fd reuse: a stale _fd_targets entry whose fd the kernel
        # already dropped from the epoll set. register_fd must re-register
        # rather than raise FileNotFoundError.
        r_fd, w_fd = os.pipe()
        self.addCleanup(lambda: os.close(w_fd))
        es = thor.loop.EventSource(self.loop)
        # Plant a stale entry for an fd epoll has never seen.
        self.loop._fd_targets[r_fd] = es
        try:
            self.loop.register_fd(r_fd, ["fd_readable"], es)
            self.assertIn(r_fd, self.loop._fd_targets)
        finally:
            self.loop.unregister_fd(r_fd)
            os.close(r_fd)


class TestEventSource(unittest.TestCase):
    def setUp(self):
        self.loop = thor.loop.make()
        self.es = thor.loop.EventSource(self.loop)
        self.events_seen = []
        self.r_fd, self.w_fd = make_fifo(f"tmp_fifo_{self.id}")

    def tearDown(self):
        os.close(self.r_fd)
        os.close(self.w_fd)
        os.unlink(f"tmp_fifo_{self.id}")

    def make_extra_fifo(self, label):
        r_fd, w_fd = make_fifo(f"tmp_fifo_{self.id}_{label}")
        self.addCleanup(os.unlink, f"tmp_fifo_{self.id}_{label}")
        self.addCleanup(os.close, w_fd)
        self.addCleanup(os.close, r_fd)
        return r_fd, w_fd

    def test_EventSource_register(self):
        self.es.register_fd(self.r_fd)
        self.assertTrue(self.r_fd in list(self.loop._fd_targets))

    def test_EventSource_unregister(self):
        self.es.register_fd(self.r_fd)
        self.assertTrue(self.r_fd in list(self.loop._fd_targets))
        self.es.unregister_fd()
        self.assertFalse(self.r_fd in list(self.loop._fd_targets))

    def test_loop_unregister_fd_is_idempotent(self):
        self.es.register_fd(self.r_fd)
        self.loop.unregister_fd(self.r_fd)
        self.loop.unregister_fd(self.r_fd)
        self.assertFalse(self.r_fd in list(self.loop._fd_targets))

    def test_EventSource_reregister_replaces_old_fd(self):
        r_fd, w_fd = self.make_extra_fifo("reregister_replaces_old_fd")
        self.es.register_fd(self.r_fd, "fd_readable")
        self.es.register_fd(r_fd, "fd_readable")

        self.assertFalse(self.r_fd in list(self.loop._fd_targets))
        self.assertTrue(r_fd in list(self.loop._fd_targets))
        self.es.on("fd_readable", lambda: self.readable_check_fd(r_fd))
        os.write(w_fd, b"foo")
        self.loop._run_fd_events()
        self.assertTrue("fd_readable" in self.events_seen)

    def test_EventSource_reregister_after_unregister_restores_events(self):
        r_fd, w_fd = self.make_extra_fifo("reregister_after_unregister")
        self.es.register_fd(self.r_fd, "fd_readable")
        self.es.unregister_fd()
        self.es.register_fd(r_fd, "fd_readable")

        self.es.on("fd_readable", lambda: self.readable_check_fd(r_fd))
        os.write(w_fd, b"foo")
        self.loop._run_fd_events()
        self.assertTrue("fd_readable" in self.events_seen)

    def test_EventSource_events_can_change_while_detached(self):
        self.es.event_add("fd_readable")
        self.es.event_del("fd_readable")
        self.es.register_fd(self.r_fd)
        os.write(self.w_fd, b"foo")
        self.loop._run_fd_events()
        self.assertFalse("fd_readable" in self.events_seen)

    def test_EventSource_event_del(self):
        self.es.register_fd(self.r_fd, "fd_readable")
        self.es.on("fd_readable", self.readable_check)
        self.es.event_del("fd_readable")
        os.write(self.w_fd, b"foo")
        self.loop._run_fd_events()
        self.assertFalse("fd_readable" in self.events_seen)

    def test_EventSource_readable(self):
        self.es.register_fd(self.r_fd, "fd_readable")
        self.es.on("fd_readable", self.readable_check)
        os.write(self.w_fd, b"foo")
        self.loop._run_fd_events()
        self.assertTrue("fd_readable" in self.events_seen)

    def test_EventSource_not_readable(self):
        self.es.register_fd(self.r_fd, "fd_readable")
        self.es.on("fd_readable", self.readable_check)
        self.loop._run_fd_events()
        self.assertFalse("fd_readable" in self.events_seen)

    def readable_check(self, check=b"foo"):
        data = os.read(self.r_fd, 5)
        self.assertEqual(data, check)
        self.events_seen.append("fd_readable")

    def readable_check_fd(self, fd, check=b"foo"):
        data = os.read(fd, 5)
        self.assertEqual(data, check)
        self.events_seen.append("fd_readable")

    def close_check(self):
        self.events_seen.append("fd_close")


if __name__ == "__main__":
    unittest.main()
