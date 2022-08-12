## Event Loops

Thor creates a "default" event loop in the *thor.loop* namespace which can be
run using *thor.loop.run*, and so on. If you need to run multiple loops (e.g.,
for testing), or want to create a loop with a custom precision, they can be
explicitly created and bound to a variable using *thor.loop.make*.


### _thor.loop_ thor.loop.make ( _int_ `precision`? )

Create and return a named loop that is suitable for the current system. If
`precision` is given, it indicates how often scheduled events will be run.

Returned loop instances have all of the methods and instance variables that
*thor.loop* has.


### _void_ thor.loop.run ()

Start the loop. Events can be scheduled, etc., before the loop is run, but
they won't fire until it starts running.


### _void_ thor.loop.stop ()

Stop the loop. Some events may still run while the loop is stopping. Stopping
the loop clears all scheduled events and forgets the file descriptors that
it's interested in.


### _object_ thor.loop.schedule ( _int_ `delta`, _func_ `callback`,  _arg_* )

Schedule callable `callback` to be called `delta` seconds from now, with
one or more `arg`s.

Returns an object with a *delete* () method; if called, it will remove the
timeout.


### _bool_ thor.loop.running

Read-only boolean that is True when the loop is running.


### _bool_ thor.loop.debug

Boolean that, when True, prints warnings to STDERR when the loop is
behaving oddly; e.g., a scheduled event is blocking. Default is False.


### event 'start' ()

Emitted right before loop starts.


### event 'stop' ()

Emitted right after the loop stops.
