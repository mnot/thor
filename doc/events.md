# Events

<span id="EventEmitter"/>
## thor.events.EventEmitter ()

An event emitter, in the style of Node.JS.


### thor.events.EventEmitter.on ( _event_, _listener_ )

Add the callable _listenter_ to the list of listeners that will be called when _event_ is emitted.


### thor.events.EventEmitter.once ( _event_, _listener_ )

Call _listener_ exactly once, the next time that _event_ is emitted.


### thor.events.EventEmitter.removeListener ( _event_, _listener_ )

Remove the callable _listener_ from the list of those that will be called when _event_ is emitted.


### thor.events.EventEmitter.removeListeners ( _event_, ... )

Remove all listeners for _event_. Additional _event_s can be passed as following arguments.


### thor.events.EventEmitter.listeners ( _event_ )

Return the list of callables listening for _event_.


### thor.events.EventEmitter.emit ( _event_, _arg_, ... )

Emit _event_ with one or more _arg_s.


### thor.events.EventEmitter.sink ( _sink_ )

Given an object _sink_, call its method (if present) that corresponds to an _events_ name if and only if there are no listeners for that event.


## Decorator thor.events.on ( _EventEmitter_, _event_ )

A decorator to nominate functions as event listeners. Its first argument is
the [EventEmitter](#EventEmitter) to attach to, and the second argument is 
the event to listen for.

For example:

    @on(my_event_emitter, 'blow_up')
    def push_red_button(thing):
        thing.red_button.push()
        
If the _event_ is omitted, the name of the function is used to determine
the event. For example, this is equivalent to the code above:

    @on(my_event_emitter)
    def blow_up(thing):
        thing.red_button.push()
