#!/usr/bin/env python

"""
push-based asynchronous UDP

This is a generic library for building event-based / asynchronous
UDP servers and clients.
"""

from __future__ import absolute_import

import errno
import socket

from thor.loop import EventSource


class UdpEndpoint(EventSource):
    """
    An asynchronous UDP endpoint.

    Emits:
      - datagram (data, address): upon recieving a datagram.

    To start:

    > s = UdpEndpoint(host, port)
    > s.on('datagram', datagram_handler)
    """
    recv_buffer = 8192
    _block_errs = set([
        errno.EAGAIN, errno.EWOULDBLOCK
    ])

    def __init__(self, loop=None):
        EventSource.__init__(self, loop)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.max_dgram = min((2**16 - 40), self.sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_SNDBUF
        ))
        self.on('readable', self.handle_datagram)
        self.register_fd(self.sock.fileno())

    def bind(self, host, port):
        """
        Bind the socket bound to host:port. If called, must be before
        sending or receiving.

        Can raise socket.error if binding fails.
        """
        # TODO: IPV6
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))

    def shutdown(self):
        "Close the listening socket."
        self.removeListeners('readable')
        self.sock.close()
        # TODO: emit close?

    def pause(self, paused):
        "Control incoming datagram events."
        if paused:
            self.event_del('readable')
        else:
            self.event_add('readable')

    def send(self, datagram, host, port):
        "send datagram to host:port."
        try:
            self.sock.sendto(datagram, (host, port))
        except socket.error as why:
            if why[0] in self._block_errs:
                pass # we drop these on the floor. It's UDP, after all.
            else:
                raise

    def handle_datagram(self):
        "Handle an incoming datagram, emitting the 'datagram' event."
        # TODO: consider pre-allocating buffers.
        # TODO: is it best to loop here?
        while True:
            try:
                data, addr = self.sock.recvfrom(self.recv_buffer)
            except socket.error as why:
                if why.args[0] in self._block_errs:
                    break
                else:
                    raise
            self.emit('datagram', data, addr[0], addr[1])
