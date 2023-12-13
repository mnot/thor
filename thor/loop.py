#!/usr/bin/env python

"""
Asynchronous event loops

This is a generic library for building asynchronous event loops, using
Python's built-in poll / epoll / kqueue support.
"""

import cProfile
import select
import time as systime
from typing import (
    Callable,
    List,
    Dict,
    Optional,
    Set,
    Iterable,
    Tuple,
    Any,
)

from thor.events import EventEmitter


__all__ = ["run", "stop", "schedule", "time"]


class EventSource(EventEmitter):
    """
    Base class for objects that the loop will direct interesting
    events to.

    An instance should map to one thing with an interesting file
    descriptor, registered with register_fd.
    """

    def __init__(self, loop: Optional["LoopBase"] = None) -> None:
        EventEmitter.__init__(self)
        self._loop = loop or _loop
        self._interesting_events: Set[str] = set()
        self._fd: int = -1

    def register_fd(self, fd: int, event: Optional[str] = None) -> None:
        """
        Register myself with the loop using file descriptor fd.
        If event is specified, start emitting it.
        """
        self._fd = fd
        self._loop.register_fd(self._fd, [], self)
        if event:
            self.event_add(event)

    def unregister_fd(self) -> None:
        "Unregister myself from the loop."
        if self._fd >= 0:
            self._loop.unregister_fd(self._fd)
            self._fd = -1

    def event_add(self, event: str) -> None:
        "Start emitting the given event."
        if event not in self._interesting_events:
            self._interesting_events.add(event)
            self._loop.event_add(self._fd, event)

    def event_del(self, event: str) -> None:
        "Stop emitting the given event."
        if event in self._interesting_events:
            self._interesting_events.remove(event)
            self._loop.event_del(self._fd, event)

    def interesting_events(self) -> Set[str]:
        return self._interesting_events


class LoopBase(EventEmitter):
    """
    Base class for async loops.
    """

    _event_types: Dict[int, str] = {}  # map of event types to names; override.

    def __init__(self, precision: Optional[float] = None) -> None:
        EventEmitter.__init__(self)
        self.precision = precision or 0.1  # of running scheduled queue (secs)
        self.running = False  # whether or not the loop is running (read-only)
        self.debug = False
        self.__sched_events: List[Tuple[float, Callable]] = []
        self._fd_targets: Dict[int, EventSource] = {}
        self.__last_event_check: float = 0.0
        self._eventlookup = {v: k for (k, v) in self._event_types.items()}
        self.__event_cache: Dict[int, Set[str]] = {}

    def __repr__(self) -> str:
        name = self.__class__.__name__
        is_running = "running" if self.running else "not-running"
        events = len(self.__sched_events)
        targets = len(self._fd_targets)
        return f"<{name} - {is_running}, {events} sched_events, {targets} fd_targets>"

    def run(self) -> None:
        "Start the loop."
        self.running = True
        self.emit("start")
        while self.running:
            if self.debug:
                pr = cProfile.Profile()
                fd_start = systime.monotonic()
                pr.enable()
                self._run_fd_events()
                pr.disable()
                delay = systime.monotonic() - fd_start
                if delay > self.precision * 2:
                    self.debug_out(f"long fd delay ({delay:.2f})", pr)
            else:
                self._run_fd_events()
            # find scheduled events
            if systime.monotonic() - self.__last_event_check >= self.precision:
                self._run_scheduled_events()

    def debug_out(self, message: str, profile: Optional[cProfile.Profile]) -> None:
        "Output a debug message and profile. Should be overridden."

    def _run_fd_events(self) -> None:
        "Run loop-specific FD events."
        raise NotImplementedError

    def _run_scheduled_events(self) -> None:
        "Run scheduled events."
        if self.debug:
            if len(self.__sched_events) > 500:
                self.debug_out(f"{len(self.__sched_events)} events scheduled", None)
        self.__last_event_check = systime.monotonic()
        for event in self.__sched_events:
            when, what = event
            if self.running and when <= self.__last_event_check:
                try:
                    self.__sched_events.remove(event)
                except ValueError:
                    # a previous event may have removed this one.
                    continue
                if self.debug:
                    pr = cProfile.Profile()
                    ev_start = systime.monotonic()
                    pr.enable()
                    what()
                    pr.disable()
                    delay = systime.monotonic() - ev_start
                    if delay > self.precision * 2:
                        self.debug_out(
                            f"long scheduled event delay ({delay:.2f}): {what.__name__}",
                            pr,
                        )
                else:
                    what()
            else:
                break

    def stop(self) -> None:
        "Stop the loop and unregister all fds."
        self.__sched_events = []
        self.running = False
        for fd in list(self._fd_targets):
            self.unregister_fd(fd)
        self.emit("stop")

    def register_fd(self, fd: int, events: List[str], target: EventSource) -> None:
        "emit events on target when they occur on fd."
        raise NotImplementedError

    def unregister_fd(self, fd: int) -> None:
        "Stop emitting events from fd."
        raise NotImplementedError

    def fd_count(self) -> int:
        "Return how many FDs are currently monitored by the loop."
        return len(self._fd_targets)

    def event_add(self, fd: int, event: str) -> None:
        "Start emitting event for fd."
        raise NotImplementedError

    def event_del(self, fd: int, event: str) -> None:
        "Stop emitting event for fd"
        raise NotImplementedError

    def _fd_event(self, event: str, fd: int) -> None:
        "An event has occured on an fd."
        if fd in self._fd_targets:
            self._fd_targets[fd].emit(event)

    def time(self) -> float:
        "Return the current time (deprecated)."
        return systime.time()

    def schedule(
        self, delta: float, callback: Callable, *args: Any
    ) -> "ScheduledEvent":
        """
        Schedule callable callback to be run in delta seconds with *args.

        Returns an object which can be used to later remove the event, by
        calling its delete() method.
        """

        def cb() -> None:
            callback(*args)

        cb.__name__ = callback.__name__
        new_event = (systime.monotonic() + delta, cb)
        events = self.__sched_events
        self._insort(events, new_event)
        if delta > self.precision:
            self._run_scheduled_events()
        return ScheduledEvent(self, new_event)

    def schedule_del(self, event: Tuple[float, Callable]) -> None:
        try:
            self.__sched_events.remove(event)
        except ValueError:  # already gone
            pass

    @staticmethod
    def _insort(li: List, thing: Any, lo: int = 0, hi: Optional[int] = None) -> None:
        if lo < 0:
            raise ValueError("lo must be non-negative")
        if hi is None:
            hi = len(li)
        while lo < hi:
            mid = (lo + hi) // 2
            if thing[0] < li[mid][0]:
                hi = mid
            else:
                lo = mid + 1
        li.insert(lo, thing)

    def _eventmask(self, events: Iterable[str]) -> int:
        "Calculate the mask for a list of events."
        eventmask = 0
        for event in events:
            eventmask |= self._eventlookup.get(event, 0)
        return eventmask

    def _filter2events(self, evfilter: int) -> Set[str]:
        "Calculate the events implied by a given filter."
        if evfilter not in self.__event_cache:
            events = set()
            for et, ev in self._event_types.items():
                if et & evfilter:
                    events.add(ev)
            self.__event_cache[evfilter] = events
        return self.__event_cache[evfilter]


