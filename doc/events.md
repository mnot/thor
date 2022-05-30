# Events

## thor.events.EventEmitter

An event emitter, in the style of Node.JS.


### _void_ thor.events.EventEmitter.on (  _str_ `event`,  _func_ `listener` )

Add the callable _listenter_ to the list of listeners that will be called when _event_ is emitted.


### _void_ thor.events.EventEmitter.once (  _str_ `event`, _func_ `listener` )

Call _listener_ exactly once, the next time that _event_ is emitted.


### _void_ thor.events.EventEmitter.remove_listener (  _str_ `event`, _func_ `listener` )

Remove the callable _listener_ from the list of those that will be called when _event_ is emitted.


### _void_ thor.events.EventEmitter.remove_listeners (  _str_ `event`+ )

Remove all listeners for _event_. Additional _event_s can be passed as following arguments.


### _list_ thor.events.EventEmitter.listeners ( _str_ `event` )

Return the list of callables listening for _event_.


### _void_ thor.events.EventEmitter.emit (  _str_ `event`,  _arg_* )

Emit _event_ with zero or more _arg_s.


### _void_ thor.events.EventEmitter.sink ( _object_ `sink` )

Given an object _sink_, call its method (if present) that corresponds to an _events_ name if and only if there are no listeners for that event.


## Decorator thor.events.on ( _EventEmitter_ `EventEmitter`,  _str_ `event` )

A decorator to nominate functions as event listeners. Its first argument is
the [EventEmitter](#EventEmitter) to attach to, and the second argument is 
the event to listen for.

For example:

    @on(my_event_emitter, 'blow_up')
    def push_red_button(thing):
        thing.red_button.push()
        
If the `event` is omitted, the name of the function is used to determine
the event. For example, this is equivalent to the code above:

    @on(my_event_emitter)
    def blow_up(thing):
        thing.red_button.push()
