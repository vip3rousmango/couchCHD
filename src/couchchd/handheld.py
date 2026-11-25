"""Copy playable images to an existing card; no conversion, source writes or journal adoption."""
from __future__ import annotations

import hashlib
import os
import secrets
import stat
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ContextManager, Protocol

from .handheld_profiles import DEFAULT_PROFILE, resolve_profile
from .ledger import SafetyError, install_new
from .menu_entry import host_home_path
from .output_formats import is_output
from .scanner import (_firmware_name, folder_system, library_root_allowed, platform_directory,
                      safe_relative_name, visible_directory)

_DIR = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
_MAX_PLAYLIST = 1024 * 1024


def safe_absolute(path):
    p = Path(path).expanduser()
    if '..' in p.parts:
        raise SafetyError(f'Choose a directory without parent traversal: {p}')
    return host_home_path(p.absolute())


@contextmanager
def open_directory(path, parent=None, *, create=False):
    """Pin each component, including the mount. Never follow a newly substituted link."""
    if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise SafetyError('Safe mounted-library access needs a Linux or macOS filesystem path.')
    if '..' in path.parts or path.is_absolute() != (parent is None):
        raise SafetyError(f'Invalid directory path: {path}')
    fd = os.open(path.anchor, _DIR) if parent is None else os.dup(parent)
    try:
        for part in (path.parts[1:] if parent is None else path.parts):
            if create:
                try:
                    os.mkdir(part, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _has_ancestor(directory, other):
    # String prefix comparisons miss differently spelled paths on NTFS/exFAT.
    expected = os.fstat(other)
    fd = os.dup(directory)
    try:
        current = os.fstat(fd)
        while not os.path.samestat(current, expected):
            parent = os.open('..', _DIR, dir_fd=fd)
            os.close(fd)
            fd = parent
            above = os.fstat(fd)
            if os.path.samestat(current, above):
                return False
            current = above
        return True
    finally:
        os.close(fd)


def _stamp(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


@dataclass(frozen=True)
class _File:
    path: Path   # relative to source root
    stamp: tuple

    @property
    def size(self):
        return self.stamp[2]


@contextmanager
def open_file(rootfd, file):
    with open_directory(file.path.parent, rootfd) as parent:
        fd = os.open(file.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or _stamp(st) != file.stamp:
                raise SafetyError(f'File changed: {file.path}')
            yield stream
            if _stamp(os.stat(file.path.name, dir_fd=parent, follow_symlinks=False)) != file.stamp:
                raise SafetyError(f'File changed during export: {file.path}')


def regular_file(path, parentfd):
    st = os.stat(path.name, dir_fd=parentfd, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or not st.st_size:
        raise SafetyError(f'Not a nonempty regular file: {path}')
    return _File(path, _stamp(st))


def _playlist(rootfd, playlist):
    if playlist.size > _MAX_PLAYLIST:
        raise SafetyError(f'Oversized playlist: {playlist.path}')
    with open_file(rootfd, playlist) as stream:
        lines = stream.read(_MAX_PLAYLIST + 1).decode('utf-8-sig').splitlines()
    members = {}
    for line in lines:
        if not line.strip() or line.startswith('#'):
            continue
        # Keep playlist bytes unchanged; don't reinterpret drives, slashes or spelling.
        rel = safe_relative_name(line.removeprefix('./'))
        if (rel.suffix.lower() != '.chd' or rel.name.startswith('.') or _firmware_name(rel) or
                any(not visible_directory(d) and d != '.chd_discs' for d in rel.parts[:-1])):
            raise SafetyError(f'Not a local playable CHD: {line}')
        path = playlist.path.parent / rel
        with open_directory(path.parent, rootfd) as parent:
            members[path] = regular_file(path, parent)
    if not members:
        raise SafetyError(f'Empty playlist: {playlist.path}')
    return (*members.values(), playlist)


@dataclass(frozen=True)
class _Entry:
    system: str
    folder: Path  # source platform folder relative to root
    files: tuple[_File, ...]  # entry point last

    @property
    def path(self):
        return self.files[-1].path


def _catalog(root, rootfd, formats, selected=()):
    catalog = {}
    members = set()
    if formats and library_root_allowed(root):
        for base, dirs, names, fd in os.fwalk('.', dir_fd=rootfd):
            dirs[:] = [d for d in dirs if visible_directory(d)]
            base = Path(base)
            platform = platform_directory(root, root / base)
            if platform is None:
                continue
            folder, system = platform
            if system not in formats:
                dirs.clear()
                continue
            for name in names:
                path = base / name
                ext = path.suffix.lower()
                playlist = ext == '.m3u'
                if name.startswith('.') or ext not in formats[system] or _firmware_name(path):
                    continue
                try:
                    safe_relative_name(path.as_posix())
                    f = regular_file(path, fd)
                    files = _playlist(rootfd, f) if playlist else (f,)
                except (ValueError, OSError) as exc:
                    if path.as_posix() in selected:
                        raise SafetyError(f'Not a complete playable export entry: {path}: {exc}') from exc
                    continue  # Only complete, portable, regular-file entries are selectable.
                members.update(member.path for member in files[:-1])
                catalog[path.as_posix()] = _Entry(system, folder.relative_to(root), files)
    for member in members:
        catalog.pop(member.as_posix(), None)
    return catalog


def entries(root, profile=DEFAULT_PROFILE, *, players=None) -> list[dict]:
    """Discover complete entries by layout, including non-ledger images. No game verification implied."""
    _, formats = resolve_profile(profile, players)
    root = safe_absolute(root)
    with open_directory(root) as fd:
        catalog = _catalog(root, fd, formats)
    return [dict(path=key, title=entry.path.stem, system=entry.system,
                 files=len(entry.files), size=sum(f.size for f in entry.files))
            for key, entry in sorted(catalog.items())]


def _fold(name):
    return unicodedata.normalize('NFC', name).casefold()


def _legacy_layout(dest, destfd, systems):
    """Backward-compatible RG405M pack; do not invent system names in a different layout."""
    direct = folder_system(dest.name)
    if direct:
        if systems != {direct}:
            raise SafetyError(f'Select the parent ROM directory to export other systems, not {dest.name}.')
        return {direct: Path('.')}
    if _fold(dest.parent.name) == 'roms' or _fold(dest.name) in {'ps', 'ds', 'nds'}:
        raise SafetyError('Choose a profile for this specific platform folder instead of the original RG405M layout.')
    base = Path('.')
    names = os.listdir(destfd)
    roms = [n for n in names if _fold(n) == 'roms']
    if roms and _fold(dest.name) != 'roms':
        if len(roms) != 1 or any(folder_system(n) for n in names):
            raise SafetyError('Ambiguous card layout; choose the intended ROMs or platform folder.')
        base = Path(roms[0])
        with open_directory(base, destfd) as romfd:
            names = os.listdir(romfd)
    result = {}
    # Keep the original empty-card workflow. On an initialized but unfamiliar card,
    # guessing a missing folder (e.g. creating psx beside a custom PS library) is unsafe.
    with open_directory(base, destfd) as romfd:
        folders = {n.casefold(): folder_system(n) for n in names if visible_directory(n) and
                   stat.S_ISDIR(os.stat(n, dir_fd=romfd, follow_symlinks=False).st_mode)}
    # An identified custom folder can receive its own games, but isn't evidence
    # that creating a *different* canonical directory will fit the card's layout.
    unfamiliar = (any(name != system and name != 'ps1' for name, system in folders.items()) or
                  (folders and {'psx', 'dreamcast', 'psp'}.isdisjoint(folders.values())))
    for system in systems:
        matches = [n for n in names if folder_system(n) == system]
        if len(matches) > 1:
            raise SafetyError(f'Multiple {system} folders; choose the intended platform folder.')
        if not matches and unfamiliar:
            raise SafetyError('Unrecognized card layout; choose a firmware or explicit platform-folder profile.')
        result[system] = base / (matches[0] if matches else system)
    return result


def _layout(dest, destfd, systems, profile):
    if not library_root_allowed(dest):
        raise SafetyError('Choose a game directory, not BIOS, firmware, arcade or private storage.')
    if profile.destination == 'rg405m':
        return _legacy_layout(dest, destfd, systems)
    if profile.destination == 'platform':
        if len(systems) != 1:
            raise SafetyError('This profile copies one system at a time. Choose its assigned game folder.')
        system, = systems
        direct = folder_system(dest.name) or {'ps': 'psx', 'ds': 'nds', 'nds': 'nds'}.get(_fold(dest.name))
        rom_parents = {'roms', 'easyroms', 'share', 'userdata', 'android'}
        if ((direct and direct != system) or _fold(dest.name) in rom_parents or
                (not direct and any(_fold(n) in rom_parents or folder_system(n) for n in os.listdir(destfd)))):
            raise SafetyError(f'Choose the existing game folder for {system}, not {dest.name}.')
        return {system: Path('.')}

    # Booted firmware creates these directories. Match its documented spelling,
    # not scanner aliases or volume labels; never provision or "fix" a card.
    direct = next((s for s in profile.folders if profile.matches_folder(dest.name, s)), None)
    if direct is not None:
        if systems != {direct}:
            raise SafetyError(f'Choose the firmware ROM root to export other systems, not {dest.name}.')
        return {direct: Path('.')}
    names = os.listdir(destfd)
    result = {}
    for system in systems:
        candidates = profile.folders[system]
        matches = [n for n in names if profile.matches_folder(n, system)]
        if len(matches) != 1:
            raise SafetyError(f'Choose an initialized {profile.firmware} ROM root with one of '
                              f'{", ".join(candidates)}, or choose the intended system folder directly.')
        with open_directory(Path(matches[0]), destfd):
            result[system] = Path(matches[0])
    return result


class PackSource(Protocol):
    size: int
    digest: bytes | None

    def stream(self) -> ContextManager[BinaryIO]: ...


@dataclass(frozen=True)
class _LocalSource:
    rootfd: int
    file: _File
    digest = None  # Local streams check file identity before and after reading.

    @property
    def size(self):
        return self.file.size

    def stream(self):
        return open_file(self.rootfd, self.file)


def _targets(chosen, layout, srcfd):
    targets = {}
    for entry in chosen:
        for file in entry.files:
            path = layout[entry.system] / file.path.relative_to(entry.folder)
            source = _LocalSource(srcfd, file)
            if targets.setdefault(path, source) != source:
                raise SafetyError(f'Two sources target the same card path: {path}')
    return targets


def _read_source(source, path, out=None, *, check=None):
    digest = hashlib.sha256()
    total = 0
    if check is not None:
        check()
    with source.stream() as stream:
        while True:
            if check is not None:
                check()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > source.size:
                raise SafetyError(f'Source exceeded declared size: {path}')
            digest.update(chunk)
            if out is not None:
                out.write(chunk)
    mark = digest.digest()
    expected = source.digest
    if total != source.size or (expected is not None and mark != expected):
        raise SafetyError(f'Source size or SHA-256 changed: {path}')
    return mark


def _preflight(targets, destfd):
    """Refuse all known conflicts before writing any files; index each card directory only once."""
    index = {}
    existing = {}
    card_device = os.fstat(destfd).st_dev
    for path, source in targets:
        base = Path('.')
        for part in path.parts:
            if base not in index:
                with open_directory(base, destfd) as fd:
                    if os.fstat(fd).st_dev != card_device:
                        raise SafetyError(f'{base} is a separate mount; choose its ROM folder directly.')
                    names = {}
                    for n in os.listdir(fd):
                        names.setdefault(_fold(n), []).append(n)
                    index[base] = names
            matches = index[base].get(_fold(part), [])
            if not matches:
                break
            if matches != [part]:
                raise SafetyError(f'Case or spelling conflict on card: {base / part}')
            base /= part
        else:
            with open_directory(path.parent, destfd) as fd:
                card = regular_file(path, fd)
            if card.size != source.size:
                raise SafetyError(f'Different file already on card; nothing overwritten: {path}')
            mark = _read_source(source, path)
            with open_file(destfd, card) as stream:
                if hashlib.file_digest(stream, 'sha256').digest() != mark:
                    raise SafetyError(f'Different file already on card; nothing overwritten: {path}')
            existing[path] = card
    return existing


def _copy(path, source, stagefd, name):
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=stagefd)
    with os.fdopen(fd, 'w+b') as dest:
        mark = _read_source(source, path, dest)
        dest.flush()
        os.fsync(fd)
        verified = _File(Path(name), _stamp(os.fstat(fd)))
        dest.seek(0)
        if hashlib.file_digest(dest, 'sha256').digest() != mark:
            raise SafetyError(f'Card copy did not match source SHA-256: {path}')
        return verified


def publish_pack(dest, files: Mapping[Path, PackSource], *, check: Callable[[], None] | None = None) -> dict:
    """Stage SHA-256-checked files, then atomically install without replacement, playlists last.

    Keys are destination-relative playable output paths. Source streams provide a known size
    and optionally an independent binary SHA-256 digest. The caller enforces source/destination
    separation; check() can reject stale snapshots after all reads and before publication.
    """
    with open_directory(safe_absolute(dest)) as fd:
        return _publish(fd, files, check)


def _ordered_targets(files):
    targets = []
    spellings = {}
    for key, source in files.items():
        path = safe_relative_name(os.fspath(key))
        if (source.size <= 0 or not (is_output(path) or path.suffix.lower() == '.m3u')
                or path.name.startswith('.') or any(not visible_directory(d) and d != '.chd_discs'
                                                    for d in path.parts[:-1])):
            raise SafetyError(f'Not a safe playable file: {path}')
        for component in (path, *path.parents):
            if spellings.setdefault(_fold(component.as_posix()), component) != component:
                raise SafetyError(f'Ambiguous card filename: {component}')
        targets.append((path, source))
    if not targets:
        raise SafetyError('Select at least one file to copy.')
    paths = {path for path, _ in targets}
    if len(paths) != len(targets) or any(parent in paths for path in paths for parent in path.parents):
        raise SafetyError('Two selected files claim the same path as a file or directory.')
    return sorted(targets, key=lambda pair: pair[0].suffix.lower() == '.m3u')


def _publish(destfd, files, check):
    targets = _ordered_targets(files)
    existing = _preflight(targets, destfd)
    pending = [(p, f) for p, f in targets if p not in existing]
    size = sum(f.size for _, f in pending)
    usage = os.fstatvfs(destfd)
    if size > usage.f_frsize * usage.f_bavail:
        raise SafetyError(f'Not enough free space on card for {size:,} bytes of new files.')
    copied = []

    def recheck():
        if check:
            check()
        for card in existing.values():
            with open_file(destfd, card):
                pass  # Identical files, especially playlist members, must still be present.

    if not pending:
        recheck()
    else:
        stage = '.couchchd-export-' + secrets.token_hex(16)
        os.mkdir(stage, 0o700, dir_fd=destfd)
        try:
            with open_directory(Path(stage), destfd) as stagefd:
                try:
                    staged = [_copy(path, source, stagefd, str(i)) for i, (path, source) in enumerate(pending)]
                    recheck()
                    for i, (path, _) in enumerate(pending):
                        if regular_file(staged[i].path, stagefd) != staged[i]:
                            raise SafetyError(f'Staged card copy changed before publication: {path}')
                        with open_directory(path.parent, destfd, create=True) as parent:
                            install_new(Path(str(i)), Path(path.name), src_fd=stagefd, dst_fd=parent)
                        copied.append(path.as_posix())
                finally:
                    for i in range(len(pending)):
                        try:
                            os.unlink(str(i), dir_fd=stagefd)
                        except FileNotFoundError:
                            pass
        except (OSError, ValueError) as exc:
            raise SafetyError(f'Export stopped: {exc}. Previously published complete files remain; '
                              'retry to verify and skip identical copies.') from exc
        finally:
            os.rmdir(stage, dir_fd=destfd)
    return dict(copied=copied, skipped=[p.as_posix() for p in existing], bytes=size)


def _selection_keys(selected):
    if isinstance(selected, (str, Path)):
        selected = [selected]
    keys = list(dict.fromkeys(safe_relative_name(os.fspath(k)).as_posix() for k in selected))
    if not keys:
        raise SafetyError('Select at least one game to export.')
    return keys


def _selected_entries(root, srcfd, formats, keys):
    catalog = _catalog(root, srcfd, formats, set(keys))
    for key in keys:
        if key not in catalog:
            raise SafetyError(f'Not a complete playable export entry: {key}')
    return [catalog[k] for k in keys]


@contextmanager
def platform_sources(root, selected, profile, *, players=None):
    """Pin the root and snapshot a single platform, using the same path rules as card export."""
    _, formats = resolve_profile(profile, players)
    root = safe_absolute(root)
    keys = _selection_keys(selected)
    systems = set()
    for key in keys:
        platform = platform_directory(root, (root / key).parent)
        system = platform[1] if platform else None
        if system not in formats:
            raise SafetyError(f'Not a complete playable export entry: {key}')
        systems.add(system)
    if len(systems) != 1:
        raise SafetyError('Send one platform at a time to its game folder.')
    system, = systems
    with open_directory(root) as fd:
        entries = _selected_entries(root, fd, {system: formats[system]}, keys)
        yield system, _ordered_targets(_targets(entries, {system: Path('.')}, fd))


def export(root, selected, dest, profile=DEFAULT_PROFILE, *, players=None, expected_destination=None) -> dict:
    """Hash both sides. Never replace files; publish complete images first, then their playlists."""
    profile, formats = resolve_profile(profile, players)
    root, dest = safe_absolute(root), safe_absolute(dest)
    if dest.is_relative_to(root) or root.is_relative_to(dest):
        raise SafetyError('Source and destination directories must not overlap.')
    keys = _selection_keys(selected)
    with open_directory(root) as srcfd, open_directory(dest) as destfd:
        if expected_destination is not None:
            pinned = os.fstat(destfd)
            if (pinned.st_dev, pinned.st_ino) != expected_destination:
                raise SafetyError('The selected destination changed. Choose the mounted game folder again.')
        if _has_ancestor(srcfd, destfd) or _has_ancestor(destfd, srcfd):
            raise SafetyError('Source and destination directories must not overlap.')
        chosen = _selected_entries(root, srcfd, formats, keys)
        targets = _targets(chosen, _layout(dest, destfd, {e.system for e in chosen}, profile), srcfd)

        def recheck():
            for source in targets.values():
                with source.stream():
                    pass  # Reconfirm the selected set is still the snapshot we staged.

        return _publish(destfd, targets, recheck)
