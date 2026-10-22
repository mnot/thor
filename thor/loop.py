#!/usr/bin/env python

"""
Asynchronous event loops

This is a generic library for building asynchronous event loops, using
Python's built-in poll / epoll / kqueue support.
"""

import bisect
import collections
import contextvars
import cProfile
import errno
import select
import time as systime
from abc import ABCMeta, abstractmethod
from functools import partial
from typing import (
    Any,
    Deque,
    Dict,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
)

from thor.events import EventEmitter
from thor.types import EventListener, ScheduledEventTuple

__all__ = ["run", "stop", "schedule", "run_in_loop"]


class EventSource(EventEmitter):
    """
    Base class for objects that the loop will direct interesting
    events to.

    An instance should map to one thing with an interesting file
    descriptor, registered with register_fd.
    """

    def __init__(self, loop: Optional["LoopBase"] = None) -> None:
        EventEmitter.__init__(self)
        self.loop = loop or _loop
        self._interesting_events: Set[str] = set()
        self._fd: int = -1

    def register_fd(self, fd: int, event: Optional[str] = None) -> None:
        """
        Register myself with the loop using file descriptor fd.
        If event is specified, start emitting it.
        """
        if self._fd >= 0 and self._fd != fd:
            self.loop.unregister_fd(self._fd)
        self._fd = fd
        self.loop.register_fd(self._fd, [], self)
        if event:
            self._interesting_events.add(event)
        for interesting_event in self._interesting_events:
            self.loop.event_add(self._fd, interesting_event)

    def unregister_fd(self) -> None:
        "Unregister myself from the loop."
        if self._fd >= 0:
            self.loop.unregister_fd(self._fd)
            self._fd = -1

    def event_add(self, event: str) -> None:
        "Start emitting the given event."
        if event not in self._interesting_events:
            self._interesting_events.add(event)
            if self._fd >= 0:
                self.loop.event_add(self._fd, event)

    def event_del(self, event: str) -> None:
        "Stop emitting the given event."
        if event in self._interesting_events:
            self._interesting_events.remove(event)
            if self._fd >= 0:
                self.loop.event_del(self._fd, event)

    def interesting_events(self) -> Set[str]:
        return self._interesting_events


class FdRegistration:
    """
    An EventSource's claim on an fd number. Each registration gets a new
    generation, so events polled for a previous owner of the number can be
    told apart from the current owner's.
    """

    __slots__ = ("fd", "target", "generation")

    def __init__(self, fd: int, target: EventSource, generation: int) -> None:
        self.fd = fd
        self.target = target
        self.generation = generation


