"""Publish native frontend entries without overwriting user customizations."""
import os
import shlex
import stat
import tempfile
from pathlib import Path

from .ledger import install_new


_ASSETS = Path(__file__).parent / 'ui/assets'
_DESKTOP_ID = 'io.github.vip3rousmango.CouchCHD'


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
            if old is not None:
                info, staged = path.lstat(), os.fstat(f.fileno())
                if (info.st_uid, info.st_gid) != (staged.st_uid, staged.st_gid):
                    os.fchown(f.fileno(), info.st_uid, info.st_gid)
                mode = stat.S_IMODE(info.st_mode)
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
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    path = data / 'applications' / f'{_DESKTOP_ID}.desktop'
    icon = data / 'icons/hicolor/scalable/apps' / f'{_DESKTOP_ID}.svg'
    _check_path(path)
    previous = read_regular(path)
    if previous is None and not install:
        return path, 'No registered desktop entry; no artwork changed.'

    def entry(icon_name: str) -> bytes:
        return ('[Desktop Entry]\nType=Application\nName=CouchCHD\n'
                f'Exec="{arg}"\nIcon={icon_name}\nTerminal=false\nCategories=Game;Utility;\n').encode()

    # Do not parse/reserialize, or migrate a different installation's launcher.
    legacy, current = entry('applications-games'), entry(_DESKTOP_ID)
    if previous not in (None, legacy, current):
        return path, 'Customized desktop entry kept; no artwork changed.'
    _check_path(icon)
    added = not icon.exists()
    if added:
        icon.parent.mkdir(parents=True, exist_ok=True)
        store_entry(icon, (_ASSETS / 'icon.svg').read_bytes(), None, 0o644)
    elif not icon.is_file():
        raise ValueError(f'Leaving existing desktop icon unchanged: {icon}')
    path.parent.mkdir(parents=True, exist_ok=True)
    store_entry(path, current, previous, 0o644)
    if previous == current and not added:
        return path, 'Desktop menu icon already present.'
    return path, 'Desktop menu icon installed.' if added else 'Desktop menu now uses the existing icon.'


def install_desktop(launcher: Path) -> Path:
    return _desktop(launcher, install=True)[0]


def refresh_desktop_artwork(launcher: Path) -> str:
    return _desktop(launcher, install=False)[1]
