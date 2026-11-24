"""Read-only discovery, disc geometry, and ownership checks for library files."""
from __future__ import annotations

import ntpath
import os
import re
import stat
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath

SYSTEMS = {name: name for name in ('psx', 'segacd', 'saturn', 'dreamcast', 'ps2', 'psp')}
SYSTEMS.update(ps1='psx', megacd='segacd')
FORMATS = {'psx': {'.cue', '.iso'}, 'segacd': {'.cue'}, 'saturn': {'.cue'},
           'dreamcast': {'.cue', '.gdi'}, 'ps2': {'.iso', '.cue'}, 'psp': {'.iso'}}
_ARCHIVES = {'.zip', '.7z', '.rar', '.ecm', '.ape', '.flac'}
_PARTIAL = {'.part', '.crdownload', '.tmp'}
_PRIVATE = {'_couchchd_backup', '.couchchd_tmp', '.chd_discs'}
_WIDTHS = {'AUDIO': 2352, 'MODE1/2048': 2048, 'MODE1/2352': 2352,
           'MODE2/2336': 2336, 'MODE2/2352': 2352, 'MODE2/2048': 2048,
           'MODE2/2324': 2324}
_STAMP = tuple[int, int, int, int, int]


@dataclass(frozen=True)
class Issue:
    path: Path
    reason: str
    category: str = 'invalid'


@dataclass(frozen=True)
class FileLine:
    """A parsed filename slot; never substitute filenames into unparsed text."""
    path: Path
    before: str
    after: str
    offset: int = 0
    length: int | None = None


@dataclass(frozen=True)
class Game:
    source: Path
    system: str
    files: tuple[Path, ...]
    size: int
    mode: str
    frames: tuple[int, ...]
    sheet: tuple[str | FileLine, ...] = field(repr=False)
    stamps: tuple[_STAMP, ...] = field(repr=False)

    def assert_unchanged(self, path: Path | None = None) -> None:
        pairs = zip(self.files, self.stamps) if path is None else ((path, self.stamps[self.files.index(path)]),)
        for item, old in pairs:
            if item.resolve(strict=True) != item or _stamp(item, False) != old:
                raise ValueError(f'Source changed since scan: {item}')


def _stamp(path: Path, fresh: bool) -> _STAMP:
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or not s.st_size:
        raise ValueError(f'Not a nonempty regular file: {path}')
    if fresh and time.time() - s.st_mtime < 10:
        raise ValueError(f'Active transfer (modified less than 10 seconds ago): {path}')
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def safe_relative_name(name: str, *, descriptor: bool = False) -> Path:
    """Confine portable names on POSIX and Windows before any filesystem access."""
    win = PureWindowsPath(name)
    parts = (name.replace('\\', '/') if descriptor else name).split('/')
    reserved = getattr(ntpath, 'isreserved', None)
    for p in parts:
        if descriptor and p == '.':
            continue
        if (p in ('', '.', '..') or p.endswith((' ', '.'))
                or (reserved(p) if reserved else PureWindowsPath(p).is_reserved())
                or any(c in '<>:"|?*\\' or ord(c) < 32 or ord(c) == 127 for c in p)):
            raise ValueError(f'Unsafe relative path: {name!r}')
    if win.drive or win.root:
        raise ValueError(f'Unsafe absolute path: {name!r}')
    return Path(*parts)


def _relative(name: str) -> Path:
    path = safe_relative_name(name, descriptor=True)
    if path.suffix.lower() not in {'.bin', '.raw', '.img'}:
        raise ValueError(f'Unsupported track file: {name}')
    return path


_DirectoryIndex = dict[Path, dict[str, list[Path]]]


def _resolve(parent: Path, ref: Path, index: _DirectoryIndex) -> Path:
    for component in ref.parts:
        folded = unicodedata.normalize('NFC', component).casefold()
        if parent not in index:
            names: dict[str, list[Path]] = {}
            for entry in parent.iterdir():
                names.setdefault(unicodedata.normalize('NFC', entry.name).casefold(), []).append(entry)
            index[parent] = names
        matches = index[parent].get(folded, [])
        exact = [p for p in matches if p.name == component]
        matches = exact or matches
        if len(matches) != 1:
            raise ValueError(f'{"Ambiguous" if matches else "Missing"} track path: {ref}')
        parent = matches[0]
        if parent.is_symlink():
            raise ValueError(f'Symlink in track path: {parent}')
    return parent


@dataclass
class _Track:
    path: Path
    width: int
    high: bool = False
    indexes: dict[int, int] = field(default_factory=dict)
    pregap: int = 0
    lba: int = 0
    offset: int = 0


