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

class DnsStubResolver(DnsPacker, DnsUnpacker):
    """
    Very simple, non-recursing DNS resolver.
    """
    def __init__(self, resolver=None, loop=None):
        self.resolver = resolver # TODO: multiple resolvers
        # TODO: suck in resolv.conf
        self.__pool = DnsEndpointPool(loop)
        self.__requests = {}
    
    # TODO: probably move to separate emitter, rather than an explicit cb
    def lookup(self, query, callback):
        txid = # TODO
        # TODO: flesh out what a query is
        request = self.pack_msg(query, txid) # FIXME
        endp = self._pool.get()
        # FIXME: get local port
        self.__requests[txid] = [local_port, query, callback]
        endp.on('datagram', self.handle_response)
        endp.send(request, self.resolver, 53)
        # TODO: timeout

    def handle_response(self, datagram, host, port):
        try:
            answer = self.unpack_msg(datagram)
        except:
            # bad formatting
            return
        try:
            local_port, query, callback = self.__requests[answer.txid]
        except KeyError:
            # unsolicited response
            return
        if local_port != ...: # FIXME: get local port
            # spoofing
            return
        callback(answer)
   
        

class DnsEndpointPool(object):
    """
    Manager for a pool of DNS endpoints, taking care of port randomisation
    and lifecycle.
    
    .get() returns an endpoint; when done with it, .release() it.
    """
    size = 512 # a decent amount of randomisation.
    ep_ttl = 300 # seconds that an endpoint is "alive".
    
    def __init__(self, loop=None):
        self.__loop = loop or thor.loop._loop
        self.__pool = {} #   endp: [refcount, evicting, evict_ev]
        self.__rand = random.SystemRandom()
    
    def get(self):
        """
        Return a viable, random local endpoint.
        """
        if len(self.__pool) < self.size:
            # We haven't yet populated the pool, so mint a new endpoint.
            # We trust the OS to randomise the ports.
            endp = thor.UdpEndpoint(self.__loop)
            evev = self.__loop.schedule(self.ep_ttl, self.__evict, endp)
            self.__pool[endp] = [1, False, evev]
            return endp
        else:
            # pool is full; chose a random pool member.
            endp = self.__rand.choice(self.__pool.keys())
            self.__pool[endp][0] += 1
            return endp

    def release(self, endp):
        """
        The transaction with the endpoint has finished.
        """
        self.__pool[endp][0] -= 1
        if self.__pool[endp][:2] == [0, True]:
            # we're the last one.
            endp.shutdown()
            del self.__pool[endp]
        # TODO: deal with naughty folks who don't release; timeout?
            
    def shutdown(self):
        """
        We're done.
        """
        # get rid of eviction events
        for endp, [refcount, evicting, evev] in self.__pool.items():
            evev.delete()
            endp.shutdown()
        self.__pool = []


    def __evict(self, endp):
        """
        An endpoint's time has come.
        """
        if self.__pool[endp][0] > 0:
            # still outstanding users
            self.__pool[endp][1] = True
        else:
            # no one is using it, it's safe to just kick it.
            endp.shutdown()
            del self.__pool[endp]

        
class DnsPacker(object):
    
    def pack_msg(self, msg):
        pass
    
class DnsUnpacker(object):
    
    def unpack_msg(self, data):
        pass
    