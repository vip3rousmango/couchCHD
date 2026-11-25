"""Real UDP discovery and certificate-pinned TLS on loopback; no external device required."""
import hashlib
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from couchchd import handheld, wifi_sender


@pytest.fixture
def lan(monkeypatch):
    monkeypatch.setattr(wifi_sender, '_lan', lambda: ipaddress.IPv4Interface('127.0.0.1/8'))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(wifi_sender, '_DISCOVERY_PORT', port)
    return port


def discover(port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.settimeout(2)
        udp.sendto(b'CouchCHD/1 find', ('127.0.0.1', port))
        reply, peer = udp.recvfrom(512)
    advert = json.loads(reply)
    assert set(advert) == {'port', 'fingerprint'}  # No device names, credentials or library info on UDP.
    assert peer[0] == '127.0.0.1'
    return (peer[0], advert['port']), advert['fingerprint']


def request(destination, pin, method, path, token=None, *, device=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Verify the exact discovered cert before sending credentials.
    conn = http.client.HTTPSConnection(*destination, timeout=3, context=ctx)
    try:
        conn.connect()
        assert hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest() == pin
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        if device is not None:
            headers['X-CouchCHD-Device'] = device
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_only_approved_receiver_reads_selected_snapshot(tmp_path, lan, monkeypatch):
    monkeypatch.setattr(socket, 'gethostname', lambda: 'Deck<OLED>.private.lan')
    folder = tmp_path / 'PS1' / 'Set'
    (folder / '.chd_discs').mkdir(parents=True)
    disc = b'Disc data'
    playlist = b'#EXTM3U\n.chd_discs/One.chd\n'
    (folder / '.chd_discs/One.chd').write_bytes(disc)
    (folder / 'Game.m3u').write_bytes(playlist)
    (folder / 'Unselected.chd').write_bytes(b'not offered')
    sender = wifi_sender.start_sender(tmp_path, ['PS1/Set/Game.m3u'], 'rg405m')
    try:
        address, pin = discover(lan)
        assert sender.address == f'{address[0]}:{address[1]}'
        token, other = secrets.token_hex(32), secrets.token_hex(32)
        assert not sender.pair_code
        initial = dict(phase='waiting', device='', peer='', file='', sent=0, total=0, rate=0)
        assert sender.transfer == initial
        with pytest.raises(ValueError):
            sender.approve()
        assert request(address, pin, 'GET', '/manifest')[0] == 403
        assert request(address, pin, 'POST', '/pair', 'bogus')[0] == 403
        assert sender.transfer == initial
        status, body = request(address, pin, 'POST', '/pair', token, device='Anbernic\tRG405M <32GB>\xff')
        assert status == 200
        raw = hashlib.sha256(bytes.fromhex(pin + token)).hexdigest()[:12]
        assert sender.pair_code == json.loads(body)['code'] == f'{raw[:4]}-{raw[4:8]}-{raw[8:]}'
        assert json.loads(body)['host'] == 'Deck OLED'
        paired = dict(initial, phase='pairing', peer='127.0.0.1', device='Anbernic RG405M 32GB')
        assert sender.transfer == paired
        for secret in (token, raw, sender.pair_code, pin, str(tmp_path)):
            assert secret not in sender.status + json.dumps(sender.transfer)
        assert request(address, pin, 'POST', '/pair', other, device='Impostor') == (409, b'')
        assert sender.transfer == paired
        assert request(address, pin, 'GET', '/manifest', token)[0] == 403
        assert request(address, pin, 'GET', '/file/0', token)[0] == 403
        sender.approve()
        assert not sender.pair_code
        connected = dict(paired, phase='connected')
        assert sender.transfer == connected
        assert request(address, pin, 'POST', '/pair', other, device='Impostor') == (409, b'')
        assert request(address, pin, 'GET', '/manifest', other)[0] == 403
        status, body = request(address, pin, 'GET', '/manifest', token)
        expected = [('Set/.chd_discs/One.chd', disc), ('Set/Game.m3u', playlist)]
        assert status == 200
        assert json.loads(body) == {'system': 'psx', 'files': [
            {'id': i, 'path': path, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            for i, (path, data) in enumerate(expected)]}
        assert sender.transfer == connected
        for i, (path, data) in enumerate(expected):
            assert request(address, pin, 'GET', f'/file/{i}', token) == (200, data)
            # The next request is served serially, after the previous worker's final progress update.
            assert request(address, pin, 'POST', '/done', other)[0] == 403
            assert sender.transfer == dict(connected, phase='verifying', file=path, sent=len(data), total=len(data))
        for path in ['/file/2', '/file/../Unselected.chd', '/file/-1', '/']:
            assert request(address, pin, 'GET', path, token)[0] == 404
        assert request(address, pin, 'POST', '/done', token)[0] == 200
        assert sender.transfer == dict(connected, phase='complete', file='Set/Game.m3u',
                                       sent=len(playlist), total=len(playlist))
    finally:
        sender.close()
    assert sender.transfer['phase'] == 'complete'  # Closing again doesn't overwrite the acknowledgement.
    assert sender.closed
    with pytest.raises(OSError):
        socket.create_connection(address, timeout=1)


def test_speed_counts_successful_writes_and_resets_on_stall_and_retry(tmp_path, lan, monkeypatch):
    platform = tmp_path / 'psx'
    platform.mkdir()
    data = b'aaaabbbbccccdddd'
    (platform / 'Game.chd').write_bytes(data)
    monkeypatch.setattr(wifi_sender, '_CHUNK', 4)
    now = 100.0
    monkeypatch.setattr(socket, 'gethostname', lambda: '')
    monkeypatch.setattr(wifi_sender, 'time', SimpleNamespace(monotonic=lambda: now))
    sender = wifi_sender.start_sender(platform, 'Game.chd', 'rg405m')
    try:
        address, pin = discover(lan)
        token = secrets.token_hex(32)
        status, reply = request(address, pin, 'POST', '/pair', token)  # preview.18 has no device header.
        assert status == 200 and json.loads(reply) == {'code': sender.pair_code}
        assert sender.transfer['device'] == ''
        sender.approve()

        # Advance a monotonic clock at the TLS boundary. First attempt stalls, resumes, then drops a write.
        sendall = ssl.SSLSocket.sendall
        snapshots = []
        stalled = []
        drop = True

        def send(sock, chunk, *args, **kwargs):
            nonlocal now
            if chunk in (b'aaaa', b'bbbb', b'cccc', b'dddd'):
                snapshots.append(sender.transfer)
                if drop and chunk == b'bbbb':
                    now += 4
                    stalled.append(sender.transfer)
                if drop and chunk == b'cccc':
                    raise BrokenPipeError()
                now += 0.5
            return sendall(sock, chunk, *args, **kwargs)

        monkeypatch.setattr(ssl.SSLSocket, 'sendall', send)
        with pytest.raises(http.client.IncompleteRead) as failed:
            request(address, pin, 'GET', '/file/0', token)
        assert failed.value.partial == b'aaaabbbb'
        sending = dict(phase='sending', device='', peer='127.0.0.1', file='Game.chd', sent=0, total=16, rate=0)
        assert snapshots == [sending, dict(sending, sent=4, rate=8), dict(sending, sent=8)]
        assert stalled == [dict(sending, sent=4)]
        assert sender.transfer == dict(sending, phase='interrupted', sent=8)

        drop = False
        snapshots.clear()
        assert request(address, pin, 'GET', '/file/0', token) == (200, data)
        assert snapshots == [sending, *(dict(sending, sent=n, rate=8) for n in (4, 8, 12))]
        assert request(address, pin, 'POST', '/done', token) == (200, b'')
        assert sender.transfer == dict(sending, phase='complete', sent=16)
    finally:
        sender.close()


def test_changed_source_conflicts_and_close_releases_discovery(tmp_path, lan):
    platform = tmp_path / 'psx'
    platform.mkdir()
    disc = platform / 'Game.chd'
    disc.write_bytes(b'original')
    sender = wifi_sender.start_sender(platform, 'Game.chd', 'rg405m')
    try:
        address, pin = discover(lan)
        token = secrets.token_hex(32)
        label = 'RG405M ' + 'A' * 90
        assert request(address, pin, 'POST', '/pair', token, device=label)[0] == 200
        assert sender.transfer['device'] == label[:48]
        sender.approve()
        elsewhere = tmp_path / 'different'
        elsewhere.write_bytes(b'not allowed')
        disc.unlink()
        disc.symlink_to(elsewhere)
        status, body = request(address, pin, 'GET', '/file/0', token)
        assert status == 409 and b'not allowed' not in body
        assert sender.transfer == dict(phase='error', device=label[:48], peer='127.0.0.1',
                                       file='Game.chd', sent=0, total=8, rate=0)
    finally:
        sender.close()
        sender.close()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as check:
        check.bind(('', lan))


def test_sender_refuses_mixed_or_unsupported_selection(tmp_path):
    for system in ('psx', 'psp'):
        (tmp_path / system).mkdir()
        (tmp_path / system / 'Disc.chd').write_bytes(b'source')
    with pytest.raises(ValueError, match='one platform'):
        wifi_sender.start_sender(tmp_path, ['psx/Disc.chd', 'psp/Disc.chd'], 'rg405m')
    with pytest.raises(ValueError, match='Not a complete'):
        wifi_sender.start_sender(tmp_path, ['psp/Disc.chd'], 'android-t618', players=['pcsx-rearmed'])


def test_unpaired_sender_expires_during_stalled_handshake(tmp_path, lan, monkeypatch):
    platform = tmp_path / 'psx'
    platform.mkdir()
    (platform / 'Game.chd').write_bytes(b'data')
    monkeypatch.setattr(wifi_sender, '_PAIR_SECONDS', 1.0)
    sender = wifi_sender.start_sender(platform, 'Game.chd', 'rg405m')
    try:
        address, _ = discover(lan)
        with socket.create_connection(address, timeout=2) as stalled:
            until = time.monotonic() + 4
            while not sender.closed and time.monotonic() < until:
                time.sleep(0.05)
            assert sender.closed and 'expired' in sender.status.lower()
            assert sender.transfer == dict(phase='closed', device='', peer='', file='', sent=0, total=0, rate=0)
            assert stalled.recv(1) == b''
    finally:
        sender.close()


def test_back_interrupts_hashing_without_opening_listener(tmp_path, lan, monkeypatch):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ValueError, match='cancelled'):
        wifi_sender.start_sender(tmp_path / 'missing', 'Game.chd', 'rg405m', cancel=cancel)
    cancel.clear()
    platform = tmp_path / 'psx'
    platform.mkdir()
    with (platform / 'Game.chd').open('wb') as game:
        game.truncate(8 * 1024 * 1024)
    open_file = handheld.open_file
    reads = []

    @contextmanager
    def cancel_during_read(fd, file):
        with open_file(fd, file) as stream:
            def read(size):
                data = stream.read(size)
                reads.append(len(data))
                cancel.set()
                return data
            yield SimpleNamespace(read=read)

    monkeypatch.setattr(handheld, 'open_file', cancel_during_read)
    monkeypatch.setattr(socket, 'socket', lambda *args: pytest.fail('Cancelled sender opened a socket'))
    with pytest.raises(ValueError, match='cancelled'):
        wifi_sender.start_sender(platform, 'Game.chd', 'rg405m', cancel=cancel)
    assert len(reads) == 1 and 0 < reads[0] < (platform / 'Game.chd').stat().st_size
