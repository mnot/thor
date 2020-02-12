# HTTP

Thor provides both a HTTP/1.1 client and server implementation. They support most HTTP features, such as:

* Persistent Connections (HTTP/1.0-style as well as 1.1)
* Headers across multiple lines
* Chunked requests


## thor.http.HttpClient ( _[thor.loop](loop.md)_ `loop`?,  _int_ `idle_timeout`? )

Instantiates a HTTP client. If _loop_ is supplied, it will be used as the *thor.loop*; otherwise, the "default" loop will be used.

HTTP clients share a pool of idle connections.

There are several settings available as class variables:

* _thor.TcpClient_ `HttpClient.tcp_client_class` - what to use as a TCP client.
* _int_ or _None_ `HttpClient.connect_timeout` - connect timeout, in seconds. Default `None`.
* _int_ or _None_ `HttpClient.read_timeout` - timeout between reads on an active connection, in seconds. Default `None`.
* _int_ or _None_ `HttpClient.idle_timeout` - how long idle persistent connections are left open, in seconds. Default `60`; `None` to disable.
* _int_ `HttpClient.retry_limit` - How many additional times to try a request that fails (e.g., dropped connection). Default `2`.
* _int_ `HttpClient.retry_delay` - how long to wait between retries, in seconds (or fractions thereof). Default `0.5`.


### _thor.http.HttpClientExchange_ thor.http.HttpClient.exchange ()

Create a request/response exchange.

### thor.http.HttpClientExchange

#### _void_ request\_start ( _bytes_ `method`,  _bytes_ `uri`,  _[headers](#headers)_ `headers` )

Start the request to `uri` using `method` and a list of tuples `headers` (see [working with HTTP headers](#headers)).

note that `uri` is a string; if you want to use an IRI, you'll need to convert it first.

Also, hop-by-hop headers will be stripped from `headers`; Thor manages its own connections headers (such as _Connection_, _Keep-Alive_, and so on.)

After calling *request_start*, *request_body* may be called zero or more times, and then *request_done* must be called.


#### _void_ request\_body ( _bytes_ `chunk` ) 

Send a `chunk` of request body content.


#### _void_ request\_done ( _[headers](#headers)_ `trailers`? )

Signal that the request body is finished. This must be called for every request. `trailers` is the list of HTTP trailers; see [working with HTTP headers](#headers).


#### event 'response\_start' ( _bytes_ `status`,  _bytes_ `phrase`,  _[headers](#headers)_ `headers` )

Emitted once, when the client starts receiving the exchange's response. `status` and `phrase` contain the HTTP response status code and reason phrase, respectively, and `headers` contains the response header tuples (see [working with HTTP headers](#headers)).


#### event 'response\_body' ( _bytes_ `chunk` )

Emitted zero to many times, when a `chunk` of the response body is received.


#### event 'response\_done' (  _[headers](#headers)_ `trailers` )

Emitted once, when the response is successfully completed. `trailers` is the list
of HTTP trailers; see [working with HTTP headers](#headers).


#### event 'error' ( _[thor.http.error.HttpError](error.md)_ `err` )

Emitted when there is an error with the request or response. `err` is an instance of one of the *thor.http.error* classes that describes what happened.

If _err.client_recoverable_ is `False`, no other events will be emitted by this exchange.



## thor.http.HttpServer ( _bytes_ `host`, _int_ `port`,  _[thor.loop](loop.md)_ `loop`? )

Creates a new server listening on `host`:`port`. If `loop` is supplied, it will be used as the *thor.loop*; otherwise, the "default" loop will be used. 

The following settings are available as class variables:

* _thor.TcpServer_ `HttpServer.tcp_server_class` - what to use as a TCP server.
* _int_ or _None_ `HttpServer.idle_timeout` - how long idle persistent connections are left open, in seconds. Default 60; None to disable.

### event 'start' ()

Emitted when the server starts.

### event 'stop' ()

Emitted when the server stops.


### event 'exchange' ( _thor.http.HttpServerExchange_ `exchange` )

Emitted when the server starts a new request/response `exchange`.


### thor.http.HttpServerExchange


#### event 'request\_start' ( _bytes_ `method`,  _bytes_ `uri`,  _[headers](#headers)_ `headers` )

Emitted once, when the exchange receives a request to `uri` using `method` and a list of tuples `headers` (see [working with HTTP headers](#headers)).


#### event 'request\_body' ( _bytes_ `chunk` )

Emitted zero to many times, when a `chunk` of the request body is received.


#### event 'request\_done' (  _[headers](#headers)_ `trailers`? )

Emitted once, when the request is successfully completed. `trailers` is the list of HTTP trailers; see [working with HTTP headers](#headers).


#### _void_ response\_start ( _bytes_ `status`,  _bytes_ `phrase`,  _[headers](#headers)_ `headers` )

Start sending the exchange's response. `status` and `phrase` should contain the HTTP response status code and reason phrase, respectively, and `headers` should contain the response header tuples (see [working with HTTP headers](#headers)).

Note that hop-by-hop headers will be stripped from `headers`; Thor manages its own connections headers (such as _Connection_, _Keep-Alive_, and so on.)


#### _void_ response\_body ( _bytes_ `chunk` )

Send a `chunk` of response body content.


#### _void_ response\_done ( _[headers](#headers)_ `trailers` )

Signal that the response body is finished. This must be called for every response. `trailers` is the list of HTTP trailers; see [working with HTTP headers](#headers).



<span id="headers"/>

## Working with HTTP Headers 

In Thor's HTTP APIs, headers are moved around as lists of tuples, where each tuple is a (_bytes_ `field-name`, _bytes_ `field-value`) pair. For example:

    [
        ("Content-Type", b" text/plain"),
        ("Foo", b"bar, baz"),
        ("Cache-Control", b" max-age=30, must-revalidate"),
        ("Foo", b"boom"),
        ("user-agent", b"Foo/1.0")
    ]

This is an intentionally low-level representation of HTTP headers; each tuple corresponds to one on-the-wire line, in order. That means that a field-name can appear more than once (note that 'Foo' appears twice above), and that multiple values can appear in one field-value (note the "Foo" and "Cache-Control" headers above). Whitespace can appear at the beginning of field-values, and field-names are not case-normalised.

Thor has several utility functions for manipulating this data structure; see [thor.http.header_names](#header_names), [thor.http.header_dict](#header_dict), and [thor.http.get_header](#get_header)


<span id="header_names"/>

### _set_ thor.http.header\_names ( _[headers](#headers)_ `headers` )

Given a list of header tuples `headers`, return the set of _bytes_ header field-names present.


<span id="header_dict"/>

### _dict_ thor.http.header\_dict ( _[headers](#headers)_ `headers`,  _list_ `omit` )

Given a list of header tuples `headers`, return a dictionary whose keys are the _bytes_ header field-names (normalised to lower case) and whose values are lists of _bytes_ field-values. 

Note that header field-values containing commas are split into separate values. Therefore, this function is NOT suitable for use on fields whose values may contain commas (e.g., in quoted strings, or in cookie values).

If `omit`, a list of _bytes_ field-names, is specified, those field names will be omitted from the dictionary.


<span id="get_header"/>

### _list_ thor.http.get\_header ( _[headers](#headers)_ `headers`, _bytes_ `fieldname` )

Given a list of header tuples `headers`, return a list of _bytes_ field-values for the given `fieldname`. 

Note that header field-values containing commas are split into separate values. Therefore, this function is NOT suitable for use on fields whose values may contain commas (e.g., in quoted strings, or in cookie values).


