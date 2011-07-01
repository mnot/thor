## Event Loops

Thor creates a "default" event loop in the *thor.loop* namespace which can be 
run using *thor.loop.run*, and so on. If you need to run multiple loops (e.g., 
for testing), or want to create a loop with a custom precision, they can be 
explicitly created and bound to a variable using *thor.loop.make*.


### thor.loop.make ( _precision_ )

Create and return a named loop that is suitable for the current system. If 
_precision_ is given, it indicates how often scheduled events will be run.

Returned loop instances have all of the methods and instance variables that 
*thor.loop* has.


### thor.loop.run ()

Start the loop. Events can be scheduled, etc., before the loop is run, but
they won't fire until it starts running.


### thor.loop.stop ()

Stop the loop. Some events may still run while the loop is stopping. Stopping
the loop clears all scheduled events and forgets the file descriptors that
it's interested in.


### thor.loop.schedule ( _delta_, _callback_, _arg_, ... )

Schedule callable _callback_ to be called _delta_ seconds from now, with
one or more _arg_s.

Returns an object with a *delete* () method; if called, it will remove the
timeout.


### thor.loop.time ()

Returns the current Unix timestamp, using the loop to save a system call
when possible. 

Note that the precision of the timestamp is determined by the _precision_ of 
the loop. Therefore, this method is not suitable for high-precision timers, 
but is useful when a reasonable resolution is adequate (e.g., in non-critical
logfiles).


### thor.loop.running 

Boolean that is True when the loop is running.


### event 'start'

Emitted right before loop starts.


### event 'stop'

Emitted right after the loop stops.