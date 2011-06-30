# UDP


## thor.UdpEndpoint ( _loop_ )

A UDP endpoint. _loop_ is a *thor.loop*; if omitted, the "default" loop will be used.

Note that new endpoints will not emit *datagram* events until they are unpaused;  see [thor.UdpEndpoint.pause](#pause).

### thor.UdpEndpoint.max\_dgram

The maximum number of bytes that sent with *send()*.


### thor.UdpEndpoint.bind ( _host_, _port_ )

Optionally binds the endpoint to _port_ on _host_ (which must be a local interface). If called, it must occur before *send()*.

If not called before *send()*, the socket will be assigned a random local port by the operating system. 


### thor.UdpEndpoint.send ( _datagram_, _host_, _port_ )

Send _datagram_ to _port_ on _host_. 

Note that UDP is intrinsically an unreliable protocol, so the datagram may or may not be received. See also *thor.UdpEndpoint.max\_dgram.*


### thor.UdpEndpoint.pause ( _paused_ )

Stop the endpoint from emitting *datagram* events if _paused_ is True; resume emitting them if False.


### thor.UdpEndpoint.shutdown ()

Stop the endpoint.


### event 'datagram' ( _datagram_, _host_, _port_ )

Emitted when the socket receives _datagram_ from _port_ on _host_.