class LoopBase(EventEmitter, metaclass=ABCMeta):
    """
    Base class for async loops.
    """

    _event_types: Dict[int, str] = {}  # map of event types to names; override.

    def __init__(self, precision: Optional[float] = None) -> None:
        EventEmitter.__init__(self)
        if precision is not None and precision <= 0:
            raise ValueError("precision must be greater than zero")
        self.precision = precision or 0.1  # of running scheduled queue (secs)
        self.running = False  # whether or not the loop is running (read-only)
        self.debug = False
        self.__profiler: Optional[cProfile.Profile] = None
        self.__sched_events: List[ScheduledEventTuple] = []
        self._async_queue: Deque[Tuple[EventListener, Tuple[Any, ...]]] = (
            collections.deque()
        )
        self._fd_targets: Dict[int, FdRegistration] = {}
        self._fd_generation = 0
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
        if self.debug:
            self.__profiler = cProfile.Profile()
        while self.running:
            self._run_async_queue()
            if self.debug and self.__profiler is not None:
                fd_start = systime.monotonic()
                self.__profiler.enable()
                self._run_fd_events()
                self.__profiler.disable()
                delay = systime.monotonic() - fd_start
                if delay > self.precision * 2:
                    self.debug_out(f"long fd delay ({delay:.2f})", self.__profiler)
            else:
                self._run_fd_events()
            # find scheduled events
            if systime.monotonic() - self.__last_event_check >= self.precision:
                self._run_scheduled_events()

    def debug_out(self, message: str, profile: Optional[cProfile.Profile]) -> None:
        "Output a debug message and profile. Should be overridden."

    def registered_fd_handlers(self) -> List[EventSource]:
        "Returns a list of registered fd EventSources."
        return [reg.target for reg in self._fd_targets.values()]

    @abstractmethod
    def _run_fd_events(self) -> None:
        "Run loop-specific FD events."
        raise NotImplementedError

    def _run_async_queue(self) -> None:
        "Run callbacks queued from other threads by run_in_loop()."
        queue = self._async_queue
        # Snapshot the length so callbacks that enqueue more work run on the
        # next iteration rather than starving fd/scheduled events this one.
        for _ in range(len(queue)):
            try:
                callback, args = queue.popleft()
            except IndexError:  # pragma: no cover - drained concurrently
                break
            callback(*args)

    def _run_scheduled_events(self) -> None:
        "Run scheduled events."
        if self.debug:
            if len(self.__sched_events) > 500:
                self.debug_out(f"{len(self.__sched_events)} events scheduled", None)
        self.__last_event_check = systime.monotonic()
        while self.__sched_events:
            when, what = self.__sched_events[0]
            if self.running and when <= self.__last_event_check:
                self.__sched_events.pop(0)
                if self.debug and self.__profiler is not None:
                    ev_start = systime.monotonic()
                    self.__profiler.enable()
                    what()
                    self.__profiler.disable()
                    delay = systime.monotonic() - ev_start
                    if delay > self.precision * 2:
                        name = getattr(what, "__name__", str(what))
                        self.debug_out(
                            f"long scheduled event delay ({delay:.2f}): {name}",
                            self.__profiler,
                        )
                else:
                    what()
            else:
                break

    def scheduled_events(self) -> List[ScheduledEventTuple]:
        """
        Return a list of (delay, callback) tuples for currently scheduled events.
        Delay is in seconds, measured from call time.
        """
        now = systime.monotonic()
        return [(when - now, what) for when, what in self.__sched_events]

    def stop(self) -> None:
        "Stop the loop and unregister all fds."
        self.__sched_events = []
        self._async_queue.clear()
        self.running = False
        for fd in list(self._fd_targets):
            self.unregister_fd(fd)
        self.emit("stop")

    @abstractmethod
    def register_fd(self, fd: int, events: List[str], target: EventSource) -> None:
        "emit events on target when they occur on fd."
        raise NotImplementedError

    @abstractmethod
    def unregister_fd(self, fd: int) -> None:
        "Stop emitting events from fd."
        raise NotImplementedError

    def fd_count(self) -> int:
        "Return how many FDs are currently monitored by the loop."
        return len(self._fd_targets)

    @abstractmethod
    def event_add(self, fd: int, event: str) -> None:
        "Start emitting event for fd."
        raise NotImplementedError

    @abstractmethod
    def event_del(self, fd: int, event: str) -> None:
        "Stop emitting event for fd"
        raise NotImplementedError

    def _register(self, fd: int, target: EventSource) -> FdRegistration:
        "Record target as the current owner of fd; the same owner keeps its claim."
        current = self._fd_targets.get(fd)
        if current is not None and current.target is target:
            return current
        self._fd_generation += 1
        registration = FdRegistration(fd, target, self._fd_generation)
        self._fd_targets[fd] = registration
        return registration

    def _snapshot_generations(self, fds: Iterable[int]) -> Dict[int, int]:
        """
        Which registration each fd in a polled batch belongs to, taken before
        any of the batch is dispatched. A handler may close an fd and a new
        source take its number (a closed socket's number is reused at once);
        the batch's remaining events for that number were polled for the old
        owner and must not reach the new one, whichever fd's handler did it.
        """
        snapshot = {}
        for fd in fds:
            registration = self._fd_targets.get(fd)
            if registration is not None:
                snapshot[fd] = registration.generation
        return snapshot

    def _emit_fd_events(
        self, fd: int, events: Iterable[str], generation: Optional[int] = None
    ) -> None:
        """
        Emit events for an fd, in order, to the registration they were polled
        for (the given generation, or the current one), stopping if the fd's
        registration changes under us.
        """
        if generation is None:
            registration = self._fd_targets.get(fd)
            if registration is None:
                return
            generation = registration.generation
        for event in events:
            current = self._fd_targets.get(fd)
            if current is None or current.generation != generation:
                return
            current.target.emit(event)

    def _dispatch_fd(self, fd: int, events: Set[str], generation: int) -> None:
        "Emit an fd's events: readable first, then writable, then the rest."
        first = ("fd_readable", "fd_writable")
        ordered = [event for event in first if event in events]
        ordered.extend(event for event in events if event not in first)
        self._emit_fd_events(fd, ordered, generation)

    def schedule(
        self, delta: float, callback: EventListener, *args: Any
    ) -> "ScheduledEvent":
        """
        Schedule callable callback to be run in delta seconds with *args.

        Returns an object which can be used to later remove the event, by
        calling its delete() method.
        """
        if delta < 0:
            raise ValueError("schedule delta must be non-negative")

        ctx = contextvars.copy_context()

        def ctx_callback(*args: Any, **kwargs: Any) -> Any:
            return ctx.run(callback, *args, **kwargs)

        cb = partial(ctx_callback, *args)
        new_event = (systime.monotonic() + delta, cb)
        bisect.insort(
            self.__sched_events, new_event, key=lambda ev: ev[0]  # type: ignore[misc]
        )
        return ScheduledEvent(self, new_event)

    def run_in_loop(self, callback: EventListener, *args: Any) -> None:
        """
        Thread-safe: arrange for callback(*args) to run in the loop thread on
        its next iteration.

        This is the ONLY loop method safe to call from a thread other than the
        one running the loop. Every other method here (schedule, register_fd,
        event_add, ...) mutates loop state without locking and MUST only be
        touched from the loop thread; use this to hand work back to it.

        deque.append is atomic under CPython, so no lock is needed. The work
        runs within one poll precision (self.precision) of being queued.

        Like any loop callback (scheduled or fd), the callback must not raise:
        an unhandled exception propagates out of run() and stops the loop.
        """
        self._async_queue.append((callback, args))

    def schedule_del(self, event: ScheduledEventTuple) -> None:
        try:
            self.__sched_events.remove(event)
        except ValueError:  # already gone
            pass

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

    def __init__(self, loop: LoopBase, event: ScheduledEventTuple) -> None:
        self._loop = loop
        self._event = event
        self._deleted = False

    def delete(self) -> None:
        if not self._deleted:
            self._loop.schedule_del(self._event)
            self._deleted = True


