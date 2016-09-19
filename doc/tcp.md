# TCP


## thor.TcpClient ( _[thor.loop](loop.md)_ `loop`? )

A TCP client. If `loop` is omitted, the "default" loop will be used.

Note that new connections will not emit *data* events until they are unpaused; see [thor.tcp.TcpConnection.pause](#void-thortcptcpconnnectionpause--bool-paused-).

For example:

    import sys
    import thor

    test_host, test_port = sys.argv[1:2]
    
    def handle_connect(conn):
        conn.on('data', sys.stdout.write)
        conn.on('close', thor.stop)
        conn.write(b"GET /\n\n")
        conn.pause(False)
        
    def handle_err(err_type, err):
        sys.stderr.write(str(err_type))
        thor.stop()

    c = thor.TcpClient()
    c.on('connect', handle_connect)
    c.on('connect_error', handle_err)
    c.connect(test_host, test_port)
    thor.run()


### _void_ thor.TcpClient.connect ( _str_ `host`,  _int_ `port`, _int_ `timeout`? )

Call to initiate a connection to `port` on `host`. [connect](#event-connect--tcpconnection-connection-) will be emitted when a connection is available, and [connect_error](#event-connect_error--errtype-error-) will be emitted when it fails.

If `timeout` is given, it specifies a connect timeout, in seconds. If the  timeout is exceeded and no connection or explicit failure is encountered, [connect_error](#event-connect_error--errtype-error-) will be emitted with *socket.error* as the _errtype_ and  *errno.ETIMEDOUT* as the _error_.


#### event 'connect' ( _[TcpConnection](#thortcptcpconnection)_ `connection` )

Emitted when the connection has succeeded.


#### event 'connect\_error' ( _errtype_, _error_ )

Emitted when the connection failed. _errtype_ is *socket.error* or  *socket.gaierror*; _error_ is the error type specific to the type. 


## thor.TcpServer ( _str_ `host`,  _int_ `port`, _[thor.loop](loop.md)_ `loop`? )

A TCP server. `host` and `port` specify the host and port to listen on, respectively; if given, `loop` specifies the *thor.loop* to use. If `loop` is omitted, the "default" loop will be used.

Note that new connections will not emit *data* events until they are unpaused; see [pause](#void-thortcptcpconnnectionpause--bool-paused-).

For example:

    s = TcpServer("localhost", 8000)
    s.on('connect', handle_conn)


### event 'start' ()

Emitted when the server starts.


### event 'connect' ( _[TcpConnection](#thortcptcpconnection)_ `connection` )

Emitted when a new connection is accepted by the server. 


### event 'stop' ()

Emitted when the server stops.


### _void_ thor.TcpServer.close () 

Stops the server from accepting new connections.



## thor.tcp.TcpConnection

A single TCP connection.


### event 'data' ( _bytes_ `data` )

Emitted when incoming _data_ is received by the connection. See [thor.tcp.TcpConnection.pause](#void-thortcptcpconnnectionpause--bool-paused-) to control these events.


### event 'close' () 

Emitted when the connection is closed, either because the other side has closed it, or because of a network problem.


### event 'pause' ( _bool_ `paused` )

Emitted to indicate the pause state, using `paused`, of the outgoing side of the connection (i.e., the *write* side).

When True, the connection buffers are full, and *write* should not be called again until this event is emitted again with `paused` as False.


### _void_ thor.tcp.TcpConnection.write ( _bytes_ `data` )

Write _data_ to the connection. Note that it may not be sent immediately.


### _void_ thor.tcp.TcpConnnection.pause ( _bool_ `paused` )

Controls the incoming side of the connection (i.e., *data* events). When  `paused` is True, incoming [data](#event-data--bytes-data-) events will stop; when `paused` is false, they will resume again.

Note that by default, *TcpConnection*s are paused; i.e., to read from them, you must first *thor.tcp.TcpConnection.pause*(_False_).


### _void_ thor.tcp.TcpConnection.close ()

Close the connection. If there is data still in the outgoing buffer, it will be written before the socket is shut down.
