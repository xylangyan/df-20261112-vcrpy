Configuration
=============

If you don't like VCR's defaults, you can set options by instantiating a
``VCR`` class and setting the options on it.

.. code:: python


    import vcr

    my_vcr = vcr.VCR(
        serializer='json',
        cassette_library_dir='fixtures/cassettes',
        record_mode='once',
        match_on=['uri', 'method'],
    )

    with my_vcr.use_cassette('test.json'):
        # your http code here

Otherwise, you can override options each time you use a cassette.

.. code:: python

    with vcr.use_cassette('test.yml', serializer='json', record_mode='once'):
        # your http code here

Note: Per-cassette overrides take precedence over the global config.

Request matching
----------------

Request matching is configurable and allows you to change which requests
VCR considers identical. The default behavior is
``['method', 'scheme', 'host', 'port', 'path', 'query']`` which means
that requests with both the same URL and method (ie POST or GET) are
considered identical.

This can be configured by changing the ``match_on`` setting.

The following options are available :

-  method (for example, POST or GET)
-  uri (the full URI)
-  scheme (for example, HTTP or HTTPS)
-  host (the hostname of the server receiving the request)
-  port (the port of the server receiving the request)
-  path (the path of the request)
-  query (the query string of the request)
-  raw\_body (the entire request body as is)
-  body (the entire request body unmarshalled by content-type
   i.e. xmlrpc, json, form-urlencoded, falling back on raw\_body)
-  headers (the headers of the request)

   Backwards compatible matchers:
-  url (the ``uri`` alias)

Order-sensitive query parameters
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``query`` matcher sorts every query parameter before comparing, so a
request recorded with ``?sort=region&sort=date`` is also replayed for
``?sort=date&sort=region``. That is convenient when parameter order is
meaningless, but some APIs use the order of a repeated parameter (e.g. two
``sort`` keys to express primary and secondary sorting) as part of the
request.

Use ``VCR.order_sensitive_query_matcher`` to build a query matcher that keeps
the order of the parameters you name, while every other parameter keeps the
default order-insensitive comparison. Blank values, percent-encoding and
repeated keys are parsed exactly as the default ``query`` matcher does.

.. code:: python

    my_vcr = vcr.VCR()

    with my_vcr.use_cassette(
        'search.yaml',
        match_on=[
            'method', 'scheme', 'host', 'port', 'path',
            my_vcr.order_sensitive_query_matcher(['sort']),
        ],
    ):
        # ?sort=region&sort=date and ?sort=date&sort=region are now
        # distinct requests, but ?page=1&size=10 still matches ?size=10&page=1
        ...

The matcher is built per cassette, so the shared default ``query`` matcher
keeps its order-insensitive behavior. On a mismatch the assertion message
says whether the order-sensitive parameters have different values or the
same values in a different order.

If these options don't work for you, you can also register your own
request matcher. This is described in the Advanced section of this
README.
