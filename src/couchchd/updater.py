"""Apply public Linux releases using the installer shipped with the running bundle."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable

_CHANNEL = 'https://vip3rousmango.github.io/couchCHD/channel.txt'
_NUMBER = r'(0|[1-9][0-9]*)'
_TAG = re.compile(rf'v{_NUMBER}\.{_NUMBER}\.{_NUMBER}(?:-preview\.{_NUMBER})?')
_MAX_TAG = 64
_MANUAL = ('Manual update required', 'Start CouchCHD from its installed public release launcher to update here. '
           'For a private build, source checkout, or standalone copy, use your original installation method.')


def _version(tag: str) -> tuple[int, int, int, bool, int]:
    match = _TAG.fullmatch(tag) if len(tag) <= _MAX_TAG else None
    if not match:
        raise ValueError('Invalid CouchCHD release tag.')
    major, minor, patch, preview = match.groups()
    return int(major), int(minor), int(patch), preview is None, int(preview or 0)


def _launcher_tag(launcher: Path) -> str:
    # Read the generated public marker as data; never source the launcher. The
    # installer compares again under its lock before switching the entry point.
    if not (launcher.is_absolute() and launcher.name == 'run-couchchd' and not launcher.is_symlink()
            and launcher.is_file() and os.access(launcher, os.X_OK)):
        return ''
    with launcher.open('rb') as stream:
        lines = stream.read(8192).split(b'\n')
    prefix = b'export COUCHCHD_RELEASE_TAG='
    tags = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    if b'# CouchCHD managed launcher' not in lines or len(tags) != 1:
        return ''
    return tags[0].decode('ascii', errors='replace')


def _native_env() -> dict[str, str]:
    # Host curl/sh and the *new* PyInstaller bootloader must not load the
    # running bundle's libraries, plugins, converter or extraction directory.
    allowed = {'home', 'user', 'logname', 'xdg_config_home', 'xdg_data_home', 'xdg_state_home', 'xdg_cache_home',
               'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'curl_ca_bundle', 'ssl_cert_file', 'ssl_cert_dir'}
    env = {k: v for k, v in os.environ.items() if k.lower() in allowed}
    env.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin', PYINSTALLER_RESET_ENVIRONMENT='1')
    return env


def _channel_tag(env: dict[str, str]) -> str:
    # A deadline covers DNS, TLS and a slow/trickling body. No curlrc and no
    # redirects: this one URL supplies a tag, never a command or asset URL.
    body_limit = _MAX_TAG + 2  # allow CRLF
    args = ['curl', '-q', '--silent', '--fail', '--proto', '=https', '--max-time', '15',
            '--max-filesize', str(body_limit), '--write-out', '\n%{http_code}', _CHANNEL]
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          cwd='/', env=env) as proc:
        # Old curl only checks Content-Length for --max-filesize; also bound
        # chunked responses before bringing any of their content into Python.
        limit = body_limit + 4  # newline + HTTP status from --write-out
        data = proc.stdout.read(limit + 1)
        if len(data) > limit:
            proc.kill()
            raise ValueError('The release channel returned too much data. No update was installed.')
        code = proc.wait()
    if code == 63:  # CURLE_FILESIZE_EXCEEDED, before the body arrived
        raise ValueError('The release channel returned too much data. No update was installed.')
    status = data[-3:]
    if data[-4:-3] == b'\n' and status.isdigit() and status not in (b'000', b'200'):
        raise RuntimeError(f'The public release channel returned HTTP {status.decode()}. Try again later.')
    if code or not data.endswith(b'\n200'):
        raise RuntimeError('Could not read the public release channel. Check your connection, proxy or CA settings and try again.')
    body = data[:-4]
    body = body[:-2] if body.endswith(b'\r\n') else body.removesuffix(b'\n')
    return body.decode('ascii', errors='replace')


def _apply(installer: Path, new: str, running: str, launcher: Path, env: dict[str, str], progress: Callable[[str], None]) -> None:
    args = ['/bin/sh', str(installer), '--apply-update', new, running, str(launcher)]
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          cwd='/', env=env) as proc:
        detail = ''
        for line in iter(lambda: proc.stdout.readline(1024), b''):
            if line.startswith(b'CouchCHD: '):
                detail = line.removeprefix(b'CouchCHD: ').decode('utf-8', errors='replace').strip()
                progress(detail)
        code = proc.wait()
    if code:
        raise RuntimeError(f'Could not install {new}. {detail or f"Installer exited with code {code}."}')


def update(progress: Callable[[str], None]) -> tuple[str, str]:
    """Check and, only when newer, install for next launch. Run off the UI thread."""
    bundle = getattr(sys, '_MEIPASS', None)
    running = os.getenv('COUCHCHD_RELEASE_TAG', '')
    path = os.getenv('COUCHCHD_LAUNCHER', '')
    if sys.platform != 'linux' or not getattr(sys, 'frozen', False) or not bundle or not running or not path:
        return _MANUAL
    launcher = Path(path)
    installed = _launcher_tag(launcher)
    try:
        current = _version(running)
        _version(installed)
    except ValueError:
        return _MANUAL
    if installed != running:
        return 'Relaunch CouchCHD', (f'You are running {running}. The installed launcher now points to {installed}. '
                                    'Close and reopen CouchCHD when ready.')
    installer = Path(bundle) / 'installer/install-device.sh'
    if not installer.is_absolute() or not installer.is_file() or installer.is_symlink():
        raise RuntimeError('The bundled update installer is missing or damaged. Reinstall using your original installation method.')
    env = _native_env()
    progress('Checking the public release channel…')
    try:
        latest = _channel_tag(env)
    except FileNotFoundError:
        raise RuntimeError('The system curl command is missing. Install curl with HTTPS support or use your original installation method.') from None
    try:
        offered = _version(latest)
    except ValueError:
        raise ValueError('The release channel did not contain a valid version. No update was installed.') from None
    if offered == current:
        return 'Already up to date', f'You are running {running}, the current public release.'
    if offered < current:
        return 'Running a newer release', f'You are running {running}. The public channel offers {latest}; no downgrade was made.'
    progress(f'Downloading and verifying {latest}…')
    _apply(installer, latest, running, launcher, env, progress)
    return 'Update installed', (f'{latest} is ready for your next launch. This session is still running {running}. '
                                'Close and reopen CouchCHD when ready.')
