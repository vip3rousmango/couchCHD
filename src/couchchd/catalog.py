"""Named, explicitly refreshed inventories. The cache is a directory, not proof of content identity."""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import catalog_transport, cloud_ingest, scanner


@dataclass(frozen=True, slots=True)
class Source:
    id: str
    name: str
    location: str
    status: str
    last_seen: float | None
    error: str


@dataclass(frozen=True, slots=True)
class Entry:
    relative: str
    source_id: str
    source_name: str
    status: str
    last_seen: float
    size: int


_SOURCE_FIELDS = 'id, name, location, status, last_seen, error'
_RECALBOX_SYSTEM = Path('/recalbox/share/system')
_MOUNTINFO = Path('/proc/self/mountinfo')
_ESCAPE = re.compile(rb'\\([0-7]{3})')
_SCHEMA = '''
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS source (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
    location TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', last_seen REAL,
    error TEXT NOT NULL DEFAULT '', mountpoint TEXT, generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entry (
    source_id TEXT NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    relative TEXT NOT NULL, size INTEGER NOT NULL, PRIMARY KEY (source_id, relative)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS entry_path ON entry(relative);
COMMIT;
'''


def _default_path() -> Path:
    if _RECALBOX_SYSTEM.is_dir():
        directory = _RECALBOX_SYSTEM
    else:
        xdg = Path(os.environ.get('XDG_DATA_HOME', ''))
        directory = xdg if xdg.is_absolute() else Path.home() / '.local/share'
    return directory / 'couchchd' / 'catalog.sqlite3'


def _location(value: str | Path) -> str:
    text = os.fspath(value)
    if not text or not text.isprintable():
        raise ValueError('Choose an absolute folder or a configured rclone remote')
    local = Path(text).expanduser()
    if local.is_absolute():
        if '..' in local.parts:
            raise ValueError('Choose a folder without parent traversal')
        return str(local)
    name, base = catalog_transport.parse_remote(text)
    if name + ':' not in cloud_ingest.remotes():
        raise ValueError('Choose a named remote from your rclone configuration')
    return f'{name}:{base}'


def _mountpoint(path: Path) -> Path:
    """Include same-device bind mounts on Linux, which os.path.ismount cannot distinguish."""
    path = path.resolve(strict=True)
    try:
        with _MOUNTINFO.open('rb') as mounts:
            deepest = Path(path.anchor)
            for row in mounts:
                field = row.split(b' ', 5)[4]
                mount = Path(os.fsdecode(_ESCAPE.sub(lambda m: bytes([int(m[1], 8)]), field)))
                if len(mount.parts) > len(deepest.parts) and path.is_relative_to(mount):
                    deepest = mount
            return deepest
    except FileNotFoundError:
        while path != path.parent and not path.is_mount():
            path = path.parent
        return path


def _mounted_state(location: str, previous: str | None) -> tuple[str, int, int] | None:
    root = Path(location)
    if not root.is_absolute():
        return None
    meta = root.stat()
    mount = _mountpoint(root)
    if previous and not mount.is_relative_to(Path(previous)):
        raise OSError(f'Expected mount is not attached: {previous}')
    return str(mount), meta.st_dev, meta.st_ino


