#!/usr/bin/env python

"""
Asyncronous DNS. Currently just a stub resolver.

See:
  - RFC1035
  - http://www.merit.edu/events/mjts/pdf/20081007/Blunk_DNSCachePoisoning.pdf
"""

__author__ = "Mark Nottingham <mnot@mnot.net>"
__copyright__ = """\
Copyright (c) 2011 Mark Nottingham

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import random
import thor

class DnsStubResolver(object):
    """
    Very simple, non-recursing DNS resolver.
    """
    def __init__(self, resolver=None, loop=None):
        self.resolver = resolver # TODO: multiple resolvers
        # TODO: suck in resolv.conf
        self.__pool = DnsEndpointPool(loop)
    
    # TODO: probably move to separate emitter, rather than an explicit cb
    def resolve(self, name, rrtype, callback):
        """
        Resolve rrtype for name.
        """
        endp = self.__pool.get()
        endp.query(name, rrtype, self.resolver, callback)

        
class DnsEndpoint(DnsPacker, DnsUnpacker):
    """
    A DNS endpoint, which can be handling 0 to many queries simultaneously."""
    def __init__(self, pool, loop):
        DnsPacker.__init__(self)
        DnsUnpacker.__init__(self)
        self.__pool = pool
        self.__loop = loop
        self.__queries = {}
        self.__evicting = False
        self.__endp = thor.UdpEndpoint()
        self.__endp.on('datagram', self.handle_answer)
        self.__loop.schedule(pool.ep_ttl, self.evict)

    def query(self, name, rrtype, resolver, callback):
        """
        Make a query.
        """
        txid = 0 # FIXME
        # TODO: flesh out what a query is
        query = self.pack_msg(query, txid) # FIXME
        toev = self.__loop.schedule(
            self.__pool.q_ttl, self.handle_timeout, txid
        )
        self.__queries[txid] = [name, rrtype, callback, toev]
        self.__endp.send(query, resolver, 53)

    def handle_answer(self, datagram, host, port):
        """
        Handle an off-the-wire answer.
        """
        try:
            answer = self.unpack_msg(datagram)
        except:
            return # bad formatting
        try:
            name, rrtype, callback, toev = self.__queries[answer.txid]
        except KeyError:
            return # unsolicited response
        toev.delete()
        del self.__queries[answer.txid]
        if self.__evicting:
            self.evict()
        callback(answer)  ## FIXME - emit something, somewhere

    def handle_timeout(self, txid):
        """
        A query has timed out.
        """
        name, rrtype, callback = self.__queries[answer.txid]
        del self.__queries[answer.txid]
        if self.__evicting:
            self.evict()
        callback(error) # FIXME: error type
    
    def evict(self, force=False):
        """
        An endpoint's time has come.
        """
        if force or len(self.__queries) == 0:
            self.__endp.shutdown()
            for name, rrtype, callback, toev in self.__queries.values():
                toev.delete()
            self.__pool.evict(self)
        else:
            self.__evicting = True


class DnsEndpointPool(object):
    """
    Manager for a pool of DNS endpoints, taking care of port randomisation
    and lifecycle.
    
    .get() returns an endpoint; when done with it, .release() it.
    """
    size = 256 # source port randomisation.
    ep_ttl = 300 # seconds that an endpoint is "alive".
    q_ttl = 5 # seconds before queries time out.
    
    def __init__(self, loop=None):
        self.__loop = loop or thor.loop._loop
        self.__pool = [] 
        self.__rand = random.SystemRandom()
    
    def get(self):
        """
        Return a viable, random local endpoint.
        """
        if len(self.__pool) < self.size:
            # We haven't yet populated the pool, so mint a new endpoint.
            # We trust the OS to randomise the ports.
            endp = DnsEndpoint(self, self.__loop)
            self.__pool.append(endp)
            return endp
        else:
            # pool is full; chose a random pool member.
            return self.__rand.choice(self.__pool)

    def evict(self, endp):
        """
        Remove an endpoint from the pool.
        """
        self.__pool.remove(endp)
            
    def shutdown(self):
        """
        We're done.
        """
        # get rid of eviction events
        for endp in self.__pool:
            endp.shutdown(True)
        self.__pool = []

        
class DnsPacker(object):
    
    def pack_msg(self, msg):
        pass
    
class DnsUnpacker(object):
    
    def unpack_msg(self, data):
        pass
    
    
    