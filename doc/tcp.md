# TCP


<span id="TcpClient"/>
## thor.TcpClient ( _loop_ ) 

A TCP client. _loop_ is a *thor.loop*; if omitted, the "default" loop will be used.

Note that new connections will not emit *data* events until they are unpaused;  see [thor.tcp.TcpConnection.pause](#pause).

For example:

    import sys
    import thor

    test_host, test_port = sys.argv[1:2]
    
    def handle_connect(conn):
        conn.on('data', sys.stdout.write)
        conn.on('close', thor.stop)
        conn.write("GET /\n\n")
        conn.pause(False)
        
    def handle_err(err_type, err):
        sys.stderr.write(str(err_type))
        thor.stop()

    c = thor.TcpClient()
    c.on('connect', handle_connect)
    c.on('connect_error', handle_err)
    c.connect(test_host, test_port)
    thor.run()


<span id="client_connect"/>
### thor.TcpClient.connect ( _host_, _port_, _timeout_ ) 

Call to initiate a connection to _port_ on _host_. [connect](#client_connect_event) will be emitted when a connection is available, and [connect_error](#connect_error) will be emitted when it fails.

If _timeout_ is given, it specifies a connect timeout, in seconds. If the  timeout is exceeded and no connection or explicit failure is encountered, [connect_error](#connect_error) will be emitted with *socket.error* as the _errtype_ and  *errno.ETIMEDOUT* as the _error_.


<span id="client_connect_event"/>
### event 'connect' ( _connection_ ) 

Emitted when the connection has succeeded. _connection_ is a [TcpConnection](#TcpConnection).


<span id="connect_error"/>
### event 'connect\_error' ( _errtype_, _error_ )  

Emitted when the connection failed. _errtype_ is *socket.error* or  *socket.gaierror*; _error_ is the error type specific to the type. 


<span id="TcpServer"/>
## thor.TcpServer ( _host_, _port_, _loop_ ) 

A TCP server. _host_ and _port_ specify the host and port to listen on,  respectively; if given, _loop_ specifies the *thor.loop* to use. If _loop_ is omitted, the "default" loop will be used.

Note that new connections will not emit *data* events until they are unpaused;  see [thor.tcp.TcpConnection.pause](#pause).

For example:

    s = TcpServer("localhost", 8000)
    s.on('connect', handle_conn)


<span id="server_connect_event"/>
### event 'connect' ( _connection_ ) 

Emitted when a new connection is accepted by the server. _connection_ is a [TcpConnection](#TcpConnection).

### thor.TcpServer.close () <span id="server_close"/>

Stops the server from accepting new connections.


<span id="TcpConnection"/>
## thor.tcp.TcpConnection () 

A single TCP connection.


<span id="data_event"/>
### event 'data' ( _data_ ) 

Emitted when incoming _data_ is received by the connection. See [thor.tcp.TcpConnection.pause](#pause) to control these events.


### event 'close' () <span id="close_event"/>

Emitted when the connection is closed, either because the other side has closed it, or because of a network problem.


### event 'pause' ( _paused_ ) <span id="pause_event"/>

Emitted to indicate the pause state, using _paused_, of the outgoing side of the connection (i.e., the *write* side).

When True, the connection buffers are full, and *write* should not be called again until this event is emitted again with _paused_ as False.


<span id="write"/>
### thor.tcp.TcpConnection.write ( _data_ ) 

Write _data_ to the connection. Note that it may not be sent immediately.


<span id="pause"/>
### thor.tcp.TcpConnnection.pause ( _paused_ ) 

Controls the incoming side of the connection (i.e., *data* events). When  _paused_ is True, incoming [data](#data_event) events will stop; when _paused_ is false, they will resume again.

Note that by default, *TcpConnection*s are paused; i.e., to read from them, you must first *thor.tcp.TcpConnection.pause*(_False_).


<span id="close"/>
### thor.tcp.TcpConnection.close () 

Close the connection. If there is data still in the outgoing buffer, it will be written before the socket is shut down.
