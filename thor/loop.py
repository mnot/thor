#!/usr/bin/env python

"""
Asynchronous event loops

This is a generic library for building asynchronous event loops, using
Python 2.6+'s built-in poll / epoll / kqueue support.
"""

from __future__ import absolute_import

import select
import sys
import time as systime

from thor.events import EventEmitter


__all__ = ['run', 'stop', 'schedule', 'time', 'running', 'debug']


class EventSource(EventEmitter):
    """
    Base class for objects that the loop will direct interesting
    events to.

    An instance should map to one thing with an interesting file
    descriptor, registered with register_fd.
    """
    def __init__(self, loop=None):
        EventEmitter.__init__(self)
        self._loop = loop or _loop
        self._interesting_events = set()
        self._fd = None

    def register_fd(self, fd, event=None):
        """
        Register myself with the loop using file descriptor fd.
        If event is specified, start emitting it.
        """
        self._fd = fd
        self._loop.register_fd(self._fd, [], self)
        self.event_add(event)

    def unregister_fd(self):
        "Unregister myself from the loop."
        if self._fd:
            self._loop.unregister_fd(self._fd)
            self._fd = None

    def event_add(self, event):
        "Start emitting the given event."
        if event and event not in self._interesting_events:
            self._interesting_events.add(event)
            self._loop.event_add(self._fd, event)

    def event_del(self, event):
        "Stop emitting the given event."
        if event in self._interesting_events:
            self._interesting_events.remove(event)
            self._loop.event_del(self._fd, event)


class LoopBase(EventEmitter):
    """
    Base class for async loops.
    """
    _event_types = {} # map of event types to names; override.

    def __init__(self, precision=None):
        EventEmitter.__init__(self)
        self.precision = precision or .5 # of running scheduled queue (secs)
        self.running = False # whether or not the loop is running (read-only)
        self.__sched_events = []
        self._fd_targets = {}
        self.__now = None
        self._eventlookup = dict([(v, k) for (k, v) in self._event_types.items()])
        self.__event_cache = {}

    def __repr__(self):
        name = self.__class__.__name__
        running = self.running and "running" or "not-running"
        events = len(self.__sched_events)
        targets = len(self._fd_targets)
        return "<%s - %s, %i events, %i fd_targets>" % (name, running, events, targets)

    def run(self):
        "Start the loop."
        self.running = True
        last_event_check = 0
        self.__now = systime.time()
        self.emit('start')
        while self.running:
            if debug:
                fd_start = systime.time()
            self._run_fd_events()
            self.__now = systime.time()
            if debug:
                delay = self.__now - fd_start
                if delay >= self.precision * 1.5:
                    sys.stderr.write("WARNING: long fd delay (%.2f)\n" % delay)
            # find scheduled events
            delay = self.__now - last_event_check
            if delay >= self.precision * 0.90:
                if debug:
                    if last_event_check and (delay >= self.precision * 4):
                        sys.stderr.write("WARNING: long loop delay (%.2f)\n" % delay)
                    if len(self.__sched_events) > 5000:
                        sys.stderr.write("WARNING: %i events scheduled\n" % \
                                         len(self.__sched_events))
                last_event_check = self.__now
                for event in self.__sched_events:
                    when, what = event
                    if self.running and self.__now >= when:
                        try:
                            self.__sched_events.remove(event)
                        except ValueError:
                            # a previous event may have removed this one.
                            continue
                        if debug:
                            ev_start = systime.time()
                        what()
                        if debug:
                            delay = systime.time() - ev_start
                            if delay > self.precision * 2:
                                sys.stderr.write("WARNING: long event delay (%.2f): %s\n" % \
                                                 (delay, repr(what)))
                    else:
                        break

    def _run_fd_events(self):
        "Run loop-specific FD events."
        raise NotImplementedError

    def stop(self):
        "Stop the loop and unregister all fds."
        self.__sched_events = []
        self.__now = None
        self.running = False
        for fd in list(self._fd_targets):
            self.unregister_fd(fd)
        self.emit('stop')

    def register_fd(self, fd, events, target):
        "emit events on target when they occur on fd."
        raise NotImplementedError

    def unregister_fd(self, fd):
        "Stop emitting events from fd."
        raise NotImplementedError

    def fd_count(self):
        "Return how many FDs are currently monitored by the loop."
        return len(self._fd_targets)

    def event_add(self, fd, event):
        "Start emitting event for fd."
        raise NotImplementedError

    def event_del(self, fd, event):
        "Stop emitting event for fd"
        raise NotImplementedError

    def _fd_event(self, event, fd):
        "An event has occured on an fd."
        if fd in self._fd_targets:
            self._fd_targets[fd].emit(event)
        # TODO: automatic unregister on 'close'?

    def time(self):
        "Return the current time (to avoid a system call)."
        return self.__now or systime.time()

    def schedule(self, delta, callback, *args):
        """
        Schedule callable callback to be run in delta seconds with *args.

        Returns an object which can be used to later remove the event, by
        calling its delete() method.
        """
        def cb(): # FIXME: can't compare functions in py3. Suck.
            if callback:
                callback(*args)
        cb.__name__ = callback.__name__
        new_event = (self.time() + delta, cb)
        events = self.__sched_events
        self._insort(events, new_event)
        class event_holder(object):
            def __init__(self):
                self._deleted = False
            def delete(self):
                if not self._deleted:
                    try:
                        events.remove(new_event)
                        self._deleted = True
                    except ValueError: # already gone
                        pass
        return event_holder()

    def _insort(self, a, x, lo=0, hi=None):
        if lo < 0:
            raise ValueError('lo must be non-negative')
        if hi is None:
            hi = len(a)
        while lo < hi:
            mid = (lo+hi)//2
            if x[0] < a[mid][0]:
                hi = mid
            else: lo = mid+1
        a.insert(lo, x)

    def _eventmask(self, events):
        "Calculate the mask for a list of events."
        eventmask = 0
        for event in events:
            eventmask |= self._eventlookup.get(event, 0)
        return eventmask

    def _filter2events(self, evfilter):
        "Calculate the events implied by a given filter."
        if evfilter not in self.__event_cache:
            events = set()
            for et in self._event_types:
                if et & evfilter:
                    events.add(self._event_types[et])
            self.__event_cache[evfilter] = events
        return self.__event_cache[evfilter]


