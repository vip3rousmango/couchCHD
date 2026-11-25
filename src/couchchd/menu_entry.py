"""Publish native frontend entries without overwriting user customizations."""
import hashlib
import os
import shlex
import stat
import tempfile
from pathlib import Path

from .ledger import install_new


_ASSETS = Path(__file__).parent / 'ui/assets'
_DESKTOP_ID = 'io.github.vip3rousmango.CouchCHD'
_PREVIEW7_SVG_SHA256 = 'df3bd1f1509f861da75dc4f13dc460a2bab14e7cfca9f7779cf7a08e22874c47'
_HOME_ROOT = Path('/home')


def host_home_path(path: Path) -> Path:
    """Map Fedora Atomic's system alias, not links inside a user's home."""
    if path.is_relative_to(_HOME_ROOT):
        physical = _HOME_ROOT.parent / 'var/home'
        try:
            if _HOME_ROOT.parent / _HOME_ROOT.readlink() == physical:
                return physical / path.relative_to(_HOME_ROOT)
        except OSError:
            pass  # Ordinary /home or unavailable; keep the original path checks.
    return path


def read_regular(path: Path) -> bytes | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise ValueError(f'Will not follow or replace {path}')
    return path.read_bytes()


def store_entry(path: Path, data: bytes, old: bytes | None, mode: int = 0o600) -> None:
    """Publish atomically; preserve permissions and detect concurrent customizations."""
    if data == old:
        return
    fd, name = tempfile.mkstemp(prefix='.couchchd-', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            if os.name == 'posix' and old is not None:
                info, staged = path.lstat(), os.fstat(f.fileno())
                if (info.st_uid, info.st_gid) != (staged.st_uid, staged.st_gid):
                    os.fchown(f.fileno(), info.st_uid, info.st_gid)
                mode = stat.S_IMODE(info.st_mode)
            if os.name == 'posix':
                os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        if old is None:
            install_new(tmp, path)
        else:
            if read_regular(path) != old:
                raise ValueError(f'{path} changed during setup; rerun the installer')
            os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _check_path(path: Path) -> None:
    if not path.is_absolute() or '..' in path.parts or path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError(f'Refusing linked or relative frontend entry: {path}')


def _publish(path: Path, content: str, mode: int) -> None:
    path = path.absolute()
    _check_path(path)
    data = content.encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        store_entry(path, data, None, mode)
    except FileExistsError:
        if (read_regular(path) != data or (mode & 0o111 and not os.access(path, os.X_OK))):
            raise FileExistsError(f'Leaving existing frontend entry unchanged: {path}') from None


def install_script(path: Path, launcher: Path, *, args: tuple[str, ...] = ()) -> None:
    cmd = shlex.join((str(launcher), *args))
    _publish(path, '#!/bin/sh\n# CouchCHD managed launcher\n'
             'if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}${QT_QPA_PLATFORM:-}" ]; then export QT_QPA_PLATFORM=eglfs; fi\n'
             f'exec {cmd}\n', 0o755)


def _desktop(launcher: Path, *, install: bool) -> tuple[Path, str]:
    if any(ord(c) < 32 for c in str(launcher)):
        raise ValueError('Desktop launcher path contains a control character')
    # Two escaping layers: quoted Exec argument, then desktop-entry string.
    arg = ''.join('\\' + c if c in '\\"`$' else c for c in str(launcher))
    arg = arg.replace('\\', '\\\\').replace('%', '%%')
    data = host_home_path(Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share'))
    path = data / 'applications' / f'{_DESKTOP_ID}.desktop'
    legacy = data / 'icons/hicolor/scalable/apps' / f'{_DESKTOP_ID}.svg'
    raster = data / 'couchchd/artwork/icon.png'
    if any(ord(c) < 32 for c in str(data)):
        raise ValueError('Desktop icon path contains a control character')
    _check_path(path)
    previous = read_regular(path)
    if previous is None and not install:
        return path, 'No registered desktop entry; no artwork changed.'

    def entry(icon_name: str, categories: str) -> bytes:
        return ('[Desktop Entry]\nType=Application\nName=CouchCHD\n'
                f'Exec="{arg}"\nIcon={icon_name}\nTerminal=false\nCategories={categories}\n').encode()

    # Do not parse/reserialize, or migrate a different installation's launcher.
    categories = 'Utility;Compression;'
    # Icon is a desktop string, not an Exec argument: literal spaces and %,
    # doubled backslashes, no shell quoting.
    raster_entry = entry(str(raster).replace('\\', '\\\\'), categories)
    legacy_entry = entry(str(legacy).replace('\\', '\\\\'), categories)
    if previous not in (None, raster_entry, legacy_entry, entry(_DESKTOP_ID, categories),
                        entry(_DESKTOP_ID, 'Game;Utility;'), entry('applications-games', 'Game;Utility;')):
        return path, 'Customized desktop entry kept; no artwork changed.'

    icon, current = raster, raster_entry
    if previous != raster_entry:
        _check_path(legacy)
        old_svg = read_regular(legacy)
        if old_svg is not None and hashlib.sha256(old_svg).hexdigest() != _PREVIEW7_SVG_SHA256:
            icon, current = legacy, legacy_entry
    added = False
    if icon == raster:
        _check_path(raster)
        if not raster.exists():
            raster.parent.mkdir(parents=True, exist_ok=True)
            store_entry(raster, (_ASSETS / 'icon.png').read_bytes(), None, 0o644)
            added = True
        elif not raster.is_file():
            raise ValueError(f'Leaving non-file desktop icon unchanged: {raster}')
    path.parent.mkdir(parents=True, exist_ok=True)
    store_entry(path, current, previous, 0o644)
    status = 'already present' if previous == current and not added else 'registered'
    return path, f'Desktop menu icon {status}: {icon}'


def install_desktop(launcher: Path) -> Path:
    return _desktop(launcher, install=True)[0]


def refresh_desktop_artwork(launcher: Path) -> str:
    return _desktop(launcher, install=False)[1]
