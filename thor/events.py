#!/usr/bin/env python

"""
Event utilities, including:

* EventEmitter - in the style of Node.JS.
* on - a decorator for making functions and methods listen to events.
"""

import contextvars
from collections import defaultdict
from typing import Optional, Any, Callable, Dict, List

# ContextVar to track if we're already executing within a context wrapper
_in_context_wrapper: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_in_context_wrapper", default=False
)


class EventEmitter:
    """
    An event emitter, in the style of Node.JS.
    """

    def __init__(self) -> None:
        self.__events: Dict[str, List[Callable]] = defaultdict(list)
        self.__sink: object = None

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        try:
            del state["_EventEmitter__events"]
        except KeyError:
            pass
        return state

    def on(self, event: str, listener: Callable) -> None:
        """
        Call listener when event is emitted.
        """
        if isinstance(listener, ContextWrapper):
            wrapped = listener
        else:
            wrapped = ContextWrapper(listener)
        self.__events[event].append(wrapped)
        self.emit("newListener", event, wrapped)

    def once(self, event: str, listener: Callable) -> None:
        """
        Call listener the first time event is emitted.
        """

        def mycall(*args: Any) -> None:
            self.remove_listener(event, mycall)
            listener(*args)

        mycall.__name__ = getattr(listener, "__name__", "listener")
        self.on(event, mycall)

    def remove_listener(self, event: str, listener: Callable) -> None:
        """
        Remove a specific listener from an event.

        If called for a specific listener by a previous listener
        for the same event, that listener will not be fired.
        """
        self.__events.get(event, [listener]).remove(listener)

    def remove_listeners(self, *events: str) -> None:
        """
        Remove all listeners from an event; if no event
        is specified, remove all listeners for all events.

        If called from an event listener, other listeners
        for that event will still be fired.
        """
        if events:
            for event in events:
                self.__events[event] = []
        else:
            self.__events = defaultdict(list)

    def listeners(self, event: str) -> List[Callable]:
        """
        Return a list of listeners for an event.
        """
        return self.__events.get(event, [])

    def events(self) -> List[str]:
        """
        Return a list of events being listened for.
        """
        return list(self.__events)

    def emit(self, event: str, *args: Any) -> None:
        """
        Emit the event (with any given args) to
        its listeners.
        """
        events = self.__events.get(event, [])
        if events:
            for ev in events:
                ev(*args)
        else:
            sink_event = getattr(self.__sink, event, None)
            if sink_event:
                sink_event(*args)

    def sink(self, sink: object) -> None:
        """
        If no listeners are found for an event, call
        the method that shares the event's name (if present)
        on the event sink.
        """
        self.__sink = sink


class ContextWrapper:
    """
    A wrapper for a listener that captures the current context and runs
    the listener within it.
    """

    def __init__(self, listener: Callable) -> None:
        self.listener = listener
        self.context = contextvars.copy_context()
        self.__name__ = getattr(listener, "__name__", "listener")

    def __call__(self, *args: Any, **lwargs: Any) -> Any:
        # If we're already executing within a context wrapper, don't nest
        if _in_context_wrapper.get():
            return self.listener(*args, **lwargs)

        # Mark that we're in a context wrapper and run the listener
        def run_with_flag() -> Any:
            _in_context_wrapper.set(True)
            try:
                return self.listener(*args, **lwargs)
            finally:
                _in_context_wrapper.set(False)

        return self.context.run(run_with_flag)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ContextWrapper):
            return self.listener == other.listener
        return bool(self.listener == other)

    def __hash__(self) -> int:
        return hash(self.listener)


def on(obj: EventEmitter, event: Optional[str] = None) -> Callable:
    """
    Decorator to call a function when an object emits
    the specified event.
    """

    def wrap(funk: Callable) -> Callable:
        name = getattr(funk, "__name__", "listener")
        obj.on(event or name, funk)
        return funk

    return wrap
