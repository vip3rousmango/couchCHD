"""Foreground-only, read-only LAN transfer of an explicitly selected platform snapshot."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import re
import select
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .handheld import _read_source, platform_sources
from .ledger import SafetyError

_DISCOVERY_PORT = 42846
_PAIR_SECONDS = 5 * 60
_IDLE_SECONDS = 10 * 60
_MAX_SECONDS = 12 * 60 * 60
_REQUEST_SECONDS = 15
_CHUNK = 256 * 1024
_RATE_SAMPLE = 0.5
_RATE_STALE = 3.0
_PRIVATE = tuple(map(ipaddress.IPv4Network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16')))


def _private(addr):
    return any(addr in net for net in _PRIVATE)


def _lan():
    """Select a private broadcast interface and its real prefix, never a wildcard TLS endpoint."""
    if sys.platform not in ('linux', 'darwin'):
        raise SafetyError('Wi-Fi sending needs a Linux or macOS host.')
    import fcntl
    # SIOCGIFFLAGS, SIOCGIFADDR, SIOCGIFNETMASK; sockaddr_in.sin_addr is at offset 4.
    flags, addr, mask = ((0x8913, 0x8915, 0x891b) if sys.platform == 'linux'
                         else (0xc0206911, 0xc0206921, 0xc0206925))
    candidates = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, name in socket.if_nameindex():
            req = struct.pack('256s', name.encode())
            try:
                bits = struct.unpack_from('H', fcntl.ioctl(sock, flags, req), 16)[0]
                if bits & 3 != 3 or bits & (8 | 16):  # UP+BROADCAST, not LOOPBACK/POINTOPOINT
                    continue
                ip = socket.inet_ntoa(fcntl.ioctl(sock, addr, req)[20:24])
                netmask = socket.inet_ntoa(fcntl.ioctl(sock, mask, req)[20:24])
                iface = ipaddress.IPv4Interface(f'{ip}/{netmask}')
            except (OSError, ValueError):
                continue
            if _private(iface.ip) and iface.network.prefixlen:
                candidates.append(iface)
        if len(candidates) == 1:
            return candidates[0]
        # UDP connect performs a routing lookup only; no traffic leaves the host.
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.connect(('255.255.255.255', _DISCOVERY_PORT))
            preferred = ipaddress.IPv4Address(sock.getsockname()[0])
            for iface in candidates:
                if iface.ip == preferred:
                    return iface
    raise SafetyError('Connect the host to a private IPv4 LAN with the handheld, then reopen Send over Wi-Fi.')


def _identity(ip):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'CouchCHD Wi-Fi sender')])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]), critical=False)
            .sign(key, hashes.SHA256()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.NamedTemporaryFile(prefix='couchchd-pair-') as pem:  # private mode, unlinked before listen()
        pem.write(cert.public_bytes(serialization.Encoding.PEM))
        pem.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
        pem.flush()
        ctx.load_cert_chain(pem.name)
    return ctx, cert.fingerprint(hashes.SHA256())


def _json(obj):
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _display_name(value):
    """Plain ASCII UI hint, never a credential, address, or persistent identifier."""
    name = re.sub(r'[^A-Za-z0-9._() -]+', ' ', value[:128])
    name = ' '.join(name.split())[:48].rstrip()
    return name if re.search(r'[A-Za-z0-9]', name) else ''


class _Request(BaseHTTPRequestHandler):
    # One request per connection. Never log request lines, headers or library paths.
    def log_message(self, *args):
        pass

    def _reply(self, status, data=b'', content_type='application/json'):
        self._headers(status, len(data), content_type)
        if data:
            self.wfile.write(data)

    def _headers(self, status, size, kind):
        self.close_connection = True
        self.send_response_only(status)
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(size))
        self.end_headers()

    def _token(self):
        auth = self.headers.get_all('Authorization', [])
        if len(auth) == 1 and (m := re.fullmatch(r'Bearer ([0-9a-f]{64})', auth[0])):
            return bytes.fromhex(m[1])
        return None

    def do_POST(self):
        if self.headers.get_all('Transfer-Encoding') or self.headers.get_all('Content-Length', ['0']) != ['0']:
            return self._reply(400)
        token = self._token()
        if token is None:
            return self._reply(403)
        if self.path == '/pair':
            names = self.headers.get_all('X-CouchCHD-Device', [])
            status, code = self.server._pair(self.client_address[0], token, names[0] if len(names) == 1 else '')
            if not code:
                return self._reply(status)
            response = {'code': code}
            if self.server._host:
                response['host'] = self.server._host
            return self._reply(status, _json(response))
        if self.path == '/done' and self.server._authorized(self.client_address[0], token):
            self.server._stop('complete', 'Receiver confirmed completion. Wi-Fi sender closed.', finish_reply=True)
            return self._reply(200)
        self._reply(403 if self.path == '/done' else 404)

    def do_GET(self):
        sender = self.server
        if not sender._authorized(self.client_address[0], self._token()):
            return self._reply(403)
        if self.path == '/manifest':
            return self._reply(200, sender._manifest)
        m = re.fullmatch(r'/file/(0|[1-9][0-9]{0,4})', self.path)
        if not m or int(m[1]) >= len(sender._files):
            return self._reply(404)
        path, source = sender._files[int(m[1])]
        started = False
        try:
            sender._begin_file(path.as_posix(), source.size)
            with source.stream() as stream:  # nofollow walk; checks the original stat tuple on both ends
                started = True
                self._headers(200, source.size, 'application/octet-stream')
                remaining = source.size
                while remaining:
                    chunk = stream.read(min(remaining, _CHUNK))
                    if not chunk:
                        raise SafetyError('Source truncated during transfer.')
                    remaining -= len(chunk)
                    if remaining:
                        self.wfile.write(chunk)
                        sender._wrote(len(chunk))
            # Withhold the tail until the final identity check; a changed stream cannot appear complete.
            self.wfile.write(chunk)
            sender._wrote(len(chunk), final=True)
        except (ValueError, OSError) as exc:
            # Never continue an invalid snapshot. A disconnected peer, in contrast, can safely retry.
            if not started or isinstance(exc, SafetyError):
                sender._stop('error', 'Selected source changed or disappeared. Reopen the sender with a fresh selection.',
                             finish_reply=not started)
                if not started:
                    self._reply(409)
            else:
                sender._interrupted()


class _Sender:
    def __init__(self, iface, ctx, fingerprint, files, manifest, resources, check):
        self._iface, self._ctx, self._fingerprint = iface, ctx, fingerprint
        self._files, self._manifest = files, manifest
        self._lock = threading.Lock()
        self._released = threading.Event()
        self._closed = self._approved = False
        self._candidate = self._peer = None
        self._host = self._device = self._file = ''
        with contextlib.suppress(OSError):
            self._host = _display_name(socket.gethostname().partition('.')[0])
        self._phase = 'waiting'
        self._sent = self._total = self._rate = self._sample_bytes = 0
        self._last_write = self._sample_time = 0.0
        self._finish_reply = False
        self._code = ''
        self._status = 'On the handheld, tap Find and compare the pairing code.'
        self._until = self._idle = time.monotonic() + _PAIR_SECONDS
        self._active = None
        self._request_until = 0.0
        with contextlib.ExitStack() as sockets:
            check()
            self._tcp = sockets.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            self._tcp.bind((str(iface.ip), 0))
            check()
            self._tcp.listen(4)
            self._tcp.setblocking(False)
            port = self._tcp.getsockname()[1]
            self.address = f'{iface.ip}:{port}'
            self._advert = _json({'port': port, 'fingerprint': fingerprint.hex()})
            self._udp = sockets.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            # Broadcast discovery requires INADDR_ANY. Only same-link private peers receive a reply;
            # this UDP socket never accepts credentials, file paths, manifests, or file requests.
            self._udp.bind(('', _DISCOVERY_PORT))
            self._udp.setblocking(False)
            rx, tx = socket.socketpair()
            self._rx, self._tx = sockets.enter_context(rx), sockets.enter_context(tx)
            self._rx.setblocking(False)
            self._tx.setblocking(False)
            listeners = sockets.pop_all()
            try:
                check()
                threading.Thread(target=self._listen, args=(listeners, resources),
                                 name='CouchCHD Wi-Fi', daemon=True).start()
            except BaseException:
                listeners.close()
                raise

    @property
    def status(self):
        with self._lock:
            return self._status

    @property
    def transfer(self):
        """Session-only display snapshot; counts describe writes for one file, not receipt on the card."""
        with self._lock:
            fresh = self._phase == 'sending' and time.monotonic() - self._last_write < _RATE_STALE
            return dict(phase=self._phase, device=self._device, peer=self._peer or '', file=self._file,
                        sent=self._sent, total=self._total, rate=self._rate if fresh else 0)

    @property
    def closed(self):
        with self._lock:
            return self._closed

    @property
    def pair_code(self):
        with self._lock:
            return self._code if not self._approved and not self._closed else ''

    def _pair(self, peer, token, device):
        with self._lock:
            if self._closed or time.monotonic() >= min(self._until, self._idle):
                return 403, ''
            if self._candidate is None:
                self._peer, self._candidate = peer, token
                self._phase, self._device = 'pairing', _display_name(device)
                raw = hashlib.sha256(self._fingerprint + token).hexdigest()[:12]
                self._code = f'{raw[:4]}-{raw[4:8]}-{raw[8:]}'
                self._status = 'Compare codes on both devices before approving.'
            elif peer != self._peer or not hmac.compare_digest(token, self._candidate):
                return 409, ''
            return 200, self._code

    def approve(self):
        with self._lock:
            if self._closed or self._candidate is None or time.monotonic() >= self._until:
                raise SafetyError('There is no current pairing request to approve.')
            if not self._approved:
                self._approved = True
                now = time.monotonic()
                self._until, self._idle = now + _MAX_SECONDS, now + _IDLE_SECONDS
                self._phase = 'connected'
                self._status = 'Approved. Choose the platform folder on the handheld.'

    def _authorized(self, peer, token):
        with self._lock:
            now = time.monotonic()
            if (self._closed or now >= min(self._until, self._idle) or not self._approved or peer != self._peer
                    or token is None or not hmac.compare_digest(token, self._candidate)):
                return False
            self._idle = now + _IDLE_SECONDS
            return True

    def _begin_file(self, path, total):
        with self._lock:
            if self._closed:
                raise OSError('Sender closed.')
            now = time.monotonic()
            self._phase, self._status = 'sending', f'Sending {path}'
            self._file, self._total = path, total
            self._sent = self._rate = self._sample_bytes = 0
            self._sample_time = self._last_write = now
            self._idle, self._request_until = now + _IDLE_SECONDS, now + _REQUEST_SECONDS

    def _wrote(self, size, *, final=False):
        with self._lock:
            if self._closed:
                raise OSError('Sender closed.')
            now = time.monotonic()
            self._sent += size
            self._idle, self._request_until = now + _IDLE_SECONDS, now + _REQUEST_SECONDS
            if final:
                self._phase, self._status = 'verifying', 'Waiting for receiver to verify or request the next file.'
            else:
                elapsed = now - self._sample_time
                if elapsed >= _RATE_SAMPLE:
                    # Exclude old bytes when a stalled transfer resumes.
                    self._rate = int((self._sent - self._sample_bytes) / elapsed) if elapsed < _RATE_STALE else 0
                    self._sample_bytes, self._sample_time = self._sent, now
            self._last_write = now

    def _interrupted(self):
        with self._lock:
            if not self._closed:
                self._phase = 'interrupted'
                self._status = 'File transfer interrupted. Retry on the handheld or close the sender.'

    def _wake(self):
        with contextlib.suppress(OSError):
            self._tx.send(b'!')

    def _stop(self, phase, reason, *, finish_reply=False):
        with self._lock:
            if self._closed:
                return
            self._closed, self._phase, self._status = True, phase, reason
            self._finish_reply = finish_reply
            self._candidate = None
            self._code = ''
        self._wake()

    def close(self):
        """Stop discovery and serving, including any in-flight socket; safe to call repeatedly."""
        self._stop('closed', 'Wi-Fi sender closed.')
        self._released.wait()

    def _peer_allowed(self, peer):
        ip = ipaddress.IPv4Address(peer)
        return ip in self._iface.network and (_private(ip) or (ip.is_loopback and self._iface.ip.is_loopback))

    def _serve(self, tls, address):
        try:
            with tls:
                tls.do_handshake()
                _Request(tls, address, self)
        except (OSError, ValueError):
            pass  # Untrusted peer disconnected, failed TLS or timed out.
        finally:
            with self._lock:
                self._active = None
            self._wake()

    @staticmethod
    def _interrupt(sock):
        if sock is not None:
            with contextlib.suppress(OSError):
                socket.socket.shutdown(sock, socket.SHUT_RDWR)

    def _listen(self, listeners, resources):
        worker = None
        try:
            while not self.closed:
                now = time.monotonic()
                with self._lock:
                    expiry, active, io_limit = min(self._until, self._idle), self._active, self._request_until
                if now >= expiry:
                    self._stop('closed', 'Wi-Fi session expired. Open Send over Wi-Fi to start again.')
                    break
                if active is not None and now >= io_limit:
                    self._interrupt(active)  # Absolute header/handshake limit, even when bytes trickle in.
                readers = [self._rx, self._udp] if active is not None else [self._rx, self._udp, self._tcp]
                ready, _, _ = select.select(readers, [], [], min(0.5, expiry - now))
                if self._rx in ready:
                    self._rx.recv(1024)
                if self.closed:
                    break
                if self._udp in ready:
                    data, addr = self._udp.recvfrom(128)
                    if data == b'CouchCHD/1 find' and self._peer_allowed(addr[0]):
                        with contextlib.suppress(OSError):
                            self._udp.sendto(self._advert, addr)
                if self._tcp in ready:
                    raw, addr = self._tcp.accept()
                    with raw:
                        if not self._peer_allowed(addr[0]):
                            continue
                        raw.settimeout(_REQUEST_SECONDS)
                        tls = self._ctx.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
                    with self._lock:
                        self._active = tls
                        self._request_until = time.monotonic() + _REQUEST_SECONDS
                    worker = threading.Thread(target=self._serve, args=(tls, addr), name='CouchCHD transfer', daemon=True)
                    worker.start()
        except (OSError, ValueError):
            self._stop('error', 'Wi-Fi connection unavailable. Reopen the sender on the local network.')
        finally:
            self._stop('closed', 'Wi-Fi sender closed.')
            with self._lock:
                if not self._finish_reply:  # Let the terminal 200/409 reach the client under its socket timeout.
                    self._interrupt(self._active)
            listeners.close()
            self._released.set()
            if worker is not None:
                worker.join()
            resources.close()  # Keep the nofollow root fd pinned until the last reader releases it.


def start_sender(root, selected, profile, *, players=None, cancel: threading.Event | None = None):
    """Snapshot and hash selected files before opening a foreground session. Call off the UI thread."""
    def check():
        if cancel is not None and cancel.is_set():
            raise SafetyError('Wi-Fi send cancelled.')

    check()
    resources = contextlib.ExitStack()
    try:
        system, files = resources.enter_context(platform_sources(root, selected, profile, players=players))
        if len(files) > 10_000:
            raise SafetyError('Select at most 10,000 files per Wi-Fi transfer.')
        check()
        iface = _lan()
        rows = [dict(id=i, path=path.as_posix(), size=source.size, sha256=_read_source(source, path, check=check).hex())
                for i, (path, source) in enumerate(files)]
        manifest = _json(dict(system=system, files=rows))
        if len(manifest) > 4 * 1024 * 1024:
            raise SafetyError('Transfer manifest exceeds 4 MiB; choose fewer games.')
        check()
        ctx, fingerprint = _identity(iface.ip)
        return _Sender(iface, ctx, fingerprint, files, manifest, resources, check)
    except BaseException:
        resources.close()
        raise
