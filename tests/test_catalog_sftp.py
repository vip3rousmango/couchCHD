"""A real, read-only LAN transport with ephemeral keys; no user's rclone config or ROMs."""
import os
from threading import Event
import shutil
import socket
import subprocess
import time

import pytest

from couchchd import cso
from couchchd.catalog import Catalog
from test_cso import _iso


@pytest.mark.skipif(not shutil.which('rclone') or not shutil.which('ssh-keygen'), reason='rclone and ssh-keygen required')
def test_existing_psp_game_can_be_pulled_over_pinned_sftp(tmp_path, monkeypatch):
    # Keep config, tokens, source and destination disjoint, like two devices.
    library = tmp_path / 'server' / 'roms'
    (library / 'psp').mkdir(parents=True)
    original = tmp_path / 'original.iso'
    original.write_bytes(_iso())
    game = library / 'psp' / 'Test [USA].cso'
    cso.convert(original, game, threads=1, cancel=Event(), on_stage=None)
    target = tmp_path / 'card'
    target.mkdir()
    config = tmp_path / 'rclone.conf'
    monkeypatch.setenv('RCLONE_CONFIG', str(config))
    for name in ('host', 'client'):
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(tmp_path / name)], check=True)
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    pub = (tmp_path / 'host.pub').read_text().split()
    known = tmp_path / 'known_hosts'
    known.write_text(f'[127.0.0.1]:{port} {pub[0]} {pub[1]}\n')
    config.write_text(f'[home]\ntype = sftp\nhost = 127.0.0.1\nport = {port}\nuser = tester\n'
                      f'key_file = {tmp_path / "client"}\nknown_hosts_file = {known}\n'
                      'shell_type = none\ndisable_hashcheck = true\n')
    os.chmod(config, 0o600)
    server = subprocess.Popen(['rclone', 'serve', 'sftp', str(library.parent), '--read-only', '--addr',
                               f'127.0.0.1:{port}', '--key', str(tmp_path / 'host'),
                               '--authorized-keys', str(tmp_path / 'client.pub')])
    try:
        until = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.25):
                    break
            except OSError:
                if server.poll() is not None or time.monotonic() >= until:
                    pytest.fail('Local read-only SFTP server did not start')
                time.sleep(.05)
        catalog = Catalog(tmp_path / 'index.sqlite3')
        source = catalog.add('Bazzite', 'home:/roms')
        result = catalog.refresh(source.id)
        assert result[0].status == 'online', result[0].error
        [entry] = catalog.entries()
        assert entry.relative == game.relative_to(library).as_posix()
        catalog.pull(source.id, entry.relative, target)
        destination = target / entry.relative
        assert destination.read_bytes() == game.read_bytes()
        destination.write_bytes(b'my existing handheld copy')
        with pytest.raises((OSError, ValueError, RuntimeError)):
            catalog.pull(source.id, entry.relative, target)
        assert destination.read_bytes() == b'my existing handheld copy'
        assert sorted(p.name for p in game.parent.iterdir()) == [game.name]
    finally:
        server.terminate()
        server.wait(timeout=10)
