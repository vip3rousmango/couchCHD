"""Run both install transports with isolated storage and a loopback SSH stand-in."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Linux device installer')


def executable(path, body):
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(0o755)


@pytest.fixture
def target(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    executable(tools / 'uname', 'case $1 in -s) echo Linux;; -m) echo x86_64;; esac')
    executable(tools / 'od', 'echo 2')  # ELF class byte of a 64-bit target shell
    executable(tools / 'ssh', '[ "$1" != -n ] || shift; shift; exec sh -c "$1"')
    if not shutil.which('sha256sum'):
        executable(tools / 'sha256sum', 'exec shasum -a 256 "$@"')
    data = tmp_path / 'installed builds'
    env = dict(os.environ, PATH=f'{tools}:{os.environ["PATH"]}', XDG_DATA_HOME=str(data), DISPLAY=':test')
    out = tmp_path / 'artifacts/x86_64'
    out.mkdir(parents=True)
    image = out / 'CouchCHD-x86_64.AppImage'
    executable(image, 'case "$*" in --appimage-extract-and-run) echo GUI;; "--appimage-extract-and-run --version") echo version;; *) exit 6;; esac')
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (out / 'SHA256SUMS-x86_64').write_text(f'{digest}  {image.name}\n')
    return tmp_path, env, data / 'couchchd', image


@pytest.mark.parametrize('push', [True, False])
def test_install_launch_and_reject_damaged_update(target, push):
    tmp, env, base, image = target
    cmd = ([sys.executable, SCRIPTS / 'send-device.py', '--builds', 'artifacts', 'device'] if push else
           ['sh', SCRIPTS / 'install-device.sh', 'host', 'artifacts'])
    def install():
        return subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True)
    for _ in range(2):
        result = install()
        assert result.returncode == 0 and 'GUI' in result.stdout, result.stderr
    assert len(list((base / 'builds').iterdir())) == 1
    launcher = base / 'run-couchchd'
    original = launcher.read_bytes()
    image.write_bytes(b'truncated update')
    result = install()
    assert result.returncode != 0 and 'SHA-256 verification' in result.stderr
    assert launcher.read_bytes() == original
    assert subprocess.check_output([launcher], env=env, text=True).strip() == 'GUI'