class PollLoop(LoopBase):
    """
    A poll()-based async loop.
    """

    def __init__(self, *args):
        # pylint: disable=E1101
        self._event_types = {
            select.POLLIN: 'fd_readable',
            select.POLLOUT: 'fd_writable',
            select.POLLERR: 'fd_error',
            select.POLLHUP: 'fd_close'}
    #        select.POLLNVAL - TODO

        LoopBase.__init__(self, *args)
        self._poll = select.poll()
        # pylint: enable=E1101

    def register_fd(self, fd, events, target):
        self._fd_targets[fd] = target
        self._poll.register(fd, self._eventmask(events))

    def unregister_fd(self, fd):
        self._poll.unregister(fd)
        del self._fd_targets[fd]

    def event_add(self, fd, event):
        eventmask = self._eventmask(self._fd_targets[fd]._interesting_events)
        self._poll.register(fd, eventmask)

    def event_del(self, fd, event):
        eventmask = self._eventmask(self._fd_targets[fd]._interesting_events)
        self._poll.register(fd, eventmask)

    def _run_fd_events(self):
        event_list = self._poll.poll(self.precision)
        for fileno, eventmask in event_list:
            for event in self._filter2events(eventmask):
                self._fd_event(event, fileno)


class EpollLoop(LoopBase):
    """
    An epoll()-based async loop.
    """

    def __init__(self, *args):
        # pylint: disable=E1101
        self._event_types = {
            select.EPOLLIN: 'fd_readable',
            select.EPOLLOUT: 'fd_writable',
            select.EPOLLHUP: 'fd_close',
            select.EPOLLERR: 'fd_error'
        }
        LoopBase.__init__(self, *args)
        self._epoll = select.epoll()
        # pylint: enable=E1101

    def register_fd(self, fd, events, target):
        eventmask = self._eventmask(events)
        if fd in self._fd_targets:
            self._epoll.modify(fd, eventmask)
        else:
            self._fd_targets[fd] = target
            self._epoll.register(fd, eventmask)

    def unregister_fd(self, fd):
        self._epoll.unregister(fd)
        del self._fd_targets[fd]

    def event_add(self, fd, event):
        eventmask = self._eventmask(self._fd_targets[fd]._interesting_events)
        self._epoll.modify(fd, eventmask)

    def event_del(self, fd, event):
        try:
            eventmask = self._eventmask(self._fd_targets[fd]._interesting_events)
        except KeyError:
            return # no longer interested
        self._epoll.modify(fd, eventmask)

    def _run_fd_events(self):
        event_list = self._epoll.poll(self.precision)
        for fileno, eventmask in event_list:
            for event in self._filter2events(eventmask):
                self._fd_event(event, fileno)