def _fd_gone(why: OSError) -> bool:
    "Did the poller reject an fd because it's closed or was never registered?"
    return why.errno in (errno.EBADF, errno.ENOENT)


class MaskLoop(LoopBase):
    """
    A loop over a poller that takes one event mask per fd, with register /
    modify / unregister calls: poll and epoll. Subclasses supply the poller
    and the event-type mapping.
    """

    def __init__(self, *args: Any) -> None:
        LoopBase.__init__(self, *args)
        self._poller = self._make_poller()

    @abstractmethod
    def _make_poller(self) -> Any:
        raise NotImplementedError

    @abstractmethod
    def _poll(self) -> List[Tuple[int, int]]:
        "Wait up to self.precision for events; return (fd, eventmask) pairs."
        raise NotImplementedError

    def register_fd(self, fd: int, events: List[str], target: EventSource) -> None:
        eventmask = self._eventmask(events)
        if fd in self._fd_targets:
            try:
                self._poller.modify(fd, eventmask)
            except OSError as why:
                if not _fd_gone(why):
                    raise
                # Stale entry: the fd was closed (and so dropped from the
                # poller) and its number reused before we unregistered it.
                self._poller.register(fd, eventmask)
        else:
            self._poller.register(fd, eventmask)
        self._register(fd, target)

    def unregister_fd(self, fd: int) -> None:
        self._fd_targets.pop(fd, None)
        try:
            self._poller.unregister(fd)
        except KeyError:  # poll(): never registered
            return
        except OSError as why:
            if not _fd_gone(why):
                raise

    def event_add(self, fd: int, event: str) -> None:
        self._update_mask(fd)

    def event_del(self, fd: int, event: str) -> None:
        self._update_mask(fd)

    def _update_mask(self, fd: int) -> None:
        "Tell the poller which events the fd's target is interested in now."
        try:
            target = self._fd_targets[fd].target
        except KeyError:
            return
        try:
            self._poller.modify(fd, self._eventmask(target.interesting_events()))
        except OSError as why:
            if not _fd_gone(why):
                raise
            self._fd_targets.pop(fd, None)

    def _run_fd_events(self) -> None:
        try:
            event_list = self._poll()
        except OSError as why:
            if why.errno == errno.EINTR:
                return
            raise
        batch = [(fd, self._filter2events(mask)) for fd, mask in event_list]
        generations = self._snapshot_generations(fd for fd, _ in batch)
        for fd, events in batch:
            if fd in generations:
                self._dispatch_fd(fd, events, generations[fd])


class PollLoop(MaskLoop):
    """
    A poll()-based async loop.
    """

    def __init__(self, *args: Any) -> None:
        # pylint: disable=E1101
        self._event_types = {
            select.POLLIN: "fd_readable",
            select.POLLOUT: "fd_writable",
            select.POLLERR: "fd_error",
            select.POLLHUP: "fd_close",
        }
        # pylint: enable=E1101
        MaskLoop.__init__(self, *args)

    def _make_poller(self) -> Any:
        return select.poll()  # pylint: disable=E1101

    def _poll(self) -> List[Tuple[int, int]]:
        return self._poller.poll(int(self.precision * 1000))  # type: ignore[no-any-return]


