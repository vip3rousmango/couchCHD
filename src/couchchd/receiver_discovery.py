"""List foreground CouchCHD Receive companions, not arbitrary network hardware.

Single owner: the desktop pairing worker. UDP names and invitations are hints;
none of these operations approves a peer, exposes a manifest or starts a copy.
"""
from __future__ import annotations

import ipaddress
import re
import secrets
import socket
import time

from . import wifi_sender

_PORT = 42847
_INTERVAL = 3
_TTL = 11
_MAX_PEERS = 24
_PREFIX = b'CouchCHD/2 '
_REPLY = re.compile(rb'CouchCHD/2 here ([0-9a-f]{32}) ([0-9a-f]{32}) ([a-zA-Z0-9 .()_-]{1,48})')


class NearbyReceivers:
    def __init__(self, address: str):
        iface = wifi_sender._lan()
        if str(iface.ip) != address.partition(':')[0]:
            raise OSError('Sender network changed.')
        self._network = iface.network
        self._target = (str(iface.network.broadcast_address), _PORT)
        self._nonce = secrets.token_hex(16).encode('ascii')
        self._probe = _PREFIX + b'find ' + self._nonce
        self._next = 0.0
        self._peers = {}
        self._view = ()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._sock.bind((str(iface.ip), 0))
            self._sock.setblocking(False)
        except OSError:
            self.close()
            raise

    def poll(self) -> tuple[tuple[str, str], ...]:
        now = time.monotonic()
        changed = False
        if now >= self._next:
            self._sock.sendto(self._probe, self._target)
            self._next = now + _INTERVAL
            for ip in tuple(self._peers):
                if self._peers[ip][2] <= now:
                    del self._peers[ip]
                    changed = True
        for _ in range(32):  # Bound work even on a noisy LAN.
            try:
                data, (ip, port) = self._sock.recvfrom(192)
            except BlockingIOError:
                break
            if port != _PORT or ipaddress.IPv4Address(ip) not in self._network:
                continue
            m = _REPLY.fullmatch(data)
            if not m or m[1] != self._nonce:
                continue
            peer = self._peers.get(ip)
            if not peer and len(self._peers) >= _MAX_PEERS:
                continue
            identity = (m[2], m[3].decode('ascii').strip() or 'Android handheld')
            changed |= peer is None or peer[:2] != identity
            self._peers[ip] = (*identity, now + _TTL)
        if changed:
            self._view = tuple(sorted(((ip, entry[1]) for ip, entry in self._peers.items()),
                                      key=lambda row: (row[1].casefold(), row[0])))
        return self._view

    def invite(self, ip: str) -> bool:
        peer = self._peers.get(ip)
        if not peer or peer[2] <= time.monotonic():
            return False
        self._sock.sendto(_PREFIX + b'invite ' + self._nonce + b' ' + peer[0], (ip, _PORT))
        return True

    def close(self):
        self._sock.close()
