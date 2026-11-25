"""Read-only discovery of originals and selective copying of published games."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from . import handheld, scanner
from .ledger import SafetyError
from .output_formats import SUFFIX_BY_MODE
from .scanner import SYSTEMS, default_mode, library_root_allowed, safe_relative_name, visible_directory

_MAX_PLAYLIST = 1024 * 1024
_REMOTE = re.compile(r'([A-Za-z0-9_][A-Za-z0-9_. -]*):(.*)\Z')
_ROM_SUFFIXES = frozenset(ext for exts in scanner.FORMATS.values() for ext in exts) | {
    '.zip', '.rar', '.7z', '.chd', '.cso', '.rvz', '.m3u', '.pbp', '.cdi', '.ecm', '.wbfs',
}
# Prune excluded directory trees on the provider, not after fetching every row.
# .chd_discs is the only hidden directory allowed (for complete playlist sets).
_REMOTE_FILTERS = tuple(arg for pattern in (
    '+ .chd_discs/', '- .*/',
    *(f'- {name}/' for name in sorted(scanner._EXCLUDED_DIRS) if not name.startswith('.')),
    '- mame/', '- mame[0-9]*/', '- mame[-_]*/',
) for arg in ('--filter', pattern))


@dataclass(frozen=True)
class Game:
    relative: str
    size: int
    members: tuple[str, ...]  # exact source keys; empty for conversion candidates
    kind: str = 'output'


@dataclass(frozen=True)
class Discovery:
    games: list[Game]
    directories: int
    platform_folders: int
    unplaced: int
    unsupported: int
    invalid_playlists: int = 0


@dataclass
class _Inventory:
    files: dict = field(default_factory=dict)
    originals: list[Game] = field(default_factory=list)
    directories: int = 0
    platform_folders: int = 0
    unplaced: int = 0
    unsupported: int = 0

    def add(self, key, kind, file):
        if '.chd_discs' in key.parts and kind != 'image':
            return
        if kind == 'unplaced':
            self.unplaced += 1
        elif kind == 'unsupported':
            self.unsupported += 1
        elif kind == 'original':
            self.originals.append(Game(key.as_posix(), file.size, (), 'original'))
        else:
            self.files[key] = file


def _platform(key):
    """A wrapper is not a system, and conflicting platform names are not a route."""
    found = None
    for i, part in enumerate(key.parts[:-1]):
        if (system := SYSTEMS.get(part.casefold())):
            if found is not None:
                return None
            found = i, system
    return found


def system_for(relative: str) -> str:
    found = _platform(Path(relative))
    return found[1] if found else ''


def _directory(name):
    return visible_directory(name) or name == '.chd_discs'


def _kind(key):
    ext = key.suffix.casefold()
    if (ext not in _ROM_SUFFIXES or key.name.startswith('.') or scanner._firmware_name(key)
            or not all(map(_directory, key.parts[:-1]))):
        return None
    platform = _platform(key)
    if not platform:
        return 'unplaced'
    index, system = platform
    if '.chd_discs' in key.parts[:index]:
        return None
    if ext == '.m3u':
        return 'playlist'
    suffix = SUFFIX_BY_MODE[default_mode(system, '')]
    if key.name.casefold().endswith(suffix) or (system == 'psp' and ext == '.cso'):
        return 'image'
    if scanner.is_input_name(key, system) or scanner._local_zip(key, system):
        return 'original'
    return 'unsupported'


def selectable_output(key: Path) -> bool:
    return '.chd_discs' not in key.parts and _kind(key) in ('image', 'playlist')


def _portable_directory(name):
    if not _directory(name):
        return False
    try:
        safe_relative_name(name)
    except ValueError:
        return False
    return True


def _raise(exc):
    raise exc


class _Mounted:
    def __init__(self, root, fd):
        self.root, self.fd = root, fd
        self.prefix = Path(root.name) if root.name.casefold() in SYSTEMS else Path()

    def inventory(self):
        result = _Inventory()
        for base, dirs, names, fd in os.fwalk('.', dir_fd=self.fd, onerror=_raise, follow_symlinks=False):
            dirs[:] = [d for d in dirs if _portable_directory(d)]
            result.directories += 1
            result.platform_folders += (self.prefix / base).name.casefold() in SYSTEMS
            for name in names:
                path = Path(base) / name
                key = self.prefix / path
                if not (kind := _kind(key)):
                    continue
                try:
                    safe_relative_name(key.as_posix())
                    result.add(key, kind, handheld.regular_file(path, fd))
                except ValueError:
                    continue  # Not a portable, nonempty regular game file.
        return result

    def lookup(self, key):
        if not key.is_relative_to(self.prefix):
            return None
        path = key.relative_to(self.prefix)
        try:
            with handheld.open_directory(path.parent, self.fd) as parent:
                return handheld.regular_file(path, parent)
        except ValueError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                return None
            raise

    def stream(self, file):
        return handheld.open_file(self.fd, file)

    def check(self, files):
        with handheld.open_directory(self.root) as fresh:
            if not os.path.samestat(os.fstat(self.fd), os.fstat(fresh)):
                raise SafetyError('Source library disconnected or replaced.')
        for file in files:
            with self.stream(file):
                pass  # Pinned no-follow open checks the inode and full stat stamp.


@dataclass(frozen=True)
class _Object:
    path: Path
    size: int
    modified: str
    identity: str


class _Remote:
    def __init__(self, name, base):
        self.exe = shutil.which('rclone')
        if not self.exe:
            raise SafetyError('Install rclone for this configured remote, or select its mounted ROM folder.')
        self.base = name + ':' + base
        folder = Path(base).name
        self.prefix = Path(folder) if folder.casefold() in SYSTEMS else Path()
        self.directories = {}

    def _args(self, command, *flags, path=None):
        target = self.base
        if path is not None:
            target += ('' if target.endswith((':', '/')) else '/') + path.as_posix()
        # No inline backends, filters from filenames, password prompts or remote hashing shell.
        return [self.exe, command, '--ask-password=false', '--sftp-ask-password=false',
                '--sftp-disable-hashcheck', '--sftp-shell-type', 'none', '--sftp-skip-links',
                '--copy-links=false', '--links=false',
                *flags, '--', target]

    def _json(self, *flags, path=None):
        cmd = self._args('lsjson', '--no-mimetype', *flags, path=path)
        run = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if run.returncode:
            raise SafetyError('Cannot read rclone library. Check its connection, configuration and permissions.')
        try:
            return json.loads(run.stdout)
        except (ValueError, UnicodeError) as exc:
            raise SafetyError('Invalid rclone library listing.') from exc

    def _object(self, path, row):
        if not isinstance(row, dict) or row.get('IsDir') is not False or type(row.get('Size')) is not int:
            raise SafetyError(f'Invalid rclone file metadata: {path}')
        return _Object(path, row['Size'], row.get('ModTime', ''), row.get('ID', ''))

    def _rows(self, *flags, path=None):
        rows = self._json(*flags, path=path)
        if not isinstance(rows, list):
            raise SafetyError('Invalid rclone library listing.')
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('Path'), str):
                raise SafetyError('Missing path in rclone listing.')
            try:
                path = safe_relative_name(row['Path'])
            except ValueError:
                continue
            if path in seen:
                raise SafetyError(f'Ambiguous duplicate rclone object: {path}')
            seen.add(path)
            yield path, row

    def inventory(self):
        result = _Inventory(directories=1, platform_folders=int(bool(self.prefix.parts)))
        for path, row in self._rows('--recursive', '--ignore-case', *_REMOTE_FILTERS):
            if row.get('IsDir') is True:
                if all(map(_directory, path.parts)):
                    result.directories += 1
                    result.platform_folders += path.name.casefold() in SYSTEMS
                continue
            key = self.prefix / path
            if not (kind := _kind(key)):
                continue
            file = self._object(path, row)
            if file.size <= 0:
                continue
            result.add(key, kind, file)
        return result

    def lookup(self, key):
        if not key.is_relative_to(self.prefix):
            return None
        path = key.relative_to(self.prefix)
        parent = Path()
        # Don't recursively inventory another device for each pull. List only the
        # exact parents, so skip-links applies to each component, including leaves.
        for idx, part in enumerate(path.parts):
            if parent not in self.directories:
                self.directories[parent] = {p.as_posix(): row for p, row in
                                            self._rows(path=parent if parent.parts else None)}
            row = self.directories[parent].get(part)
            if row is None:
                return None
            parent /= part
            if idx == len(path.parts) - 1:
                if row.get('IsDir') is not False:
                    return None
                file = self._object(path, row)
                return file if file.size > 0 else None
            if row.get('IsDir') is not True:
                return None

    @contextmanager
    def stream(self, file):
        cmd = self._args('cat', '--count', str(file.size + 1), path=file.path)
        with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            try:
                yield process.stdout
            except BaseException:
                process.kill()
                raise
            finally:
                process.stdout.close()
            if process.wait():
                raise SafetyError(f'Could not read remote file: {file.path}')

    def check(self, files):
        self.directories.clear()
        for file in files:
            if self.lookup(self.prefix / file.path) != file:
                raise SafetyError(f'Remote source changed: {file.path}')


def parse_remote(location: str) -> tuple[str, str]:
    """Parse a named rclone root, never an inline backend or credential-bearing URL."""
    match = _REMOTE.fullmatch(location)
    if not match:
        raise SafetyError('Choose an absolute mounted folder or a configured rclone name:path.')
    name, base = match.groups()
    if base.startswith('//'):
        raise SafetyError('Invalid rclone root.')
    parts = base.strip('/')
    if parts and not library_root_allowed(safe_relative_name(parts)):
        raise SafetyError('Excluded remote library folder.')
    return name, base.rstrip('/') or ('/' if base else '')


@contextmanager
def _open(location, expected_root):
    location = os.fspath(location)
    if os.path.isabs(location):
        root = handheld.safe_absolute(location)
        if not library_root_allowed(root):
            raise SafetyError(f'Excluded library folder: {root}')
        with handheld.open_directory(root) as fd:
            identity = os.fstat(fd)
            if expected_root is not None and (identity.st_dev, identity.st_ino) != expected_root:
                raise SafetyError(f'Mounted library disconnected or replaced: {root}')
            yield _Mounted(root, fd)
    else:
        yield _Remote(*parse_remote(location))


class _InvalidPlaylist(ValueError):
    pass


def _members(store, key, lookup):
    file = lookup(key)
    if file is None:
        raise SafetyError(f'Selected game is no longer available: {key}')
    if _kind(key) != 'playlist':
        return {key: file}, None
    if file.size > _MAX_PLAYLIST:
        raise _InvalidPlaylist(f'Oversized playlist: {key}')
    with store.stream(file) as stream:
        data = stream.read(_MAX_PLAYLIST + 1)
    if len(data) != file.size:
        raise SafetyError(f'Playlist changed: {key}')
    try:
        members = {}
        for line in data.decode('utf-8-sig').splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            ref = safe_relative_name(line.removeprefix('./'))
            path = key.parent / ref
            if _kind(path) != 'image':
                raise ValueError(f'Ineligible member: {line}')
            members[path] = lookup(path)
            if members[path] is None:
                raise ValueError(f'Missing member: {line}')
        if not members:
            raise ValueError('Empty playlist')
    except ValueError as exc:
        raise _InvalidPlaylist(f'Invalid playlist {key}: {exc}') from exc
    members[key] = file
    return members, hashlib.sha256(data).digest()


def discover(location: str | Path, *, expected_root=None) -> Discovery:
    """Read metadata and bounded playlist text only; never probe discs, archives or loose tracks."""
    with _open(location, expected_root) as store:
        inventory = store.inventory()
        games = inventory.originals
        invalid_playlists = 0
        grouped = set()
        for key, file in inventory.files.items():
            if '.chd_discs' in key.parts:
                continue
            if key.suffix.casefold() != '.m3u':
                name = key.as_posix()
                games.append(Game(name, file.size, (name,)))
                continue
            try:
                pack, _ = _members(store, key, inventory.files.get)
            except _InvalidPlaylist:
                invalid_playlists += 1
                continue
            paths = tuple(p.as_posix() for p in pack)
            grouped.update(paths[:-1])
            games.append(Game(key.as_posix(), sum(f.size for f in pack.values()), paths))
        if isinstance(store, _Mounted):
            store.check(())  # Never cache a listing of a disconnected mount.
        return Discovery(sorted((g for g in games if g.relative not in grouped), key=lambda g: g.relative),
                         inventory.directories, inventory.platform_folders, inventory.unplaced, inventory.unsupported,
                         invalid_playlists)


def list_games(location: str | Path, *, expected_root=None) -> list[Game]:
    """List only selectable published games, not the originals detected alongside them."""
    return [g for g in discover(location, expected_root=expected_root).games if g.kind == 'output']


class _Reader:
    def __init__(self, stream, report):
        self.stream, self.report = stream, report

    def read(self, size):
        data = self.stream.read(size)
        if data:
            self.report(len(data))
        return data


class _Source:
    def __init__(self, store, file, playlist_hash, report):
        self.store, self.file = store, file
        self.size, self._playlist_hash, self.report = file.size, playlist_hash, report

    @cached_property
    def digest(self):
        if self._playlist_hash is not None:
            return self._playlist_hash  # Must publish exactly the descriptor that was parsed.
        if isinstance(self.store, _Remote):
            # SFTP/SMB need not offer trustworthy content hashes or fine-grained mtimes.
            # Independently read selected bytes; do not invoke a shell on the peer.
            with self.store.stream(self.file) as stream:
                return hashlib.file_digest(stream, 'sha256').digest()
        return None  # A mounted source has a pinned, stat-checked reader.

    @contextmanager
    def stream(self):
        with self.store.stream(self.file) as stream:
            yield _Reader(stream, self.report) if self.report else stream


def destination_paths(destination, members) -> tuple[Path, ...]:
    """Compute actual target paths, including when the selected root is itself a platform."""
    root = handheld.safe_absolute(destination)
    if not library_root_allowed(root):
        raise SafetyError(f'Excluded destination folder: {root}')
    direct = SYSTEMS.get(root.name.casefold())
    folders = {}
    if not direct:
        with handheld.open_directory(root) as fd:
            for name in os.listdir(fd):
                if (system := SYSTEMS.get(name.casefold())):
                    folders[system] = None if system in folders else name
    result = []
    for member in members:
        key = safe_relative_name(os.fspath(member))
        if _kind(key) not in ('image', 'playlist'):
            raise SafetyError(f'Not a compressed platform game: {key}')
        index, system = _platform(key)
        tail = Path(*key.parts[index + 1:])
        if direct and direct != system:
            raise SafetyError(f'Select a ROM parent or a matching platform folder, not {root.name}.')
        if not direct:
            folder = folders.get(system, system)
            if folder is None:
                raise SafetyError(f'Multiple {system} folders; select one as the destination.')
            tail = Path(folder) / tail
        result.append(root / tail)
    return tuple(result)


def pull_game(location: str | Path, relative: str, destination: Path, progress=None, *,
              expected_root=None) -> tuple[Path, ...]:
    """Verify one selected file or complete set; never replace differing bytes or create a missing root."""
    key = safe_relative_name(relative)
    if not selectable_output(key):
        raise SafetyError(f'Not a selectable compressed game: {key}')
    dest = handheld.safe_absolute(destination)
    with _open(location, expected_root) as store, handheld.open_directory(dest) as destfd:
        if isinstance(store, _Mounted):
            if handheld._has_ancestor(destfd, store.fd) or handheld._has_ancestor(store.fd, destfd):
                raise SafetyError('Source and destination libraries must not overlap.')
        pack, playlist_hash = _members(store, key, store.lookup)
        paths = tuple(pack)
        targets = destination_paths(dest, paths)
        total = sum(f.size for f in pack.values())
        done = 0

        def report(count):
            nonlocal done
            done += count
            progress(done, total)

        def check():
            store.check(pack.values())
            with handheld.open_directory(dest) as fresh:
                if not os.path.samestat(os.fstat(destfd), os.fstat(fresh)):
                    raise SafetyError('Destination library disconnected or replaced.')

        if progress:
            progress(0, total)
        files = {target.relative_to(dest): _Source(store, pack[p], playlist_hash if p == key else None,
                                                  report if progress else None)
                 for p, target in zip(paths, targets, strict=True)}
        handheld.publish_pack(dest, files, check=check)
        return targets