class EpollLoop(MaskLoop):
    """
    An epoll()-based async loop. Currently level-triggered.
    """

    def __init__(self, *args: Any) -> None:
        # pylint: disable=E1101
        self._event_types = {
            select.EPOLLIN: "fd_readable",  # type: ignore[attr-defined, unused-ignore]
            select.EPOLLOUT: "fd_writable",  # type: ignore[attr-defined, unused-ignore]
            select.EPOLLRDHUP: "fd_close",  # type: ignore[attr-defined, unused-ignore]
            select.EPOLLHUP: "fd_close",  # type: ignore[attr-defined, unused-ignore]
            select.EPOLLERR: "fd_error",  # type: ignore[attr-defined, unused-ignore]
        }
        # pylint: enable=E1101
        MaskLoop.__init__(self, *args)

    def _make_poller(self) -> Any:
        return select.epoll()  # type: ignore[attr-defined, unused-ignore]  # pylint: disable=E1101

    def _poll(self) -> List[Tuple[int, int]]:
        return self._poller.poll(self.precision)  # type: ignore[no-any-return]


class KqueueLoop(LoopBase):
    """
    A kqueue()-based async loop.
    """

    # pylint: disable=no-member

    def __init__(self, *args: Any) -> None:
        self._event_types = {
            select.KQ_FILTER_READ: "fd_readable",  # type: ignore[attr-defined, unused-ignore]
            select.KQ_FILTER_WRITE: "fd_writable",  # type: ignore[attr-defined, unused-ignore]
        }
        LoopBase.__init__(self, *args)
        self.max_ev = 50  # maximum number of events to pull from the queue
        self._kq = select.kqueue()  # type: ignore[attr-defined, unused-ignore]

    def register_fd(self, fd: int, events: List[str], target: EventSource) -> None:
        self._register(fd, target)
        for event in events:
            self.event_add(fd, event)

    def unregister_fd(self, fd: int) -> None:
        try:
            target = self._fd_targets[fd].target
        except KeyError:
            return
        for event in list(target.interesting_events()):
            target.event_del(event)
        del self._fd_targets[fd]

    def event_add(self, fd: int, event: str) -> None:
        eventmask = self._eventmask([event])
        if eventmask:
            ev = select.kevent(  # type: ignore[attr-defined, unused-ignore]
                fd,
                eventmask,
                select.KQ_EV_ADD | select.KQ_EV_ENABLE,  # type: ignore[attr-defined, unused-ignore]
            )
            try:
                self._kq.control([ev], 0, 0)
            except OSError as why:
                if why.errno in (errno.EBADF, errno.ENOENT):
                    self._fd_targets.pop(fd, None)
                    return
                raise

    def event_del(self, fd: int, event: str) -> None:
        eventmask = self._eventmask([event])
        if eventmask:
            ev = select.kevent(  # type: ignore[attr-defined, unused-ignore]
                fd,
                eventmask,
                select.KQ_EV_DELETE,  # type: ignore[attr-defined, unused-ignore]
            )
            if ev:
                try:
                    self._kq.control([ev], 0, 0)
                except (FileNotFoundError, OSError) as why:
                    if getattr(why, "errno", None) in (errno.EBADF, errno.ENOENT):
                        return
                    raise

    def _run_fd_events(self) -> None:
        try:
            events = self._kq.control([], self.max_ev, self.precision)
        except (OSError, select.error) as why:
            if why.args[0] == errno.EINTR:
                return
            raise
        batch: List[Tuple[int, List[str]]] = []
        for ev in events:
            fileno = int(ev.ident)
            if ev.flags & select.KQ_EV_ERROR:  # type: ignore[attr-defined, unused-ignore]
                batch.append((fileno, ["fd_error"]))
                continue
            eof = bool(ev.flags & select.KQ_EV_EOF)  # type: ignore[attr-defined, unused-ignore]
            ordered: List[str] = ["fd_error"] if eof and ev.fflags else []
            ordered.extend(self._filter2events(ev.filter))
            if eof:
                ordered.append("fd_close")
            batch.append((fileno, ordered))
        generations = self._snapshot_generations(fd for fd, _ in batch)
        for fd, ordered in batch:
            if fd in generations:
                self._emit_fd_events(fd, ordered, generations[fd])


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
run_in_loop = _loop.run_in_loop
