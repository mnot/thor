# UDP


## thor.UdpEndpoint (  _[thor.loop](loop.md)_ `loop`? )

A UDP endpoint. If `loop` is omitted, the "default" loop will be used.

Note that new endpoints will not emit *datagram* events until they are unpaused; see [thor.UdpEndpoint.pause](#void-thorudpendpointpause-bool-paused-).


### _int_ thor.UdpEndpoint.max\_dgram

The maximum number of bytes that sent with *send()*.


### _void_ thor.UdpEndpoint.bind ( _str_ `host`,  _int_ `port` )

Optionally binds the endpoint to _port_ on _host_ (which must be a local interface). If called, it must occur before *send()*.

If not called before *send()*, the socket will be assigned a random local port by the operating system. 


### _void_ thor.UdpEndpoint.send ( _bytes_ `datagram`,  _str_ `host`,  _int_ `port` )

Send `datagram` to `port` on `host`. 

Note that UDP is intrinsically an unreliable protocol, so the datagram may or may not be received. See also *thor.UdpEndpoint.max\_dgram.*


### _void_ thor.UdpEndpoint.pause (  _bool_ `paused` )

Stop the endpoint from emitting *datagram* events if `paused` is True; resume emitting them if False.


### _void_ thor.UdpEndpoint.shutdown ()

Stop the endpoint.


### event 'datagram' (  _bytes_ `datagram`,  _str_ `host`,  _int_ `port` )

Emitted when the socket receives `datagram` from `port` on `host`.