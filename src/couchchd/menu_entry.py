"""Publish native frontend entries without overwriting user customizations."""
import os
import shlex
import tempfile
from pathlib import Path

from .ledger import install_new


def _publish(path: Path, content: str, mode: int) -> None:
    path = path.absolute()
    if '..' in path.parts or path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError(f'Refusing linked frontend entry: {path}')
    data = content.encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='.couchchd-', dir=path.parent, delete=False) as handle:
        staged = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
            try:
                install_new(staged, path)
            except FileExistsError:
                if (path.is_symlink() or not path.is_file() or path.stat().st_size != len(data)
                        or path.read_bytes() != data or (mode & 0o111 and not os.access(path, os.X_OK))):
                    raise FileExistsError(f'Leaving existing frontend entry unchanged: {path}') from None
        finally:
            staged.unlink(missing_ok=True)


def install_script(path: Path, launcher: Path, *, args: tuple[str, ...] = ()) -> None:
    cmd = shlex.join((str(launcher), *args))
    _publish(path, '#!/bin/sh\n# CouchCHD managed launcher\n'
             'if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}${QT_QPA_PLATFORM:-}" ]; then export QT_QPA_PLATFORM=eglfs; fi\n'
             f'exec {cmd}\n', 0o755)


def install_desktop(launcher: Path) -> Path:
    if any(ord(c) < 32 for c in str(launcher)):
        raise ValueError('Desktop launcher path contains a control character')
    # Two escaping layers: quoted Exec argument, then desktop-entry string.
    arg = ''.join('\\' + c if c in '\\"`$' else c for c in str(launcher))
    arg = arg.replace('\\', '\\\\').replace('%', '%%')
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    path = data / 'applications/io.github.vip3rousmango.CouchCHD.desktop'
    _publish(path, '[Desktop Entry]\nType=Application\nName=CouchCHD\n'
             f'Exec="{arg}"\nIcon=applications-games\nTerminal=false\nCategories=Game;Utility;\n', 0o644)
    return path