class ScheduledEvent:
    """
    Holds a scheduled event.
    """

    def __init__(self, loop: LoopBase, event: Tuple[float, Callable]) -> None:
        self._loop = loop
        self._event = event
        self._deleted = False

    def delete(self) -> None:
        if not self._deleted:
            self._loop.schedule_del(self._event)
            self._deleted = True


class PollLoop(LoopBase):
    """
    A poll()-based async loop.
    """

    def __init__(self, *args: Any) -> None:
        # pylint: disable=E1101
        self._event_types = {
            select.POLLIN: "fd_readable",  # type: ignore[attr-defined]
            select.POLLOUT: "fd_writable",  # type: ignore[attr-defined]
            select.POLLERR: "fd_error",  # type: ignore[attr-defined]
            select.POLLHUP: "fd_close",  # type: ignore[attr-defined]
        }

        LoopBase.__init__(self, *args)
        self._poll = select.poll()
        # pylint: enable=E1101

    def register_fd(self, fd: int, events: List[str], target: EventSource) -> None:
        self._fd_targets[fd] = target
        self._poll.register(fd, self._eventmask(events))

    def unregister_fd(self, fd: int) -> None:
        self._poll.unregister(fd)
        del self._fd_targets[fd]

    def event_add(self, fd: int, event: str) -> None:
        eventmask = self._eventmask(self._fd_targets[fd].interesting_events())
        self._poll.register(fd, eventmask)

    def event_del(self, fd: int, event: str) -> None:
        eventmask = self._eventmask(self._fd_targets[fd].interesting_events())
        self._poll.register(fd, eventmask)

    def _run_fd_events(self) -> None:
        event_list = self._poll.poll(self.precision)
        for fileno, eventmask in event_list:
            for event in self._filter2events(eventmask):
                self._fd_event(event, fileno)