def _msf(value: str) -> int:
    m = re.fullmatch(r'(\d{1,3}):([0-5]\d):([0-7]\d)', value)
    if not m or int(m[3]) >= 75:
        raise ValueError(f'Invalid minute:second:frame: {value}')
    return (int(m[1]) * 60 + int(m[2])) * 75 + int(m[3])


def _cue(text: str) -> tuple[list[_Track], list[str | FileLine]]:
    tracks: list[_Track] = []
    lines: list[str | FileLine] = []
    pending: Path | None = None
    current: _Track | None = None
    high = False
    for line in text.splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        cmd = parts[0].upper()
        arg = parts[1].strip() if len(parts) == 2 else ''
        args = arg.upper().split()
        if cmd == 'FILE':
            match = re.fullmatch(r'(?:"([^"]+)"|(\S+))\s+BINARY', arg, re.I)
            if not match or pending is not None and current is None:
                raise ValueError(f'Malformed or unused FILE: {line}')
            pending = _relative(match[1] or match[2])
            lines.append(FileLine(pending, 'FILE ', ' BINARY'))
            current = None
            continue
        if cmd == 'TRACK':
            if (pending is None or len(args) != 2 or not re.fullmatch(r'\d{1,2}', args[0])
                    or int(args[0]) != len(tracks) + 1 or args[1] not in _WIDTHS):
                raise ValueError(f'Unsupported or out-of-order track: {line}')
            current = _Track(pending, _WIDTHS[args[1]], high)
            tracks.append(current)
        elif cmd == 'INDEX':
            if current is None or len(args) != 2 or not re.fullmatch(r'\d{1,2}', args[0]):
                raise ValueError(f'Malformed INDEX: {line}')
            index, pos = int(args[0]), _msf(args[1])
            if index <= max(current.indexes, default=-1) or pos < max(current.indexes.values(), default=0):
                raise ValueError(f'Unordered INDEX: {line}')
            current.indexes[index] = pos
        elif cmd in ('PREGAP', 'POSTGAP'):
            if current is None:
                raise ValueError(f'{cmd} outside a track')
            gap = _msf(arg)
            if cmd == 'PREGAP':
                current.pregap = gap
        elif cmd == 'REM':
            if args in (['SINGLE-DENSITY', 'AREA'], ['HIGH-DENSITY', 'AREA']):
                high = args[0] == 'HIGH-DENSITY'
            elif args[:1] == ['SESSION'] and args[1:] not in (['01'], ['1']):
                raise ValueError('Multi-session CUE is not losslessly supported')
            else:
                continue
        elif cmd in ('CATALOG', 'ISRC', 'TITLE', 'PERFORMER', 'SONGWRITER', 'FLAGS'):
            continue  # Text tags are not inputs to chdman's binary track parser.
        else:
            raise ValueError(f'Unsupported CUE directive: {cmd}')
        lines.append(' '.join((cmd, *args)))
    if current is None or not tracks or any(1 not in t.indexes for t in tracks):
        raise ValueError('Missing track or INDEX 01 in CUE')
    return tracks, lines


def _gdi(text: str) -> tuple[list[_Track], list[str | FileLine]]:
    rows = [r.strip() for r in text.splitlines() if r.strip()]
    if not rows or not re.fullmatch(r'\d{1,2}', rows[0]) or not 1 <= int(rows[0]) == len(rows) - 1:
        raise ValueError('Incorrect GDI track count')
    tracks: list[_Track] = []
    lines: list[str | FileLine] = [rows[0]]
    for i, row in enumerate(rows[1:], 1):
        m = re.fullmatch(r'(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(?:"([^"]+)"|([^"\n]+?))\s+(\d+)', row)
        if not m or int(m[1]) != i:
            raise ValueError(f'Invalid GDI row: {row}')
        lba, kind, width = map(int, m.group(2, 3, 4))
        if (kind, width) not in {(0, 2352), (4, 2352), (4, 2048)} or i == 1 and lba != 0:
            raise ValueError(f'Invalid GDI geometry: {row}')
        path = _relative(m[5] or m[6])
        tracks.append(_Track(path, width, lba=lba, offset=int(m[7])))
        # Older chdman ignores the last column. Give it the exact track slice at offset zero.
        lines.append(FileLine(path, f'{i} {lba} {kind} {width} ', ' 0', int(m[7])))
    return tracks, lines


def _descriptors(path: Path):
    if path.suffix.lower() not in ('.cue', '.gdi') or path.is_symlink():
        raise ValueError(f'Invalid descriptor: {path}')
    if _stamp(path, False)[2] > 1024 * 1024:
        raise ValueError(f'Descriptor exceeds 1 MiB: {path}')
    with path.open('rb') as f:
        data = f.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError(f'Descriptor exceeds 1 MiB: {path}')
    error = None
    for encoding in ('utf-8-sig', 'cp1252', 'shift_jis'):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if '\0' in text:
            raise ValueError('NUL byte in disc descriptor')
        try:
            parsed = (_cue if path.suffix.lower() == '.cue' else _gdi)(text)
        except ValueError as exc:
            if encoding == 'utf-8-sig':
                raise
            error = error or exc
            continue
        yield parsed
        if encoding == 'utf-8-sig' or data.isascii():
            break
    if error:
        raise error


