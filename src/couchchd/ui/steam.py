"""Add a single non-Steam shortcut without re-encoding other shortcuts."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
import zlib

import vdf

ASSETS = Path(__file__).with_name('assets')


def _steam_dir() -> Path:
    home = Path.home()
    candidates = [home / '.local/share/Steam', home / '.steam/steam',
                  home / '.var/app/com.valvesoftware.Steam/.local/share/Steam',
                  home / 'Library/Application Support/Steam']
    if os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
            candidates.insert(0, Path(winreg.QueryValueEx(key, 'SteamPath')[0]))
    for path in candidates:
        if (path / 'userdata').is_dir():
            return path
    raise RuntimeError('Steam user data was not found. Sign into Steam on this device first.')


def _offline(steam: Path) -> None:
    if os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam\ActiveProcess') as key:
                active = bool(winreg.QueryValueEx(key, 'ActiveUser')[0])
        except FileNotFoundError:
            active = False
    else:
        active = False
        for file in (steam / 'steam.pid', Path.home() / '.steam/steam.pid'):
            try:
                pid = int(file.read_text().strip())
                if pid > 0:
                    os.kill(pid, 0)
                    active = True
            except (FileNotFoundError, ProcessLookupError, ValueError):
                continue
    if active or os.environ.get('SteamAppId'):
        raise RuntimeError('Exit Steam completely, then launch CouchCHD outside Gaming Mode and try again. Steam can overwrite shortcuts while running.')


def _config(steam: Path) -> Path:
    profiles = [p for p in (steam / 'userdata').iterdir() if p.name.isdecimal() and (p / 'config').is_dir()]
    if len(profiles) == 1:
        return profiles[0] / 'config'
    logins = steam / 'config/loginusers.vdf'
    if logins.exists():
        with logins.open(encoding='utf-8-sig') as stream:
            users = vdf.load(stream).get('users', {})
        for steam_id, values in users.items():
            if values.get('MostRecent') == '1' and steam_id.isdecimal():
                account = str(int(steam_id) & 0xffffffff)
                for profile in profiles:
                    if profile.name == account:
                        return profile / 'config'
    raise RuntimeError('Cannot identify the active Steam account. Sign in with the intended account, then exit Steam.')


def _launcher() -> Path:
    command = os.environ.get('APPIMAGE') or (sys.executable if getattr(sys, 'frozen', False) else shutil.which('couchchd'))
    if not command:
        # Virtual environments need not be active when their console script runs.
        script = Path(sys.argv[0]).absolute()
        if script.stem.lower() == 'couchchd':
            command = str(script)
    if not command or not Path(command).is_file():
        raise RuntimeError('Install the couchchd launcher or open the AppImage before adding it to Steam.')
    return Path(command).absolute()


def _shortcut(original: bytes | None, launcher: Path) -> tuple[bytes, int]:
    exe = f'"{launcher}"'
    appid = zlib.crc32((exe + 'CouchCHD').encode()) | 0x80000000
    if original is None:
        original = vdf.binary_dumps({'shortcuts': {}})
    try:
        tree = vdf.binary_loads(original)
        entries = tree['shortcuts']
        if (set(tree) != {'shortcuts'} or not original.startswith(b'\0shortcuts\0')
                or not original.endswith(b'\x08\x08') or not isinstance(entries, dict)
                or not all(k.isdecimal() and isinstance(v, dict) for k, v in entries.items())):
            raise ValueError('Unexpected shortcut structure')
        for fields in entries.values():
            fields = {k.casefold(): v for k, v in fields.items()}
            if fields.get('appname') == 'CouchCHD' and fields.get('exe') == exe:
                return original, fields.get('appid', appid) & 0xffffffff
            if (fields.get('appid', 0) & 0xffffffff) == appid:
                raise RuntimeError('A different Steam shortcut uses this ID. No shortcuts were changed.')
    except (SyntaxError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError('Cannot read shortcuts.vdf safely. Your Steam shortcuts have not been changed.') from exc
    index = str(1 + max((int(k) for k in entries), default=-1))
    item = {'appid': appid - (1 << 32), 'AppName': 'CouchCHD', 'Exe': exe,
            'StartDir': f'"{launcher.parent}"', 'icon': '', 'ShortcutPath': '', 'LaunchOptions': '',
            'IsHidden': 0, 'AllowDesktopConfig': 1, 'AllowOverlay': 1, 'OpenVR': 0, 'tags': {}}
    # Drop the new document terminator; retain every existing entry byte for byte.
    blob = original[:-2] + vdf.binary_dumps({index: item})[:-1] + b'\x08\x08'
    if vdf.binary_loads(blob)['shortcuts'][index]['Exe'] != exe:
        raise RuntimeError('Invalid shortcut; no changes made.')
    return blob, appid


def _art(svg: Path) -> bytes:
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise RuntimeError(f'Cannot load bundled art: {svg.name}')
    image = QImage(renderer.defaultSize(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, 'PNG'):
        raise RuntimeError('Steam artwork could not be encoded.')
    return bytes(data)


def _create(path: Path, data: bytes) -> None:
    # Publish complete artwork/backup files without clobbering existing user data.
    try:
        stream = path.open('xb')
    except FileExistsError:
        return
    try:
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def register() -> str:
    steam = _steam_dir()
    _offline(steam)
    config = _config(steam)
    file = config / 'shortcuts.vdf'
    original = file.read_bytes() if file.exists() else None
    blob, appid = _shortcut(original, _launcher())
    grid = config / 'grid'
    grid.mkdir(exist_ok=True)
    for suffix, name in [('p', 'grid'), ('_hero', 'hero')]:
        dest = grid / f'{appid}{suffix}.png'
        if not any(grid.glob(f'{appid}{suffix}.*')):
            _create(dest, _art(ASSETS / f'{name}.svg'))
    templates = steam / 'controller_base/templates'
    templates.mkdir(parents=True, exist_ok=True)
    name = 'couchchd_joystick_trackpad.vdf'
    _create(templates / name, (ASSETS / name).read_bytes())
    if blob != original:
        temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=config, prefix='.couchchd-', delete=False) as out:
                temp = Path(out.name)
                out.write(blob)
                out.flush()
                os.fsync(out.fileno())
            _offline(steam)
            if (file.read_bytes() if file.exists() else None) != original:
                raise RuntimeError('Steam shortcuts changed during registration. Try again with Steam closed.')
            if original is not None:
                _create(config / 'shortcuts.vdf.couchchd-backup', original)
            os.replace(temp, file)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    return 'Open Steam and find CouchCHD in Library → Non-Steam. Use the Gamepad with Joystick Trackpad layout.'
