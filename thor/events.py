#!/usr/bin/env python

"""
Event utilities, including:

* EventEmitter - in the style of Node.JS.
* on - a decorator for making functions and methods listen to events.
"""

__author__ = "Mark Nottingham <mnot@mnot.net>"
__copyright__ = """\
Copyright (c) 2005-2011 Mark Nottingham

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from collections import defaultdict


class EventEmitter(object):
    """
    An event emitter, in the style of Node.JS.
    """

    def __init__(self):
        self.__events = defaultdict(list)
        self.__sink = None

    def __getstate__(self):
        state = self.__dict__
        del state["__events"]
        return state

    def on(self, event, listener):
        """
        Call listener when event is emitted.
        """
        self.__events[event].append(listener)
        self.emit('newListener', event, listener)

    def once(self, event, listener):
        """
        Call listener the first time event is emitted.
        """
        def mycall(*args):
            listener(*args)
            self.removeListener(event, mycall)
        self.on(event, mycall)

    def removeListener(self, event, listener):
        """
        Remove a specific listener from an event.

        If called for a specific listener by a previous listener
        for the same event, that listener will not be fired.
        """
        self.__events.get(event, [listener]).remove(listener)

    def removeListeners(self, *events):
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

    def listeners(self, event):
        """
        Return a list of listeners for an event.
        """
        return self.__events.get(event, [])

    def events(self):
        """
        Return a list of events being listened for.
        """
        return self.__events.keys()

    def emit(self, event, *args):
        """
        Emit the event (with any given args) to
        its listeners.
        """
        events = self.__events.get(event, [])
        if len(events):
            for e in events:
                e(*args)
        else:
            sink_event = getattr(self.__sink, event, None)
            if sink_event:
                sink_event(*args)

    def sink(self, sink):
        """
        If no listeners are found for an event, call
        the method that shares the event's name (if present)
        on the event sink.
        """
        self.__sink = sink

    # TODO: event bubbling


def on(obj, event=None):
    """
    Decorator to call a function when an object emits
    the specified event.
    """
    def wrap(funk):
        obj.on(event or funk.__name__, funk)
        return funk
    return wrap