def references(descriptor: Path, *, available: Callable[[Path], bool] | None = None) -> tuple[Path, ...]:
    """Parse safe relative names. A remote listing can disambiguate legacy encodings."""
    for tracks, _ in _descriptors(descriptor):
        refs = tuple(dict.fromkeys(t.path for t in tracks))
        if available is None or all(available(ref) for ref in refs):
            return refs
    raise ValueError(f'No descriptor encoding with available tracks: {descriptor}')


def _cue_frames(tracks: list[_Track], sizes: dict[Path, int]) -> list[int]:
    frames: list[int] = []
    consumed = 0
    seen: set[Path] = set()
    for i, t in enumerate(tracks):
        start = t.indexes.get(0, t.indexes[1])
        if t.pregap and 0 in t.indexes:
            raise ValueError('Ambiguous PREGAP and INDEX 00')
        if i == 0 or tracks[i-1].path != t.path:
            if start != 0 or t.path in seen:
                raise ValueError(f'Noncontiguous track file: {t.path}')
            seen.add(t.path)
            consumed = 0
        if i+1 < len(tracks) and tracks[i+1].path == t.path:
            nxt = tracks[i+1].indexes
            n = nxt.get(0, nxt[1]) - start
        else:
            n, rem = divmod(sizes[t.path] - consumed, t.width)
            if rem:
                raise ValueError(f'Partial sector in {t.path}')
        if n <= max(t.indexes.values()) - start:
            raise ValueError(f'INDEX outside track data: {t.path}')
        consumed += n * t.width
        frames.append(n)
    if any(t.high for t in tracks):
        # Redump Dreamcast cues transfer each physically stored pregap to the preceding
        # track and insert a density gap up to LBA 45000 (MAME cdrom_file::parse_cue).
        if tracks[0].high:
            raise ValueError('Missing single-density tracks')
        for i, t in enumerate(tracks[1:], 1):
            gap = t.pregap or t.indexes[1] - t.indexes.get(0, t.indexes[1])
            frames[i-1] += gap
            frames[i] -= gap
            if frames[i] <= 0:
                raise ValueError('GD-ROM pregap exceeds data')
        first = next(i for i, t in enumerate(tracks) if t.high)
        pad = 45000 - sum(frames[:first])
        if pad < 0 or any(not t.high for t in tracks[first:]):
            raise ValueError('Invalid GD-ROM density layout')
        frames[first-1] += pad
    return frames


def _gdi_frames(tracks: list[_Track], sizes: dict[Path, int], lines: list) -> list[int]:
    frames: list[int] = []
    for i, t in enumerate(tracks):
        nxt = next((s for s in tracks[i+1:] if s.path == t.path), None)
        end = nxt.offset if nxt else sizes[t.path]
        n, remainder = divmod(end - t.offset, t.width)
        if remainder or n <= 0 or end > sizes[t.path]:
            raise ValueError(f'Invalid track extent: {t.path}')
        if t.offset or end != sizes[t.path]:
            lines[i+1] = replace(lines[i+1], length=end - t.offset)
        span = tracks[i+1].lba - t.lba if i+1 < len(tracks) else n
        if span < n or t.lba + span >= 2**31:
            raise ValueError(f'Overlapping or oversized GDI track: {t.path}')
        frames.append(span)
    return frames


