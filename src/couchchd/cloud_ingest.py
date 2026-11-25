"""Read-only rclone and mounted-library ingestion. Each yielded disc owns one scratch directory."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from . import hardware, scanner
from .telemetry import ItemProgress


_REMOTE_NAME = re.compile(r'[^:/\\\x00-\x1f\x7f]+:\Z')
_NETWORK = {'cifs', 'sshfs', 'nfs', '9p', 'ceph', 'afs', 'fuse.rclone', 'davfs', 'davfs2'}


def remotes() -> list[str]:
    """Return configured remote names only; never accept inline backend configs."""
    executable = shutil.which('rclone')
    if not executable:
        return []
    try:
        result = subprocess.run([executable, 'listremotes'], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=5, check=True, encoding='utf-8')
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return []
    return sorted({name for name in result.stdout.splitlines() if _REMOTE_NAME.fullmatch(name)})


def _visible(path: Path) -> bool:
    return not any(p.startswith('.') or p.casefold() == '_couchchd_backup' for p in path.parts)


def _destination(path: Path, root_system: str | None, systems: set[str]) -> tuple[str, Path] | None:
    system, start = root_system, 0
    # The first system directory under the selected root defines the library;
    # a game's title/subdirectory can itself happen to be another system name.
    if system is None:
        for i, part in enumerate(path.parts[:-1]):
            if system := scanner.SYSTEMS.get(part.casefold()):
                start = i + 1
                break
    if system not in systems or path.suffix.lower() not in scanner.FORMATS[system]:
        return None
    return system, (Path(system, *path.parts[start:])).with_suffix('.chd')


def _cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise InterruptedError('Ingest cancelled')


def _headroom(path: Path, needed: int) -> None:
    if needed > hardware.free_bytes(path):
        raise OSError(errno.ENOSPC, 'Not enough local scratch space for this disc', str(path))


@contextmanager
def _workspace(scratch: Path, protected: Path | None = None) -> Iterator[Path]:
    scratch = scratch.resolve()
    if protected is not None and scratch.is_relative_to(protected.resolve()):
        raise ValueError('Scratch must be outside the selected source directory')
    if hardware.fs_type(scratch) in _NETWORK:
        raise ValueError('Choose a local drive for scratch, not a network mount')
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == 'posix':
        meta = scratch.stat()
        if meta.st_uid not in (0, os.geteuid()) or (meta.st_mode & 0o022 and not meta.st_mode & stat.S_ISVTX):
            raise ValueError('Choose a private scratch directory owned by your account')
    with tempfile.TemporaryDirectory(prefix='disc-', dir=scratch) as tmp:
        yield Path(tmp)


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _stream_digest(src: BinaryIO, buffer: memoryview, cancel: threading.Event | None,
                   dst: BinaryIO | None = None) -> bytes:
    digest = hashlib.sha256()
    while count := src.readinto(buffer):
        _cancelled(cancel)
        chunk = buffer[:count]
        digest.update(chunk)
        if dst is not None:
            dst.write(chunk)
    return digest.digest()


@contextmanager
def stage(game: scanner.Game, scratch: Path, *, cancel: threading.Event | None = None) -> Iterator[scanner.Game]:
    """Make a private, validated local copy; never rename or write a source file."""
    source = game.source
    base = source.parent
    game.assert_unchanged()
    _cancelled(cancel)
    with _workspace(scratch, protected=base) as work:
        _headroom(work, game.size)
        buffer = memoryview(bytearray(1024 * 1024))
        for path, identity in zip(game.files, game.stamps, strict=True):
            _cancelled(cancel)
            destination = work / scanner.safe_relative_name(path.relative_to(base).as_posix())
            destination.parent.mkdir(parents=True, exist_ok=True)
            with path.open('rb') as src, destination.open('xb') as dst:
                if _identity(os.fstat(src.fileno())) != identity:
                    raise ValueError(f'Source changed before staging: {path}')
                expected = _stream_digest(src, buffer, cancel, dst)
            with destination.open('rb') as staged:
                if _stream_digest(staged, buffer, cancel) != expected:
                    raise ValueError(f'Staged content does not match the source: {path}')
        game.assert_unchanged()
        yield scanner.inspect(work / source.name, game.system, fresh=False)


@contextmanager
def _preparing(on_item, key, name, system):
    if on_item is not None:
        on_item(ItemProgress(key, name, system, 'queued'))
        on_item(ItemProgress(key, name, system, 'preparing', detail='Staging a local copy'))
    try:
        yield
    except (OSError, RuntimeError, ValueError) as exc:
        if on_item is not None:
            stage = ('cancelled' if isinstance(exc, InterruptedError) else
                     'paused' if getattr(exc, 'errno', None) == errno.ENOSPC else 'failed')
            on_item(ItemProgress(key, name, system, stage, detail=str(exc)))
        raise


def _mounted(root: Path, scratch: Path, systems: set[str], cancel: threading.Event, on_item):
    if scratch.resolve().is_relative_to(root):
        raise ValueError('Scratch must be outside the selected source directory')
    root_system = scanner.SYSTEMS.get(root.name.casefold())

    def raise_error(error: OSError) -> None:
        raise error

    for directory, dirs, names in os.walk(root, onerror=raise_error):
        _cancelled(cancel)
        directory = Path(directory)
        dirs[:] = sorted(d for d in dirs if _visible(Path(d)) and not (directory / d).is_symlink())
        for name in sorted(names):
            _cancelled(cancel)
            source = directory / name
            relative = source.relative_to(root)
            if not _visible(relative) or source.is_symlink():
                continue
            route = _destination(relative, root_system, systems)
            if route is None:
                continue
            system, output = route
            scanner.safe_relative_name(output.as_posix())
            st = source.lstat()
            if not stat.S_ISREG(st.st_mode) or time.time() - st.st_mtime < 10:
                continue
            with ExitStack() as disc:
                key = str(source)
                with _preparing(on_item, key, name, system):
                    game = scanner.inspect(source, system, fresh=True)
                    staged = disc.enter_context(stage(game, scratch, cancel=cancel))
                yield staged, output, key


def _rclone(args: list[str], cancel: threading.Event) -> bytes:
    _cancelled(cancel)
    with subprocess.Popen(['rclone', *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as proc:
        try:
            while True:
                _cancelled(cancel)
                try:
                    out, err = proc.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        if proc.returncode:
            raise RuntimeError(f'rclone {args[0]} failed: {err.decode("utf-8", "replace").strip()}')
    return out


@dataclass(frozen=True, slots=True)
class _Object:
    path: Path
    size: int
    modified: float


def _listing(remote: str, cancel: threading.Event) -> dict[str, list[_Object]]:
    data = _rclone(['lsjson', '--recursive', '--files-only', '--no-mimetype', '--', remote], cancel)
    index: dict[str, list[_Object]] = {}
    for row in json.loads(data):
        _cancelled(cancel)
        path = scanner.safe_relative_name(row['Path'])
        if row.get('IsDir') or not _visible(path):
            continue
        modified = datetime.fromisoformat(row['ModTime'].replace('Z', '+00:00'))
        if modified.tzinfo is None or not isinstance(row['Size'], int) or row['Size'] < 0:
            raise ValueError(f'Invalid size or timestamp for {path}')
        index.setdefault(path.as_posix().casefold(), []).append(_Object(path, row['Size'], modified.timestamp()))
    return index


def _cloud(remote: str, scratch: Path, systems: set[str], cancel: threading.Event, on_item):
    name, colon, prefix = remote.partition(':')
    if not colon or name + colon not in remotes():
        raise ValueError(f'Not a mounted directory or configured rclone remote: {remote}')
    if prefix.strip('/'):
        scanner.safe_relative_name(prefix.strip('/'))
    root_system = scanner.SYSTEMS.get((Path(prefix.rstrip('/')).name or name).casefold())
    index = _listing(remote, cancel)
    remote_base = remote + ('' if remote.endswith((':', '/')) else '/')

    def pull(obj: _Object, work: Path, base: Path) -> None:
        destination = work / obj.path.relative_to(base)
        destination.parent.mkdir(parents=True, exist_ok=True)
        remote_path = remote_base + obj.path.as_posix()
        _rclone(['copyto', '--', remote_path, str(destination)], cancel)
        if not destination.is_file() or destination.is_symlink() or destination.stat().st_size != obj.size:
            raise ValueError(f'Incomplete download: {obj.path}')

    for objects in index.values():
        _cancelled(cancel)
        obj = objects[0]
        route = _destination(obj.path, root_system, systems)
        if route is None or time.time() - obj.modified < 10:
            continue
        key = remote_base + obj.path.as_posix()
        system, output = route
        base = obj.path.parent
        with ExitStack() as disc:
            with _preparing(on_item, key, obj.path.name, system):
                if len(objects) != 1:
                    raise ValueError(f'Ambiguous source name: {obj.path}')
                if obj.path.suffix.lower() in ('.cue', '.gdi') and obj.size > 1024 * 1024:
                    raise ValueError(f'Descriptor exceeds 1 MiB: {obj.path}')
                work = disc.enter_context(_workspace(scratch))
                _headroom(work, obj.size)
                pull(obj, work, base)
                source = work / obj.path.name
                if source.suffix.lower() in ('.cue', '.gdi'):
                    tracks = set()
                    def candidates(ref: Path) -> list[_Object]:
                        path = (base / scanner.safe_relative_name(ref.as_posix())).as_posix().casefold()
                        return index.get(path, [])

                    for ref in scanner.references(source, available=lambda p: len(candidates(p)) == 1):
                        track, = candidates(ref)
                        if not track.size or time.time() - track.modified < 10:
                            raise ValueError(f'Track is empty or still transferring: {track.path}')
                        tracks.add(track)
                    _headroom(work, sum(track.size for track in tracks))
                    for track in sorted(tracks, key=lambda item: item.path):
                        pull(track, work, base)
                staged = scanner.inspect(source, system, fresh=False)
            yield staged, output, key


def games(remote: str, scratch: Path, systems: set[str], cancel: threading.Event,
          on_item=None) -> Iterator[tuple[scanner.Game, Path, str]]:
    """Yield a staged game, its library-relative .chd destination and original source key.

    Consume the game before advancing the iterator. Close the iterator on early
    exit to release its current disc's scratch space immediately.
    """
    _cancelled(cancel)
    if not systems:
        return
    if not remote.strip():
        raise ValueError('Choose a mounted directory or a configured rclone remote')
    mount = Path(remote).expanduser()
    if mount.is_dir():
        yield from _mounted(mount.resolve(), scratch, systems, cancel, on_item)
    else:
        yield from _cloud(remote, scratch, systems, cancel, on_item)
