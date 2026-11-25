import os
import subprocess
import sys
from unittest.mock import Mock

import pytest

from couchchd import updater


@pytest.fixture
def managed(tmp_path, monkeypatch):
    if os.name != 'posix':
        pytest.skip('The device installer requires a POSIX shell.')
    tag = 'v0.1.0-preview.4'
    launcher = tmp_path / 'install with spaces' / 'run-couchchd'
    launcher.parent.mkdir()
    launcher.write_text(f'#!/bin/sh\n# CouchCHD managed launcher\nexport COUCHCHD_RELEASE_TAG={tag}\n')
    launcher.chmod(0o755)
    installer = tmp_path / 'bundle/installer/install-device.sh'
    installer.parent.mkdir(parents=True)
    # If an unnecessary apply is attempted, make it observable.
    installer.write_text('echo "CouchCHD: unexpected installer call" >&2; exit 3\n')
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(installer.parents[1]), raising=False)
    monkeypatch.setenv('COUCHCHD_RELEASE_TAG', tag)
    monkeypatch.setenv('COUCHCHD_LAUNCHER', str(launcher))
    return launcher, installer


@pytest.mark.parametrize(('running', 'channel', 'title'), [
    ('v0.1.0-preview.4', 'v0.1.0-preview.4', 'Already up to date'),
    ('v0.1.0-preview.10', 'v0.1.0-preview.9', 'Running a newer release'),
    ('v0.1.0', 'v0.1.0-preview.99', 'Running a newer release'),
    ('v1.12.0', 'v1.2.99', 'Running a newer release'),
])
def test_no_update_never_runs_installer(managed, monkeypatch, running, channel, title):
    launcher, _ = managed
    launcher.write_text(launcher.read_text().replace('v0.1.0-preview.4', running))
    monkeypatch.setenv('COUCHCHD_RELEASE_TAG', running)
    monkeypatch.setattr(updater, '_channel_tag', lambda env: channel)
    before = launcher.read_bytes()
    actual_title, message = updater.update(lambda text: None)
    assert actual_title == title
    assert running in message and channel in message
    assert launcher.read_bytes() == before


