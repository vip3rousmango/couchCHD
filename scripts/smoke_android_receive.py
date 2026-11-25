"""Release gate: real sender, installed APK, Android's picker and on-device SHA-256.

Run only on an empty disposable English Android emulator. No permission grants via adb.
"""
import hashlib
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from couchchd.wifi_sender import start_sender


def adb(*args):
    return subprocess.check_output(['adb', *args], timeout=20, text=True).strip()


def screen():
    adb('shell', 'uiautomator', 'dump', '/data/local/tmp/receive-window.xml')
    return ET.fromstring(adb('exec-out', 'cat', '/data/local/tmp/receive-window.xml')).findall('.//node')


def locate(*names, kind=None, timeout=32):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        nodes = screen()
        for node in nodes:
            a = node.attrib
            if kind == a['class'] or any(n.casefold() in {a.get(k, '').casefold() for k in ('text', 'resource-id', 'content-desc')} for n in names):
                return a
        time.sleep(.25)
    raise AssertionError(f'Control {names or kind} not found: {[(n.get("text"), n.get("content-desc")) for n in nodes]}')


def tap(*names, kind=None):
    node = locate(*names, kind=kind)
    left, top, right, bottom = map(int, re.findall(r'\d+', node['bounds']))
    adb('shell', 'input', 'tap', str((left + right) // 2), str((top + bottom) // 2))


def smoke():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'roms'
        psx = root / 'psx'
        discs = psx / '.chd_discs'
        discs.mkdir(parents=True)
        data = {'.chd_discs/Disc.chd': b'wireless test disc\x00' * 131072,
                'Pair.m3u': b'.chd_discs/Disc.chd\n'}
        for rel, payload in data.items():
            (psx / rel).write_bytes(payload)
        sender = start_sender(root, ['psx/Pair.m3u'], 'android-t618', players=['pcsx-rearmed'])
        try:
            adb('shell', 'am', 'start', '-W', '-n', 'io.github.vip3rousmango.couchchd.receiver/.ReceiverActivity')
            # The emulator's NAT doesn't forward LAN broadcasts. Use the supported
            # host-address fallback: discovery, pinning and data still go over IP.
            tap('Enter host IP')
            tap(kind='android.widget.EditText')
            adb('shell', 'input', 'text', sender.address.split(':')[0])
            adb('shell', 'input', 'keyevent', '66')
            tap('android:id/button1')
            locate('Codes match')
            assert sender.pair_code, 'No candidate on sender'
            locate(sender.pair_code)
            sender.approve()
            tap('Codes match')
            tap('Choose folder')
            tap('New folder', 'Create new folder', 'com.google.android.documentsui:id/menu_create_dir', 'com.android.documentsui:id/menu_create_dir')
            locate(kind='android.widget.EditText')
            adb('shell', 'input', 'text', 'CouchCHD-Smoke')
            tap('OK', 'android:id/button1')
            tap('Use this folder')
            tap('Allow', 'android:id/button1')
            tap('Receive files')
            locate('Transfer complete', timeout=90)
            for rel, payload in data.items():
                name = Path(rel).name
                paths = adb('shell', 'find', '/storage/emulated/0', '-name', 'Android', '-prune', '-o',
                            '-name', name, '-print').splitlines()
                matches = [p for p in paths if p.endswith('/CouchCHD-Smoke/' + rel)]
                assert len(matches) == 1, (name, paths)
                assert adb('shell', 'sha256sum', matches[0]).split()[0] == hashlib.sha256(payload).hexdigest()
            print('Android 12: host and receiver codes match; SAF folder grant, verified disc and playlist readback passed.')
        finally:
            sender.close()
            dest = Path(os.environ.get('RUNNER_TEMP', tmp)) / 'receiver-screen.png'
            with dest.open('wb') as shot:
                subprocess.run(['adb', 'exec-out', 'screencap', '-p'], stdout=shot, timeout=10)


if __name__ == '__main__':
    smoke()
