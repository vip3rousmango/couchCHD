"""Release gate: real sender, installed APK, Android's picker and on-device SHA-256.

Run only on an empty disposable English Android emulator. No permission grants via adb.
"""
import hashlib
import os
import re
import secrets
import socket
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from couchchd.wifi_sender import start_pairing


def adb(*args):
    return subprocess.check_output(['adb', *args], timeout=20, text=True).strip()


def screen():
    adb('shell', 'uiautomator', 'dump', '/data/local/tmp/receive-window.xml')
    return ET.fromstring(adb('exec-out', 'cat', '/data/local/tmp/receive-window.xml')).findall('.//node')


def locate(*names, kind=None, resource=None, timeout=32):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        nodes = screen()
        for node in nodes:
            a = node.attrib
            if (kind and kind != a['class']) or (resource and resource != a['resource-id']):
                continue
            if not names or any(n.casefold() in {a.get(k, '').casefold() for k in ('text', 'resource-id', 'content-desc')} for n in names):
                return a
        time.sleep(.25)
    raise AssertionError(f'Control {names or kind} not found: {[(n.get("text"), n.get("content-desc")) for n in nodes]}')


def tap(*names, kind=None, resource=None):
    node = locate(*names, kind=kind, resource=resource)
    left, top, right, bottom = map(int, re.findall(r'\d+', node['bounds']))
    adb('shell', 'input', 'tap', str((left + right) // 2), str((top + bottom) // 2))


def screenshot(directory, name):
    with (directory / name).open('wb') as shot:
        subprocess.run(['adb', 'exec-out', 'screencap', '-p'], stdout=shot, timeout=10, check=True)


def invite_idle_companion(evidence):
    # Emulator NAT cannot relay a LAN broadcast. Forward just the presence
    # datagram; the actual APK still answers and validates the invitation.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        port = 42847
        assert adb('emu', 'redir', 'add', f'udp:{port}:{port}').startswith('OK')
        try:
            probe.settimeout(1)
            nonce = secrets.token_hex(16).encode('ascii')
            target = ('127.0.0.1', port)
            for _ in range(12):
                probe.sendto(b'CouchCHD/2 find ' + nonce, target)
                try:
                    reply, _ = probe.recvfrom(192)
                    break
                except socket.timeout:
                    continue
            else:
                raise AssertionError('Installed Android companion did not answer the local presence probe')
            prefix, action, echoed, cookie, label = reply.split(b' ', 4)
            assert (prefix, action, echoed) == (b'CouchCHD/2', b'here', nonce)
            assert label.strip() and re.fullmatch(rb'[0-9a-f]{32}', cookie)
            probe.sendto(b'CouchCHD/2 invite ' + nonce + b' ' + cookie, target)
            locate('Dismiss', timeout=12)
            screenshot(evidence, 'receiver-invited.png')
            tap('Dismiss')
            locate('Not paired')
        finally:
            adb('emu', 'redir', 'del', f'udp:{port}')


def smoke():
    with tempfile.TemporaryDirectory() as tmp:
        evidence = Path(os.environ.get('RUNNER_TEMP', tmp))
        root = Path(tmp) / 'roms'
        psx = root / 'psx'
        discs = psx / '.chd_discs'
        discs.mkdir(parents=True)
        data = {'.chd_discs/Disc.chd': b'wireless test disc\x00' * 131072,
                'Pair.m3u': b'.chd_discs/Disc.chd\n'}
        for rel, payload in data.items():
            (psx / rel).write_bytes(payload)
        sender = start_pairing()  # No ROM root, platform, profile or manifest at discovery time.
        try:
            adb('shell', 'am', 'start', '-W', '-n', 'io.github.vip3rousmango.couchchd.receiver/.ReceiverActivity')
            for label in ('Sender', 'This handheld', 'Not paired', 'Find sender'):
                locate(label)
            screenshot(evidence, 'receiver-start.png')
            invite_idle_companion(evidence)
            # The emulator's NAT doesn't forward LAN broadcasts. Use the supported
            # host-address fallback: discovery, pinning and data still go over IP.
            # Enter non-touch mode and navigate the pinned actions as on a handheld.
            adb('shell', 'input', 'keyevent', 'KEYCODE_DPAD_LEFT', 'KEYCODE_DPAD_LEFT', 'KEYCODE_DPAD_RIGHT')
            assert locate('Enter host IP')['focused'] == 'true'
            adb('shell', 'input', 'keyevent', 'KEYCODE_BUTTON_A')
            tap(kind='android.widget.EditText')
            adb('shell', 'input', 'text', sender.address.split(':')[0])
            adb('shell', 'input', 'keyevent', '66')
            tap('Find sender', resource='android:id/button1')
            locate('Codes match')
            assert sender.pair_code, 'No candidate on sender'
            locate(sender.pair_code)
            assert sender.transfer['phase'] == 'pairing' and sender.transfer['device']
            screenshot(evidence, 'receiver-pairing.png')
            sender.approve()
            assert sender.transfer['phase'] == 'approved', 'Host approval alone claimed a connection'
            tap('Codes match')
            locate('Connected · waiting for games')
            assert sender.transfer['phase'] == 'connected', 'Receiver has not confirmed the connection'
            screenshot(evidence, 'receiver-connected.png')
            sender.offer(root, ['psx/Pair.m3u'], 'android-t618', players=['pcsx-rearmed'])
            locate('Choose a destination')
            tap('Choose folder')
            tap('New folder', 'Create new folder', 'com.google.android.documentsui:id/menu_create_dir', 'com.android.documentsui:id/menu_create_dir')
            locate(kind='android.widget.EditText')
            adb('shell', 'input', 'text', 'CouchCHD-Smoke')
            tap('OK', 'Create', kind='android.widget.Button')
            tap('Use this folder')
            tap('Allow')
            tap('Receive files')
            locate('Transfer complete', timeout=90)
            for rel, payload in data.items():
                name = Path(rel).name
                paths = adb('shell', 'find', '/storage/emulated/0', '-name', 'Android', '-prune', '-o',
                            '-name', name, '-print').splitlines()
                matches = [p for p in paths if p.endswith('/CouchCHD-Smoke/' + rel)]
                assert len(matches) == 1, (name, paths)
                assert adb('shell', 'sha256sum', matches[0]).split()[0] == hashlib.sha256(payload).hexdigest()
            assert sender.transfer['phase'] == 'complete'
            print('Android 12: both peers connected before selection; SAF grant and disc + playlist SHA-256 verified.')
        finally:
            sender.close()
            screenshot(evidence, 'receiver-screen.png')


if __name__ == '__main__':
    smoke()