class KqueueLoop(LoopBase):
    """
    A kqueue()-based async loop.
    """
    def __init__(self, *args):
        self._event_types = {
            select.KQ_FILTER_READ: 'fd_readable',
            select.KQ_FILTER_WRITE: 'fd_writable'}
        LoopBase.__init__(self, *args)
        self.max_ev = 50 # maximum number of events to pull from the queue
        self._kq = select.kqueue()

    # TODO: override schedule() to use kqueue event scheduling.

    def register_fd(self, fd, events, target):
        self._fd_targets[fd] = target
        for event in events:
            self.event_add(fd, event)

    def unregister_fd(self, fd):
        try:
            obj = self._fd_targets[fd]
        except KeyError:
            return
        for event in list(obj._interesting_events):
            obj.event_del(event)
        del self._fd_targets[fd]

    def event_add(self, fd, event):
        eventmask = self._eventmask([event])
        if eventmask:
            ev = select.kevent(fd, eventmask, select.KQ_EV_ADD | select.KQ_EV_ENABLE)
            self._kq.control([ev], 0, 0)

    def event_del(self, fd, event):
        eventmask = self._eventmask([event])
        if eventmask:
            ev = select.kevent(fd, eventmask, select.KQ_EV_DELETE)
            self._kq.control([ev], 0, 0)

    def _run_fd_events(self):
        events = self._kq.control([], self.max_ev, self.precision)
        for e in events:
            event_types = self._filter2events(e.filter)
            for event_type in event_types:
                self._fd_event(event_type, int(e.ident))
            if e.flags & select.KQ_EV_EOF:
                self._fd_event('fd_close', int(e.ident))
            if e.flags & select.KQ_EV_ERROR:
                pass
            # TODO: pull errors, etc. out of flags and fflags
            #   If the read direction of the socket has shutdown, then
    		#	the filter also sets EV_EOF in flags, and returns the
    		#	socket error (if any) in fflags.  It is possible for
    		#	EOF to be returned (indicating the connection is gone)
    		#	while there is still data pending in the socket
    		#	buffer.


def make(precision=None):
    """
    Create and return a named loop that is suitable for the current system. If
    _precision_ is given, it indicates how often scheduled events will be run.

    Returned loop instances have all of the methods and instance variables
    that *thor.loop* has.
    """
    if hasattr(select, 'epoll'):
        loop = EpollLoop(precision)
    elif hasattr(select, 'kqueue'):
        loop = KqueueLoop(precision)
    elif hasattr(select, 'poll'):
        loop = PollLoop(precision)
    else:
        # TODO: select()-based loop (I suppose)
        raise ImportError("What is this thing, a Windows box?")
    return loop

_loop = make() # by default, just one big loop.
run = _loop.run
stop = _loop.stop
schedule = _loop.schedule
time = _loop.time
running = _loop.running
debug = False