def _supply_channel(monkeypatch, tmp_path, body, status=b'200'):
    response = tmp_path / 'channel-response'
    response.write_bytes(body + b'\n' + status)
    popen = subprocess.Popen
    def launch(args, **kwargs):
        if args[0] == 'curl':
            assert args[1] == '-q' and '--location' not in args and '-L' not in args
            assert args[args.index('--proto') + 1] == '=https'
            assert 0 < int(args[args.index('--max-time') + 1]) <= 30
            assert args[-1] == 'https://vip3rousmango.github.io/couchCHD/channel.txt'
            return popen(['/bin/cat', str(response)], **kwargs)
        return popen(args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', launch)


@pytest.mark.parametrize(('body', 'status'), [
    (b'<html>Not published</html>', b'200'),
    (b'v1.2.3\nsh -c whoami', b'200'),
    (b'https://example.org/installer.sh', b'200'),
    (b'v01.2.3', b'200'), (b'v1.0.0-preview.04', b'200'),
    (b'v1.2.3\r', b'200'), (b'v1.2.3\n\n', b'200'),
    (b'v1.\xff.0', b'200'), (b'v' + b'1' * 10000, b'200'),
    (b'v9.9.9', b'302'), (b'v9.9.9', b'404'),
])
def test_unusable_channel_preserves_launcher(managed, monkeypatch, tmp_path, body, status):
    launcher, _ = managed
    before = launcher.read_bytes()
    _supply_channel(monkeypatch, tmp_path, body, status)
    with pytest.raises((ValueError, RuntimeError), match='release channel'):
        updater.update(lambda text: None)
    assert launcher.read_bytes() == before


@pytest.mark.parametrize(('channel', 'newline'), [('v0.1.0', b'\r\n'), ('v0.1.0-preview.10', b'\n'), ('v0.2.0-preview.1', b'')])
def test_next_launch_update_uses_clean_native_environment(managed, monkeypatch, tmp_path, channel, newline):
    launcher, installer = managed
    _supply_channel(monkeypatch, tmp_path, channel.encode() + newline)
    for name in ('LD_LIBRARY_PATH', 'LD_LIBRARY_PATH_ORIG', 'LD_PRELOAD', '_PYI_APPLICATION_HOME_DIR', 'PYTHONPATH',
                 'QT_PLUGIN_PATH', 'QML2_IMPORT_PATH', 'COUCHCHD_CHDMAN', 'COUCHCHD_HOME', 'APPDIR', 'BASH_ENV'):
        monkeypatch.setenv(name, '/poison')
    monkeypatch.setenv('PATH', '/poison')
    monkeypatch.setenv('https_proxy', 'http://vpn.internal:3128')
    monkeypatch.setenv('SSL_CERT_FILE', '/network-ca.pem')
    # A bundled fixture records the actual argv and env that cross the sh
    # boundary. Emulate the atomic launcher switch, not a running binary swap.
    installer.write_text('''set -eu
printf '%s\\n' "$@" > "$4.args"
env > "$4.env"
printf 'CouchCHD: Checking the new build\\n' >&2
sed "s/^export COUCHCHD_RELEASE_TAG=.*/export COUCHCHD_RELEASE_TAG=$2/" "$4" > "$4.new"
chmod 755 "$4.new"; mv "$4.new" "$4"
''')
    progress = []
    title, message = updater.update(progress.append)
    assert title == 'Update installed'
    assert channel in message and 'still running v0.1.0-preview.4' in message
    assert 'Checking the new build' in progress
    assert launcher.with_suffix('.args').read_text().splitlines() == [
        '--apply-update', channel, 'v0.1.0-preview.4', str(launcher)]
    native_env = launcher.with_suffix('.env').read_text()
    assert '/poison' not in native_env and 'COUCHCHD_' not in native_env
    assert 'https_proxy=http://vpn.internal:3128' in native_env
    assert 'SSL_CERT_FILE=/network-ca.pem' in native_env
    assert 'PYINSTALLER_RESET_ENVIRONMENT=1' in native_env
    # A second tap from the old process never fetches or installs again.
    monkeypatch.setattr(subprocess, 'Popen', Mock(side_effect=AssertionError('unexpected second fetch/install')))
    title, message = updater.update(progress.append)
    assert title == 'Relaunch CouchCHD'
    assert 'running v0.1.0-preview.4' in message and channel in message


def test_stale_rejection_is_not_reported_as_install_success(managed, monkeypatch):
    launcher, installer = managed
    before = launcher.read_bytes()
    monkeypatch.setattr(updater, '_channel_tag', lambda env: 'v1.0.0')
    installer.write_text('echo "CouchCHD: Launcher changed; relaunch before updating." >&2; exit 1')
    with pytest.raises(RuntimeError, match='Launcher changed; relaunch'):
        updater.update(lambda text: None)
    assert launcher.read_bytes() == before


@pytest.mark.parametrize('kind', ['standalone', 'source', 'private', 'untagged', 'foreign', 'unsupported'])
def test_unmanaged_installs_get_offline_manual_guidance(managed, monkeypatch, kind):
    launcher, _ = managed
    if kind == 'standalone':
        monkeypatch.delenv('COUCHCHD_LAUNCHER')
    elif kind == 'source':
        monkeypatch.setattr(sys, 'frozen', False)
    elif kind == 'private':
        launcher.write_text('#!/bin/sh\n# CouchCHD managed launcher\nunset COUCHCHD_RELEASE_TAG\n')
    elif kind == 'untagged':
        monkeypatch.delenv('COUCHCHD_RELEASE_TAG')
    elif kind == 'foreign':
        launcher.write_text('#!/bin/sh\nexport COUCHCHD_RELEASE_TAG=v0.1.0-preview.4\n')
    else:
        monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(subprocess, 'Popen', Mock(side_effect=AssertionError('unexpected fetch/install')))
    title, message = updater.update(lambda text: None)
    assert title == 'Manual update required' and 'original installation method' in message


@pytest.mark.parametrize('replacement', ['missing', 'symlink'])
def test_unavailable_bundled_installer_fails_before_network(managed, monkeypatch, replacement):
    launcher, installer = managed
    installer.unlink()
    if replacement == 'symlink':
        installer.symlink_to(launcher)
    monkeypatch.setattr(subprocess, 'Popen', Mock(side_effect=AssertionError('unexpected fetch/install')))
    with pytest.raises(RuntimeError, match='bundled update installer'):
        updater.update(lambda text: None)