class Catalog:
    """Each operation owns its connection; slow source reads do not hold a database lock."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else _default_path()
        self.path = self.path.expanduser().absolute()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # SQLite's default creation permissions depend on umask. Keep location names private.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        with self._connection() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            with db:
                yield db

    def add(self, name: str, location: str | Path) -> Source:
        """Remember a folder without scanning it. Re-adding the same name/location is idempotent."""
        name = name.strip()
        if not name or len(name) > 128 or not name.isprintable():
            raise ValueError('Use a printable library name of 1–128 characters')
        location = _location(location)
        root = Path(location)
        if root.is_absolute() and self.path.resolve().is_relative_to(root.resolve()):
            raise ValueError('The catalog database must be outside the named source folder')
        mount = str(_mountpoint(root)) if root.is_absolute() and root.is_dir() else None
        key = name.casefold()
        with self._connection() as db:
            db.execute('INSERT INTO source (id,name,name_key,location,mountpoint) VALUES (?,?,?,?,?) '
                       'ON CONFLICT(name_key) DO NOTHING', (uuid.uuid4().hex, name, key, location, mount))
            source = Source(*db.execute(f'SELECT {_SOURCE_FIELDS} FROM source WHERE name_key=?', (key,)).fetchone())
            if source.location != location:
                raise ValueError(f'The library name {name!r} is already used for another location')
            return source

    def remove(self, source_id: str) -> None:
        """Forget this inventory. No filesystem or remote changes are made."""
        with self._connection() as db:
            if not db.execute('DELETE FROM source WHERE id=?', (source_id,)).rowcount:
                raise ValueError('Unknown catalog source')

    def sources(self) -> list[Source]:
        with self._connection() as db:
            return [Source(*row) for row in db.execute(f'SELECT {_SOURCE_FIELDS} FROM source ORDER BY name_key')]

    def _refresh_one(self, source_id: str, *, required: bool) -> None:
        # A later refresh wins, even when an earlier network request completes out of order.
        with self._connection() as db:
            if not db.execute('UPDATE source SET generation=generation+1 WHERE id=?', (source_id,)).rowcount:
                if required:
                    raise ValueError('Unknown catalog source')
                return
            location, mount, generation = db.execute('SELECT location,mountpoint,generation FROM source WHERE id=?',
                                                       (source_id,)).fetchone()
        version = (source_id, generation)
        try:
            before = _mounted_state(location, mount)
            games = catalog_transport.list_games(location, expected_root=before[1:] if before else None)
            if before != _mounted_state(location, mount):
                raise OSError('Source directory or mount changed during refresh')
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            # Remote command stderr may contain provider URLs or credentials. Never persist it.
            error = (str(exc)[:512] if Path(location).is_absolute() else
                     'Remote unavailable or listing failed; check rclone configuration and connection.')
            with self._connection() as db:
                db.execute("UPDATE source SET status='offline',error=? WHERE id=? AND generation=?", (error, *version))
            return
        with self._connection() as db:
            if not db.execute("UPDATE source SET status='online',last_seen=?,error='',mountpoint=? "
                              'WHERE id=? AND generation=?', (time.time(), before[0] if before else None, *version)).rowcount:
                return
            db.execute('DELETE FROM entry WHERE source_id=?', (source_id,))
            db.executemany('INSERT INTO entry (source_id,relative,size) VALUES (?,?,?)',
                           ((source_id, game.relative, game.size) for game in games))

    def refresh(self, source_id: str | None = None) -> list[Source]:
        """Replace each reachable source snapshot atomically; retain its last good snapshot on failure."""
        if source_id is not None:
            self._refresh_one(source_id, required=True)
            with self._connection() as db:
                return [Source(*row) for row in db.execute(f'SELECT {_SOURCE_FIELDS} FROM source WHERE id=?', (source_id,))]
        for source in self.sources():
            self._refresh_one(source.id, required=False)
        return self.sources()

    def entries(self, relative: str | None = None) -> list[Entry]:
        """One row per observed location, never a claim that matching paths have matching bytes."""
        query = ('SELECT e.relative,s.id,s.name,s.status,s.last_seen,e.size '
                 'FROM entry e JOIN source s ON e.source_id=s.id ')
        if relative is not None:
            query += 'WHERE e.relative=? '
        with self._connection() as db:
            return [Entry(*row) for row in db.execute(query + 'ORDER BY e.relative COLLATE NOCASE,e.relative,s.name_key',
                                                     () if relative is None else (relative,))]

    def pull(self, source_id: str, entry: str, destination: Path, progress=None) -> tuple[Path, ...]:
        """Copy a live, validated selection. Neither cached membership nor size authorizes a write."""
        scanner.safe_relative_name(entry)
        with self._connection() as db:
            row = db.execute('SELECT location,mountpoint FROM source WHERE id=?', (source_id,)).fetchone()
        if not row:
            raise ValueError('Unknown catalog source')
        location, mount = row
        state = _mounted_state(location, mount)
        return catalog_transport.pull_game(location, entry, destination, progress=progress,
                                           expected_root=state[1:] if state else None)
