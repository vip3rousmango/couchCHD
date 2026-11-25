#!/usr/bin/env python3
"""Install a local native build on a test device without publishing it."""
import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path


def main():
    scripts = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('device', help='SSH destination, e.g. root@recalbox.local')
    parser.add_argument('--builds', type=Path, default=scripts.parent / 'dist/release',
                        help='parent of x86_64/ and aarch64/ build directories')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.@-]*', args.device):
        parser.error('Use a hostname or user@hostname (SSH aliases supported).')
    ssh = ['ssh', args.device]
    installer = (scripts / 'install-device.sh').read_text()
    selected = subprocess.check_output([*ssh, 'sh -s -- --probe-asset'], input=installer, text=True).split()
    assets = {'x86_64': {'CouchCHD-x86_64.AppImage'},
              'aarch64': {'CouchCHD-Recalbox-aarch64.tar.gz', 'CouchCHD-Recalbox-aarch64.squashfs'}}
    if len(selected) != 2 or selected[1] not in assets.get(selected[0], ()):
        raise ValueError(f'Unexpected target release asset: {selected!r}')
    arch, filename = selected
    directory = args.builds / arch
    asset = directory / filename
    entries = [line.split() for line in (directory / f'SHA256SUMS-{arch}').read_text().splitlines()]
    matches = [row[0] for row in entries if len(row) == 2 and row[1] == asset.name]
    if len(matches) != 1 or not re.fullmatch('[0-9a-f]{64}', matches[0]):
        raise ValueError(f'No unique SHA-256 checksum for {asset}')
    command = shlex.join(['sh', '-c', installer, 'install-device', '--receive', arch, matches[0]])
    with asset.open('rb') as payload:
        subprocess.run([*ssh, command], stdin=payload, check=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(f'CouchCHD setup: {error}')