class EpollLoop(LoopBase):
    """
    An epoll()-based async loop. Currently level-triggered.
    """

    def __init__(self, *args: Any) -> None:
        # pylint: disable=E1101
        self._event_types = {
            select.EPOLLIN: "fd_readable",  # type: ignore[attr-defined]
            select.EPOLLOUT: "fd_writable",  # type: ignore[attr-defined]
            select.EPOLLRDHUP: "fd_close",  # type: ignore[attr-defined]
            select.EPOLLERR: "fd_error",  # type: ignore[attr-defined]
        }
        LoopBase.__init__(self, *args)
        self._epoll = select.epoll()  # type: ignore[attr-defined]
        # pylint: enable=E1101

    def register_fd(self, fd: int, events: List[str], target: EventSource) -> None:
        eventmask = self._eventmask(events)
        if fd in self._fd_targets:
            self._epoll.modify(fd, eventmask)
        else:
            self._fd_targets[fd] = target
            self._epoll.register(fd, eventmask)

    def unregister_fd(self, fd: int) -> None:
        self._epoll.unregister(fd)
        del self._fd_targets[fd]

    def event_add(self, fd: int, event: str) -> None:
        eventmask = self._eventmask(self._fd_targets[fd].interesting_events())
        self._epoll.modify(fd, eventmask)

    def event_del(self, fd: int, event: str) -> None:
        try:
            eventmask = self._eventmask(self._fd_targets[fd].interesting_events())
        except KeyError:
            return  # no longer interested
        self._epoll.modify(fd, eventmask)

    def _run_fd_events(self) -> None:
        event_list = self._epoll.poll(self.precision)
        for fileno, eventmask in event_list:
            for event in self._filter2events(eventmask):
                self._fd_event(event, fileno)


class KqueueLoop(LoopBase):
    """
    A kqueue()-based async loop.
    """

    def __init__(self, *args: Any) -> None:
        # pylint: disable=E1101
        self._event_types = {
            select.KQ_FILTER_READ: "fd_readable",  # type: ignore[attr-defined]
            select.KQ_FILTER_WRITE: "fd_writable",  # type: ignore[attr-defined]
        }
        LoopBase.__init__(self, *args)
        self.max_ev = 50  # maximum number of events to pull from the queue
        self._kq = select.kqueue()  # type: ignore[attr-defined]
        # pylint: enable=E1101

    def register_fd(self, fd: int, events: List[str], target: EventSource) -> None:
        self._fd_targets[fd] = target
        for event in events:
            self.event_add(fd, event)

    def unregister_fd(self, fd: int) -> None:
        try:
            obj = self._fd_targets[fd]
        except KeyError:
            return
        for event in list(obj.interesting_events()):
            obj.event_del(event)
        del self._fd_targets[fd]

    def event_add(self, fd: int, event: str) -> None:
        eventmask = self._eventmask([event])
        if eventmask:
            ev = select.kevent(  # type: ignore[attr-defined]
                fd,
                eventmask,
                select.KQ_EV_ADD | select.KQ_EV_ENABLE,  # type: ignore[attr-defined]
            )
            self._kq.control([ev], 0, 0)

    def event_del(self, fd: int, event: str) -> None:
        eventmask = self._eventmask([event])
        if eventmask:
            ev = select.kevent(fd, eventmask, select.KQ_EV_DELETE)  # type: ignore[attr-defined]
            self._kq.control([ev], 0, 0)

    def _run_fd_events(self) -> None:
        events = self._kq.control([], self.max_ev, self.precision)
        for ev in events:
            event_types = self._filter2events(ev.filter)
            for event_type in event_types:
                self._fd_event(event_type, int(ev.ident))
            if ev.flags & select.KQ_EV_EOF:  # type: ignore[attr-defined]
                self._fd_event("fd_close", int(ev.ident))
            if ev.flags & select.KQ_EV_ERROR:  # type: ignore[attr-defined]
                pass


def make(precision: Optional[float] = None) -> LoopBase:
    """
    Create and return a named loop that is suitable for the current system. If
    _precision_ is given, it indicates how often scheduled events will be run.

    Returned loop instances have all of the methods and instance variables
    that *thor.loop* has.
    """
    loop: LoopBase
    if hasattr(select, "epoll"):
        loop = EpollLoop(precision)
    elif hasattr(select, "kqueue"):
        loop = KqueueLoop(precision)
    elif hasattr(select, "poll"):
        loop = PollLoop(precision)
    else:
        raise ImportError("What is this thing, a Windows box?")
    return loop


_loop = make()  # by default, just one big loop.
run = _loop.run
stop = _loop.stop
schedule = _loop.schedule
time = _loop.time
