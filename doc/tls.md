# TLS/SSL

<span id="TlsClient"/>
## thor.TlsClient ( _loop_ )

A TCP client with a SSL/TLS wrapper. The interface is identical to that
of *thor.TcpClient*.

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

    c = thor.TlsClient()
    c.on('connect', handle_connect)
    c.on('connect_error', handle_err)
    c.connect(test_host, test_port)
    thor.run()