def _iso(path: Path, size: int, system: str) -> tuple[str, tuple[int, ...], list[str | FileLine]]:
    if system == 'ps2' and size < 700_000_000:
        raise ValueError('PS2 ISO below DVD threshold; use the original BIN/CUE for CD media')
    with path.open('rb') as disc:
        for width in ((2048, 2352, 2336) if system == 'psx' else (2048,)):
            if size % width or size < 17 * width:
                continue
            disc.seek(16 * width)
            sector = disc.read(32)
            offset = 0
            if width == 2352:
                if sector[:12] != b'\x00' + b'\xff' * 10 + b'\x00' or sector[15] not in (1, 2):
                    continue
                mode = f'MODE{sector[15]}/2352'
                offset = 16 if sector[15] == 1 else 24
            elif width == 2336:
                mode = 'MODE2/2336'
                offset = 8  # XA subheader
            sig = sector[offset:offset+7]
            if sig != b'\x00BEA01\x01' and not (sig[1:] == b'CD001\x01' and sig[0] in range(4)):
                continue
            # Do not let chdman guess between raw and cooked sectors from length
            # divisibility: some raw CDs are *also* a multiple of 2048 bytes.
            sheet = [] if width == 2048 else [FileLine(path, 'FILE ', ' BINARY'), f'TRACK 01 {mode}', 'INDEX 01 00:00:00']
            return ('createcd' if system == 'psx' else 'createdvd'), (size // width,), sheet
    raise ValueError('Missing ISO 9660/UDF volume descriptor or incomplete sectors')


def inspect(source: Path, system: str, *, fresh: bool = True) -> Game:
    return _inspect(source, system, fresh, {})


def _inspect(source: Path, system: str, fresh: bool, index: _DirectoryIndex) -> Game:
    system = SYSTEMS.get(system.lower(), '')
    ext = source.suffix.lower()
    if ext not in FORMATS.get(system, set()):
        raise ValueError(f'Unsupported system/format: {system}/{ext}')
    if source.is_symlink():
        raise ValueError(f'Symlink source: {source}')
    source = source.resolve(strict=True)
    stamps = {source: _stamp(source, fresh)}
    if ext == '.iso':
        mode, frames, lines = _iso(source, stamps[source][2], system)
    else:
        failure: ValueError | None = None
        for tracks, lines in _descriptors(source):
            try:
                names = {ref: _resolve(source.parent, ref, index) for ref in dict.fromkeys(t.path for t in tracks)}
            except ValueError as exc:
                failure = failure or exc
                continue  # cp1252 may decode Shift-JIS filenames without raising.
            break
        else:
            raise failure or ValueError(f'Unknown descriptor encoding: {source}')
        for path in names.values():
            stamps[path] = _stamp(path, fresh)
        for t in tracks:
            t.path = names[t.path]
        lines = [replace(line, path=names[line.path]) if isinstance(line, FileLine) else line for line in lines]
        sizes = {p: stamp[2] for p, stamp in stamps.items()}
        frames = tuple(_gdi_frames(tracks, sizes, lines) if ext == '.gdi' else _cue_frames(tracks, sizes))
        mode = 'createcd'
    game = Game(source, system, tuple(stamps), sum(s[2] for s in stamps.values()), mode, frames,
                tuple(lines), tuple(stamps.values()))
    game.assert_unchanged()
    return game


def scan(root: Path) -> tuple[list[Game], list[Issue]]:
    games: list[Game] = []
    index: _DirectoryIndex = {}
    issues: list[Issue] = []
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    for basedir, dirs, names in os.walk(root, onerror=lambda e: issues.append(Issue(Path(e.filename), str(e)))):
        base = Path(basedir)
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d.lower() not in _PRIVATE and not (base/d).is_symlink())
        system = next((SYSTEMS[p.lower()] for p in reversed((root.name, *base.relative_to(root).parts)) if p.lower() in SYSTEMS), None)
        for name in sorted(names):
            path = base / name
            ext = path.suffix.lower()
            if name.startswith('.'):
                continue
            if ext in _ARCHIVES:
                issues.append(Issue(path, 'Needs unpacking before conversion', 'needs_unpacking'))
            elif ext in _PARTIAL:
                issues.append(Issue(path, 'Incomplete transfer', 'active_transfer'))
            elif system and ext in ('.iso', '.gdi', '.cue'):
                try:
                    games.append(_inspect(path, system, True, index))
                except (OSError, ValueError) as exc:
                    category = 'active_transfer' if str(exc).startswith('Active transfer') else 'invalid'
                    issues.append(Issue(path, str(exc), category))
    return games, issues


def unreferenced(path: Path, root: Path) -> bool:
    """Check live descriptors *after* quarantine of the completed descriptor, under lock.

    Even an unsupported system, incomplete sheet, or unreadable directory might own
    this track. In ambiguous cases retain the original. Never follow library symlinks.
    """
    index: _DirectoryIndex = {}

    def fail(error: OSError) -> None:
        raise error

    try:
        root = root.resolve(strict=True)
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            return False
        path = path.resolve(strict=True)
        for base, dirs, files in os.walk(root, onerror=fail):
            # Quarantined originals and application-owned scratch files aren't live games.
            dirs[:] = [d for d in dirs if d.lower() not in {'_couchchd_backup', '.couchchd_tmp'}]
            parent = Path(base)
            for name in files:
                if Path(name).suffix.lower() not in ('.cue', '.gdi'):
                    continue
                desc = parent / name
                found = False
                for tracks, _ in _descriptors(desc):
                    try:
                        resolved = {_resolve(parent, t.path, index) for t in tracks}
                    except ValueError:
                        continue
                    if path in resolved:
                        return False
                    found = True
                    break
                if not found:
                    return False
    except (OSError, ValueError):
        return False
    return True
