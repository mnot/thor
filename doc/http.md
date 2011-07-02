# HTTP

Thor provides both a HTTP/1.1 client and server implementation. They support most HTTP features, such as:

* Persistent Connections (HTTP/1.0-style as well as 1.1)
* Headers across multiple lines
* Chunked requests


## thor.http.HttpClient ( _loop_, _idle_timeout_ )

Instantiates a HTTP client. If _loop_ is supplied, it will be used as the *thor.loop*; otherwise, the "default" loop will be used.

HTTP clients share a pool of idle connections.

There are several settings available as class variables:

* HttpClient.tcp_client_class - what to use as a TCP client; must implement *thor.TcpClient*.
* HttpClient.connect_timeout - connect timeout, in seconds. Default _None_.
* HttpClient.read_timeout - timeout between reads on an active connection, in seconds. Default _None_.
* HttpClient.idle_timeout - how long idle persistent connections are left open, in seconds. Default 60; None to disable.
* HttpClient.retry_limit - How many additional times to try a request that fails (e.g., dropped connection). Default _2_.
* HttpClient.retry_delay - how long to wait between retries, in seconds (or fractions thereof). Default _0.5_.


### thor.HttpClient.exchange ()

Create a request/response exchange.


#### thor.HttpClient.exchange.request\_start ( _method_, _uri_, _headers_ )

Start the request to _uri_ using _method_ and a list of tuples _headers_ (see [working with HTTP headers](#headers)).

Note that hop-by-hop headers will be stripped from _headers_; Thor manages its own connections headers (such as _Connection_, _Keep-Alive_, and so on.)

After calling *request_start*, *request_body* may be called zero or more times, and then *request_done* must be called.

#### thor.HttpClient.exchange.request\_body ( _chunk_ ) 

Send a _chunk_ of request body content.


#### thor.HttpClient.exchange.request\_done ( _trailers_ )

Signal that the request body is finished. This must be called for every request. _trailers_ is the list of HTTP trailers; see [working with HTTP headers](#headers).


#### Event 'response\_start' ( _status_, _phrase_, _headers_ )

Emitted when the client starts receiving the exchange's response. _status_ and _phrase_ contain the HTTP response status code and reason phrase, respectively, and _headers_ contains the response header tuples (see [working with HTTP headers](#headers)).


#### Event 'response\_body' ( _chunk_ )

Emitted when a _chunk_ of the response body is received.


#### Event 'response\_done' ( _trailers_ )

Emitted when the response is successfully completed. _trailers_ is the list
of HTTP trailers; see [working with HTTP headers](#headers).


#### Event 'error' ( _err_ )

Emitted when there is an error with the request or response. _err_ is an instance of one of the *thor.http.error* classes that describes what happened.

If *error* is emitted, no other events will be emitted by this exchange.



## thor.http.HttpServer ( _host_, _port_, _loop_ )

Creates a new server listening on _host_:_port_. If _loop_ is supplied, it will be used as the *thor.loop*; otherwise, the "default" loop will be used. 

The following settings are available as class variables:

* HttpServer.tcp_server_class - what to use as a TCP server; must implement *thor.TcpServer*.
* HttpServer.idle_timeout - how long idle persistent connections are left open, in seconds. Default 60; None to disable.


### Event 'exchange' ( _exchange_ )

Emitted when the server starts a new request/response _exchange_.


#### event 'request\_start' ( _method_, _uri_, _headers_ )

Emitted when the exchange receives a request to _uri_ using _method_ and a list of tuples _headers_ (see [working with HTTP headers](#headers)).


#### event 'request\_body' ( _chunk_ )

Emitted when a _chunk_ of the request body is received.


#### event 'request\_done' ( _trailers_ )

Emitted when the request is successfully completed. _trailers_ is the list of HTTP trailers; see [working with HTTP headers](#headers).


#### exchange.response\_start ( _status_, _phrase_, _headers_ )

Start sending the exchange's response. _status_ and _phrase_ should contain the HTTP response status code and reason phrase, respectively, and _headers_ should contain the response header tuples (see [working with HTTP headers](#headers)).

Note that hop-by-hop headers will be stripped from _headers_; Thor manages its own connections headers (such as _Connection_, _Keep-Alive_, and so on.)


#### exchange.response\_body ( _chunk_ )

Send a _chunk_ of response body content.


#### exchange.response\_done ( _trailers_ )

Signal that the response body is finished. This must be called for every response. _trailers_ is the list of HTTP trailers; see [working with HTTP headers](#headers).


<span id="headers"/>
## Working with HTTP Headers 

In Thor's HTTP APIs, headers are moved around as lists of tuples, where each tuple is a (field-name, field-value) pair. For example:

    [
        ("Content-Type", " text/plain"),
        ("Foo", "bar, baz"),
        ("Cache-Control", " max-age=30, must-revalidate"),
        ("Foo", "boom"),
        ("user-agent", "Foo/1.0")
    ]

This is an intentionally low-level representation of HTTP headers; each tuple corresponds to one on-the-wire line, in order. That means that a field-name can appear more than once (note that 'Foo' appears twice above), and that multiple values can appear in one field-value (note the "Foo" and "Cache-Control" headers above). Whitespace can appear at the beginning of field-values, and field-names are not case-normalised.

Thor has several utility functions for manipulating this data structure; see [thor.http.header_names](#header_names), [thor.http.header_dict](#header_dict), and [thor.http.get_header](#get_header)


<span id="header_names"/>
### thor.http.header\_names ( _headers_ )

Given a list of header tuples _headers_, return the set of header field-names present.


<span id="header_dict"/>
### thor.http.header\_dict ( _headers_, _omit_ )

Given a list of header tuples _headers_, return a dictionary whose keys are the header field-names (normalised to lower case) and whose values are lists of field-values. 

Note that header field-values containing commas are split into separate values. Therefore, this function is NOT suitable for use on fields whose values may contain commas (e.g., in quoted strings, or in cookie values).

If _omit_, a list of field-names, is specified, those field names will be omitted from the dictionary.


<span id="get_header"/>
### thor.http.get\_header ( _headers_, _fieldname_ )

Given a list of header tuples _headers_, return a list of field-values for the given _fieldname_. 

Note that header field-values containing commas are split into separate values. Therefore, this function is NOT suitable for use on fields whose values may contain commas (e.g., in quoted strings, or in cookie values).


