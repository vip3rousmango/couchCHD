"""Add a single non-Steam shortcut without re-encoding other shortcuts."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import zlib

import vdf

from ..ledger import install_new
from ..menu_entry import host_home_path
from ..stock_art import upgrade_stock

ASSETS = Path(__file__).with_name('assets')


class SteamRunning(RuntimeError):
    """Registration may be retried once the client exits."""


def _steam_dir() -> Path:
    home = host_home_path(Path.home())
    candidates = [home / '.local/share/Steam', home / '.steam/steam',
                  home / 'Library/Application Support/Steam']
    flatpak = home / '.var/app/com.valvesoftware.Steam'
    if os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
            candidates.insert(0, Path(winreg.QueryValueEx(key, 'SteamPath')[0]))
    for path in candidates:
        if (path / 'userdata').is_dir() and not path.resolve().is_relative_to(flatpak.resolve()):
            return path
    if (flatpak / '.local/share/Steam/userdata').is_dir():
        raise RuntimeError('Only Flatpak Steam was found. Automatic registration targets native Steam; the sandbox may not be able to launch host programs.')
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
                if pid <= 0:
                    raise ValueError('Expected a positive Steam PID')
                os.kill(pid, 0)
                active = True
                break
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (ValueError, OverflowError) as exc:
                raise RuntimeError(f'Cannot determine whether Steam is running: invalid {file}. No shortcuts were changed.') from exc
    if active or os.environ.get('SteamAppId') or os.environ.get('SteamGameId'):
        raise SteamRunning('Exit Steam completely, then launch CouchCHD outside Gaming Mode and try again. Steam can overwrite shortcuts while running.')


def _config(steam: Path) -> Path:
    profiles = [p for p in (steam / 'userdata').iterdir() if p.name.isdecimal() and (p / 'config').is_dir()]
    if len(profiles) == 1:
        return profiles[0] / 'config'
    logins = steam / 'config/loginusers.vdf'
    if logins.exists():
        try:
            with logins.open(encoding='utf-8-sig') as stream:
                users = vdf.load(stream).get('users', {})
            recent = [steam_id for steam_id, values in users.items()
                      if values.get('MostRecent') == '1' and steam_id.isdecimal()]
        except (AttributeError, SyntaxError, ValueError) as exc:
            raise RuntimeError('Cannot read the active Steam account safely.') from exc
        if len(recent) == 1:
            account = str(int(recent[0]) & 0xffffffff)
            for profile in profiles:
                if profile.name == account:
                    return profile / 'config'
    raise RuntimeError('Cannot identify the active Steam account. Sign in with the intended account, then exit Steam.')


def _launcher() -> Path:
    command = (os.environ.get('COUCHCHD_LAUNCHER') or os.environ.get('APPIMAGE')
               or (sys.executable if getattr(sys, 'frozen', False) else shutil.which('couchchd')))
    if not command:
        # Virtual environments need not be active when their console script runs.
        script = Path(sys.argv[0]).absolute()
        if script.stem.lower() == 'couchchd':
            command = str(script)
    if not command or not Path(command).is_file():
        raise RuntimeError('Install the couchchd launcher or open the AppImage before adding it to Steam.')
    return Path(command).absolute()


# Binary KeyValues has no entry lengths. Walk the typed fields to get exact
# byte spans; decoding/reserializing even one entry can damage opaque metadata.
def _fields(data: bytes, pos: int):
    depth = 0
    while True:
        kind = data[pos]
        pos += 1
        if kind == 8:
            if depth == 0:
                return
            depth -= 1
        else:
            key_end = data.index(b'\0', pos)
            if depth == 0:
                key, field_type, start = data[pos:key_end].lower(), kind, key_end + 1
            pos = key_end + 1
            if kind == 0:
                depth += 1
            elif kind == 1:
                pos = data.index(b'\0', pos) + 1
            elif kind in (2, 3, 4, 6):
                pos += 4
            elif kind in (7, 10):
                pos += 8
            elif kind == 5:
                end = data.index(b'\0\0', pos)
                while (end - pos) % 2:
                    end = data.index(b'\0\0', end + 1)
                pos = end + 2
            else:
                raise ValueError('Unknown binary VDF type')
        if depth == 0:
            yield key, field_type, start, pos


_DESKTOP_FILES = {'couchchd.desktop', 'io.github.vip3rousmango.couchchd.desktop'}
_APPIMAGES = {'couchchd.appimage', 'couchchd-x86_64.appimage'}
_MANUAL_NAMES = {'couchchd', 'run-couchchd', 'couchchd-x86_64', 'io.github.vip3rousmango.couchchd',
                 *_DESKTOP_FILES, *_APPIMAGES}


def _known_target(target: Path, launcher: Path, named: bool) -> bool:
    if not target.is_absolute():
        return False
    target = host_home_path(target)
    name = target.name.casefold()
    if target == launcher:
        return True
    data = host_home_path(Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share'))
    if target == data / 'applications/io.github.vip3rousmango.CouchCHD.desktop':
        return True
    if ((name == 'run' or name in _APPIMAGES) and target.parent.parent == launcher.parent / 'builds'
            and re.fullmatch('[0-9a-f]{64}', target.parent.name)):
        return True
    # Outside the installed tree, Steam's name must corroborate the target.
    # A separately named development build is not ours to replace.
    if not named:
        return False
    if name in _DESKTOP_FILES or name in _APPIMAGES or name in ('couchchd', 'couchchd.exe', 'run-couchchd'):
        return True
    # AppImage extraction directories change on every launch. AppRun alone
    # identifies no application; only accept it from a CouchCHD-named mount.
    return name == 'apprun' and target.parent.name.casefold().startswith('.mount_couchc')


def _locate(original: bytes, launcher: Path):
    """Read only the fields we own; preserve opaque, non-UTF8 and nested data."""
    header = b'\0shortcuts\0'
    indices: set[int] = set()
    ids: list[int] = []
    matches = []
    try:
        if not original.startswith(header):
            raise ValueError('Missing shortcuts')
        end = len(header)
        for key, kind, start, end in _fields(original, end):
            if kind != 0 or not key.isdigit() or int(key) in indices:
                raise ValueError('Invalid shortcut index')
            indices.add(int(key))
            spans = {}
            values = {}
            for field, kind, begin, stop in _fields(original, start):
                if field not in (b'appid', b'appname', b'exe', b'startdir', b'launchoptions', b'icon'):
                    continue
                if field in spans or kind != (2 if field == b'appid' else 1):
                    raise ValueError('Duplicate or invalid shortcut field')
                spans[field] = (begin, stop)
                values[field] = original[begin:stop] if field == b'appid' else original[begin:stop - 1]
            old_id = int.from_bytes(values[b'appid'], 'little') if b'appid' in values else None
            if old_id is not None:
                ids.append(old_id)
            raw = os.fsdecode(values.get(b'exe', b'')).strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
                raw = raw[1:-1]
            target = Path(raw)
            named = os.fsdecode(values.get(b'appname', b'')).casefold() in _MANUAL_NAMES
            if _known_target(target, launcher, named):
                matches.append((old_id, target, spans, values, end - 1))
            elif named:
                raise RuntimeError('A shortcut named CouchCHD has an unrecognized target. Review it in Steam; no shortcuts were changed.')
        if original[end:] != b'\x08\x08':
            raise ValueError('Unexpected end of shortcuts')
    except (IndexError, ValueError) as exc:
        raise RuntimeError('Cannot read shortcuts.vdf safely. Your Steam shortcuts have not been changed.') from exc
    if len(matches) > 1:
        raise RuntimeError('Multiple CouchCHD shortcuts were found. Choose which to keep in Steam; no shortcuts were changed.')
    if matches:
        old_id = matches[0][0]
        if old_id is None or ids.count(old_id) != 1:
            raise RuntimeError('The CouchCHD shortcut has no unique app ID. No shortcuts were changed.')
    return (matches[0] if matches else None), ids, str(1 + max(indices, default=-1))


def _shortcut(original: bytes | None, launcher: Path, grid: Path) -> tuple[bytes, int, bytes]:
    if not launcher.is_absolute() or any(c == '"' or ord(c) < 32 for c in str(launcher)):
        raise ValueError('Steam needs an absolute launcher path without quotes or control characters.')
    exe = f'"{launcher}"'
    if original is None:
        original = vdf.binary_dumps({'shortcuts': {}})
    match, ids, index = _locate(original, launcher)
    if match:
        old_id, target, spans, values, entry_end = match
        replacements = {b'exe': exe.encode(), b'startdir': f'"{launcher.parent}"'.encode()}
        if not values.get(b'icon'):
            icon = _icon_path(grid, old_id)
            if icon is not None:
                replacements[b'icon'] = os.fsencode(icon)
        options = values.get(b'launchoptions', b'').strip()
        if (options == b'--appimage-extract-and-run' and target.name.casefold() in _APPIMAGES
                and launcher.name.casefold() not in _APPIMAGES):
            replacements[b'launchoptions'] = b''
        elif options and target != launcher:
            raise RuntimeError('CouchCHD has custom Steam launch options. Review and clear them before repairing the target; no shortcuts were changed.')
        edits = []
        for field, value in replacements.items():
            if values.get(field) == value:
                continue
            if field in spans:
                begin, stop = spans[field]
                edits.append((begin, stop, value + b'\0'))
            else:
                label = {b'startdir': b'StartDir', b'icon': b'icon'}[field]
                edits.append((entry_end, entry_end, b'\x01' + label + b'\0' + value + b'\0'))
        for begin, stop, value in sorted(edits, reverse=True):
            original = original[:begin] + value + original[stop:]
        return original, old_id, values.get(b'icon', b'')
    appid = zlib.crc32((exe + 'CouchCHD').encode()) | 0x80000000
    if appid in ids:
        raise RuntimeError('A different Steam shortcut uses this ID. No shortcuts were changed.')
    icon = _icon_path(grid, appid)
    item = {'appid': appid - (1 << 32), 'AppName': 'CouchCHD', 'Exe': exe,
            'StartDir': f'"{launcher.parent}"', 'icon': str(icon) if icon is not None else '',
            'ShortcutPath': '', 'LaunchOptions': '',
            'IsHidden': 0, 'AllowDesktopConfig': 1, 'AllowOverlay': 1, 'OpenVR': 0, 'tags': {}}
    # Drop the new document terminator; retain every existing entry byte for byte.
    blob = original[:-2] + vdf.binary_dumps({index: item})[:-1] + b'\x08\x08'
    return blob, appid, b''


_SLOTS = (('p', 'grid', 'portrait'), ('', 'landscape', 'landscape'),
          ('_hero', 'hero', 'hero'), ('_logo', 'logo', 'logo'), ('_icon', 'icon', 'icon'))
_IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.webp', '.ico', '.tga', '.gif', '.bmp', '.svg', '.apng'}


def _art_files(grid: Path, stems: set[str]) -> dict[str, list[Path]]:
    # Steam's <appid>.json stores logo placement, not landscape artwork.
    files: dict[str, list[Path]] = {}
    if grid.is_dir():
        with os.scandir(grid) as entries:
            for entry in entries:
                name, _, ext = entry.name.rpartition('.')
                if name.casefold() in stems and '.' + ext.casefold() in _IMAGE_TYPES:
                    files.setdefault(name.casefold(), []).append(grid / entry.name)
    return files


def _icon_path(grid: Path, appid: int) -> Path | None:
    stem = f'{appid}_icon'
    matches = _art_files(grid, {stem}).get(stem, [])
    if not matches:
        return grid / (stem + '.png')
    # Never pick between competing custom icons or point Steam at a link.
    return matches[0] if len(matches) == 1 and not matches[0].is_symlink() and matches[0].is_file() else None


def _create(path: Path, data: bytes) -> bool:
    # Readers (including a running Steam) never observe a partial PNG.
    if os.path.lexists(path):
        return False
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.couchchd-', delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            try:
                install_new(staged, path)
            except FileExistsError:
                return False
            return True
        finally:
            staged.unlink(missing_ok=True)


def _steam_paths(steam: Path, config: Path) -> tuple[Path, Path]:
    file, grid = config / 'shortcuts.vdf', config / 'grid'
    if any(p.is_symlink() for p in (steam / 'userdata', config.parent, config, file, grid)):
        raise RuntimeError('Leaving redirected Steam profile data unchanged.')
    return file, grid


def _fill_grid(grid: Path, appid: int, *, selected_icon: bytes) -> list[str]:
    grid.mkdir(exist_ok=True)
    added = []
    occupied = _art_files(grid, {f'{appid}{suffix}' for suffix, _, _ in _SLOTS})
    own_icon = os.fsencode(grid / f'{appid}_icon.png')
    for suffix, asset, label in _SLOTS:
        stem = f'{appid}{suffix}'
        if suffix == '_icon' and selected_icon not in (b'', own_icon):
            continue
        dest = grid / f'{stem}.png'
        image = ASSETS / f'{asset}.png'
        existing = occupied.get(stem)
        if existing:
            if existing == [dest] and upgrade_stock(dest, image):
                added.append(label)
        elif _create(dest, image.read_bytes()):
            added.append(label)
    return added


def refresh_artwork(launcher: Path) -> str:
    """Fill missing art for an existing managed shortcut without editing Steam's database."""
    steam = _steam_dir()
    config = _config(steam)
    file, grid = _steam_paths(steam, config)
    if not file.is_file():
        raise RuntimeError('No Steam shortcut file was found. Use Add to Steam with Steam closed first.')
    original = file.read_bytes()
    match, _, _ = _locate(original, launcher)
    if not match:
        raise RuntimeError('No existing CouchCHD Steam shortcut was found. Use Add to Steam with Steam closed first.')
    appid, target, _, values, _ = match
    if not _known_target(target, launcher, False):
        raise RuntimeError('The Steam shortcut targets a different installation. No artwork changed.')
    game_id = str((appid << 32) | 0x02000000)
    # Steam can report zero for the store app ID of a non-Steam game.
    if (os.environ.get('SteamAppId', '') not in ('', '0', str(appid)) or
            os.environ.get('SteamGameId', '') not in ('', str(appid), game_id)):
        raise RuntimeError('The running Steam game ID does not match this shortcut. No artwork changed.')
    if _config(steam) != config or file.read_bytes() != original:
        raise RuntimeError('The active Steam profile or shortcuts changed. Try again.')
    selected_icon = values.get(b'icon', b'')
    added = _fill_grid(grid, appid, selected_icon=selected_icon)
    result = ('Refreshed Steam artwork: ' + ', '.join(added) + '.' if added
              else 'No missing Steam artwork files; existing choices kept.')
    if not selected_icon:
        result += ' To attach the shortcut icon, exit Steam and choose Add to Steam in CouchCHD Settings.'
    return result


def register(launcher: Path | None = None) -> str:
    steam = _steam_dir()
    config = _config(steam)
    _offline(steam)
    launcher = _launcher() if launcher is None else launcher.absolute()
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise RuntimeError(f'The CouchCHD launcher is not executable: {launcher}')
    file, grid = _steam_paths(steam, config)
    original = file.read_bytes() if file.exists() else None
    blob, appid, selected_icon = _shortcut(original, launcher, grid)
    _fill_grid(grid, appid, selected_icon=selected_icon)
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
