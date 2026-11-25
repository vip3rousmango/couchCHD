"""Local UDP companion discovery is a hint, not a retained peer or approval."""
import ipaddress
import socket
import select
from types import SimpleNamespace

from couchchd import receiver_discovery as nearby


def test_only_fresh_matching_receiver_can_be_invited(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(nearby, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(nearby.wifi_sender, '_lan', lambda: ipaddress.IPv4Interface('127.0.0.1/8'))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(('127.0.0.1', 0))
        receiver.settimeout(2)
        ip, port = receiver.getsockname()
        monkeypatch.setattr(nearby, '_PORT', port)
        browser = nearby.NearbyReceivers(ip + ':50000')
        try:
            browser._target = (ip, port)  # No LAN broadcast from the test suite.
            assert browser.poll() == () and not browser.invite(ip)
            probe, source = receiver.recvfrom(192)
            name, verb, nonce = probe.split()
            assert (name, verb) == (b'CouchCHD/2', b'find')
            cookie = b'a' * 32
            def reply(n, model):
                return b' '.join((name, b'here', n, cookie, model))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as wrong_port:
                wrong_port.sendto(reply(nonce, b'Other'), source)
            receiver.sendto(reply(b'0' * 31 + b'x', b'Other'), source)
            assert browser.poll() == () and not browser.invite(ip)
            receiver.sendto(reply(nonce, b'RG405M'), source)
            assert select.select([browser._sock], [], [], 2)[0]
            # Unrelated datagrams can arrive ahead of the valid reply.
            import time
            end = time.monotonic() + 2
            while not browser.poll() and time.monotonic() < end:
                select.select([browser._sock], [], [], max(0, end - time.monotonic()))
            assert browser.poll() == ((ip, 'RG405M'),)
            assert browser.invite(ip)
            assert receiver.recvfrom(192) == (b' '.join((name, b'invite', nonce, cookie)), source)
            clock[0] += 20
            assert not browser.invite(ip) and browser.poll() == ()
        finally:
            browser.close()
