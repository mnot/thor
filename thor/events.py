#!/usr/bin/env python

"""
Event utilities, including:

* EventEmitter - in the style of Node.JS.
* on - a decorator for making functions and methods listen to events.
"""

from collections import defaultdict
from typing import Any, Callable, Dict, List


class EventEmitter:
    """
    An event emitter, in the style of Node.JS.
    """

    def __init__(self) -> None:
        self.__events = defaultdict(list)  # type: Dict[str, List[Callable]]
        self.__sink = None                 # type: object

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        del state["_EventEmitter__events"]
        return state

    def on(self, event: str, listener: Callable) -> None:
        """
        Call listener when event is emitted.
        """
        self.__events[event].append(listener)
        self.emit('newListener', event, listener)

    def once(self, event: str, listener: Callable) -> None:
        """
        Call listener the first time event is emitted.
        """
        def mycall(*args: Any) -> None:
            listener(*args)
            self.removeListener(event, mycall)
        self.on(event, mycall)

    def removeListener(self, event: str, listener: Callable) -> None:
        """
        Remove a specific listener from an event.

        If called for a specific listener by a previous listener
        for the same event, that listener will not be fired.
        """
        self.__events.get(event, [listener]).remove(listener)

    def removeListeners(self, *events: str) -> None:
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
            for e in events:
                e(*args)
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

    # TODO: event bubbling


def on(obj: EventEmitter, event: str = None) -> Callable:
    """
    Decorator to call a function when an object emits
    the specified event.
    """
    def wrap(funk: Callable) -> Callable:
        obj.on(event or funk.__name__, funk)
        return funk
    return wrap
