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

from . import archive7z, disc_zip
from .output_formats import SUFFIX_BY_MODE


SYSTEM_LABELS = {
    'psx': 'PlayStation', 'ps2': 'PlayStation 2', 'psp': 'PlayStation Portable',
    'segacd': 'Sega CD', 'saturn': 'Sega Saturn', 'dreamcast': 'Dreamcast',
    'pcenginecd': 'PC Engine CD / TurboGrafx-CD', 'pcfx': 'PC-FX',
    'neogeocd': 'Neo Geo CD', '3do': '3DO', 'amigacd32': 'Amiga CD32', 'amigacdtv': 'Amiga CDTV',
    'gc': 'GameCube', 'wii': 'Wii', 'xbox': 'Original Xbox', '3ds': 'Nintendo 3DS',
    'nes': 'NES / Famicom', 'snes': 'SNES / Super Famicom', 'gb': 'Game Boy', 'gbc': 'Game Boy Color',
    'gba': 'Game Boy Advance', 'n64': 'Nintendo 64', 'megadrive': 'Mega Drive / Genesis',
    'mastersystem': 'Master System', 'gamegear': 'Game Gear',
}
SYSTEMS = {
    **{name: name for name in SYSTEM_LABELS}, 'ps1': 'psx', 'megacd': 'segacd', 'gamecube': 'gc',
    'ngc': 'gc', 'n3ds': '3ds', 'xboxclassic': 'xbox', 'pcecd': 'pcenginecd', 'tgcd': 'pcenginecd',
    'tg-cd': 'pcenginecd', 'tg16cd': 'pcenginecd', 'turbografxcd': 'pcenginecd', 'turbografx-cd': 'pcenginecd',
    'neocd': 'neogeocd', 'neogeocdjp': 'neogeocd', 'cd32': 'amigacd32', 'cdtv': 'amigacdtv',
    'famicom': 'nes', 'fc': 'nes', 'sfc': 'snes', 'superfamicom': 'snes', 'snesna': 'snes',
    'genesis': 'megadrive', 'sms': 'mastersystem', 'gg': 'gamegear', 'gameboy': 'gb',
    'gameboycolor': 'gbc', 'gameboyadvance': 'gba',
}
# These additional systems have *CD* cores advertising cue + chd (libretro-core-info:
# mednafen_pce_fast, mednafen_pcfx, neocd, opera and puae). Not their floppy,
# HuCard, BIOS, hard disk or arcade counterparts.
FORMATS = {system: frozenset({'.cue'}) for system in (
    'segacd', 'saturn', 'pcenginecd', 'pcfx', 'neogeocd', '3do', 'amigacd32', 'amigacdtv')}
FORMATS.update({
    'psx': {'.cue', '.iso'}, 'ps2': {'.iso', '.cue'}, 'psp': {'.iso'},
    'dreamcast': {'.cue', '.gdi'}, 'gc': {'.iso', '.gcm'}, 'wii': {'.iso'},
    'xbox': {'.iso'}, '3ds': {'.3ds', '.cci'}, **archive7z.FORMATS,
})
_SINGLE_MODES = {'gc': 'rvz', 'wii': 'rvz', 'xbox': 'xiso', '3ds': 'trim3ds',
                 **dict.fromkeys(archive7z.FORMATS, '7z')}
_ARCHIVES = {'.zip', '.7z', '.rar', '.ecm', '.ape', '.flac'}
_PARTIAL = {'.part', '.crdownload', '.tmp'}
_EXCLUDED_DIRS = {'_couchchd_backup', '.couchchd_tmp', '.chd_discs', 'bios', 'firmware',
                  'arcade', 'fbneo', 'fba', 'fba_libretro', 'fbn', 'finalburnneo', 'neogeo',
                  'naomi', 'naomi2', 'atomiswave'}
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
    archive: disc_zip.Archive | None = field(default=None, repr=False)

    def assert_unchanged(self, path: Path | None = None) -> None:
        pairs = zip(self.files, self.stamps) if path is None else ((path, self.stamps[self.files.index(path)]),)
        for item, old in pairs:
            if item.resolve(strict=True) != item or _stamp(item, False) != old:
                raise ValueError(f'Source changed since scan: {item}')


def _excluded_directory(name: str) -> bool:
    folded = name.casefold()
    return folded in _EXCLUDED_DIRS or re.fullmatch(r'mame(?:\d.*|[-_].*)?', folded) is not None


def visible_directory(name: str) -> bool:
    return not name.startswith('.') and not _excluded_directory(name)


def library_root_allowed(root: Path) -> bool:
    # Selecting bios/nes (or a system inside the quarantine) directly must
    # not bypass exclusions. Hidden ancestors such as ~/.local are fine.
    return not root.name.startswith('.') and not any(map(_excluded_directory, root.parts))


def _system_directory(root: Path, directory: Path) -> Path | None:
    if not directory.is_relative_to(root):
        return None
    parts = directory.relative_to(root).parts
    if not visible_directory(root.name) or not all(map(visible_directory, parts)):
        return None
    folder = root
    if root.name.casefold() in SYSTEMS:
        return root
    for part in parts:
        folder /= part
        if part.casefold() in SYSTEMS:
            return folder
    return None


def system_folder(root: Path, path: Path) -> Path | None:
    """First matching system at or below root. A nested title is not another library."""
    return _system_directory(root, path.parent)


def library_system(root: Path, path: Path) -> str:
    folder = system_folder(root, path)
    return SYSTEMS[folder.name.casefold()] if folder is not None else ''


def _firmware_name(path: Path) -> bool:
    # Firmware bundles may live beside games (notably Neo Geo CD's neocdz.zip).
    stem = path.stem.casefold()
    return ((stem in {'neogeo', 'neocd', 'neocdz'} and path.suffix.lower() in _ARCHIVES)
            or re.search(r'(?:^|[\W_])(?:bios|firmware|ipl)(?:$|[\W_])', stem) is not None)


def is_input_name(path: Path, system: str) -> bool:
    name = path.name.casefold()
    return (not name.startswith('.') and not name.endswith(('.trim.3ds', '.trim.cci', '.xiso.iso'))
            and path.suffix.lower() in FORMATS.get(system, ()) and not _firmware_name(path))


def default_mode(system: str, ext: str) -> str:
    if system in _SINGLE_MODES:
        return _SINGLE_MODES[system]
    return 'createdvd' if system in ('psp', 'ps2') and ext.lower() == '.iso' else 'createcd'


def output_path(source: Path, mode: str) -> Path:
    return source.with_suffix(SUFFIX_BY_MODE[mode])


def output_mode(game: Game, psp_format: str) -> str:
    if psp_format not in ('chd', 'cso'):
        raise ValueError(f'Unknown PSP format: {psp_format}')
    if game.system != 'psp':
        return game.mode
    return 'cso' if psp_format == 'cso' else 'createdvd'


def select_format(game: Game, psp_format: str) -> Game:
    mode = output_mode(game, psp_format)
    if mode == game.mode:
        return game
    if mode == 'cso':
        from . import cso
        game.assert_unchanged()
        if game.archive is None:
            cso.validate(game.source, game.system)
        else:
            with disc_zip.catalog(game.source, game.stamps[0]) as (members, prefix):
                signature = prefix(members[0], 16 * 2048 + 7)[-7:]
            if signature[1:] != b'CD001\x01' or signature[0] not in range(4):
                raise ValueError('PSP ISO lacks an ISO 9660 volume descriptor')
        game.assert_unchanged()
    return replace(game, mode=mode)


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
    if path.name.startswith('.') or not all(map(visible_directory, path.parts[:-1])) or _firmware_name(path):
        raise ValueError(f'Unsafe firmware, hidden or excluded track path: {name}')
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
    yield from _decode_descriptor(path, data)


def _decode_descriptor(path: Path, data: bytes):
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


def _iso_geometry(path: Path, size: int, system: str, read_at) -> tuple[str, tuple[int, ...], list[str | FileLine]]:
    if system == 'ps2' and size < 700_000_000:
        raise ValueError('PS2 ISO below DVD threshold; use the original BIN/CUE for CD media')
    for width in ((2048, 2352, 2336) if system == 'psx' else (2048,)):
        if size % width or size < 17 * width:
            continue
        sector = read_at(16 * width, 32)
        if len(sector) != 32:
            continue
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
        # Some raw CDs are also a multiple of 2048 bytes. Supply their real geometry.
        sheet = [] if width == 2048 else [FileLine(path, 'FILE ', ' BINARY'), f'TRACK 01 {mode}', 'INDEX 01 00:00:00']
        return default_mode(system, '.iso'), (size // width,), sheet
    raise ValueError('Missing ISO 9660/UDF volume descriptor or incomplete sectors')


def _geometry(ext, tracks, lines, names, sizes):
    for track in tracks:
        track.path = names[track.path]
    lines = [replace(line, path=names[line.path]) if isinstance(line, FileLine) else line for line in lines]
    frames = _gdi_frames(tracks, sizes, lines) if ext == '.gdi' else _cue_frames(tracks, sizes)
    return tuple(frames), lines


def _local_zip(path: Path, system: str) -> bool:
    return (path.suffix.lower() == '.zip' and not path.name.startswith('.') and not _firmware_name(path)
            and bool({'.iso', '.cue', '.gdi', '.gcm'}.intersection(FORMATS.get(system, ()))))


def _archived_disc(source: Path, system: str, stamp: _STAMP) -> Game:
    with disc_zip.catalog(source, stamp) as (members, prefix):
        primaries = [m for m in members if is_input_name(m.path, system)]
        if len(primaries) != 1:
            raise ValueError(f'ZIP must contain exactly one supported {SYSTEM_LABELS[system]} disc; found {len(primaries)}')
        entry = primaries[0]
        ext = entry.path.suffix.lower()
        mode = default_mode(system, ext)
        sizes = {m.path: m.size for m in members}
        if ext in ('.cue', '.gdi'):
            if entry.size > 1 << 20:
                raise ValueError('ZIP disc descriptor exceeds 1 MiB')
            by_key = {unicodedata.normalize('NFC', p.as_posix()).casefold(): p for p in sizes}
            missing = None
            for tracks, lines in _decode_descriptor(entry.path, prefix(entry, entry.size)):
                names = {}
                for ref in dict.fromkeys(t.path for t in tracks):
                    key = unicodedata.normalize('NFC', (entry.path.parent / ref).as_posix()).casefold()
                    if key not in by_key:
                        missing = ref
                        break
                    names[ref] = by_key[key]
                else:
                    break
            else:
                raise ValueError(f'Missing ZIP track: {missing}' if missing else 'Unknown ZIP descriptor encoding')
            if set(sizes) != {entry.path, *names.values()}:
                raise ValueError('ZIP includes unreferenced files; repack only the sheet and its tracks')
            frames, lines = _geometry(ext, tracks, lines, names, sizes)
            if sum(frames) * 2448 > disc_zip.MAX_BYTES:
                raise ValueError('ZIP disc track geometry expands beyond 16 GiB')
        else:
            if len(members) != 1:
                raise ValueError('ZIP includes extra files; repack only the disc image')
            frames, lines = (), []
            if mode == 'rvz':
                from . import rvz
                header = prefix(entry, 0x440)
                gc, wii = header[0x1c:0x20] == rvz._GC, header[0x18:0x1c] == rvz._WII
                if (not 0x2460 <= entry.size <= rvz._MAX_DISC or gc == wii or gc != (system == 'gc')
                        or header[0x200:0x204] == b'NKIT'):
                    raise ValueError(f'Expected a raw {SYSTEM_LABELS[system]} disc header in ZIP')
            elif mode == 'xiso':
                from . import xiso
                # The XGD1 descriptor is ~387 MiB into the deflated stream. Do not
                # expand that during discovery; full XDVDFS validation is mandatory in scratch.
                if entry.size % 2048 or entry.size < xiso._XGD1 + xiso._GAME_BYTES:
                    raise ValueError('ZIP does not contain a complete original Xbox XGD1 image')
                if prefix(entry, xiso._DESCRIPTOR + len(xiso._MAGIC)).endswith(xiso._MAGIC):
                    raise ValueError('ZIP already contains a standalone XISO; nothing to convert')
            else:
                header = prefix(entry, 16 * (2352 if system == 'psx' else 2048) + 32)
                mode, frames, lines = _iso_geometry(entry.path, entry.size, system,
                                                     lambda pos, length: header[pos:pos + length])
    return Game(source, system, (source,), sum(sizes.values()), mode, frames, tuple(lines), (stamp,),
                disc_zip.Archive(entry.path, members))


def inspect(source: Path, system: str, *, fresh: bool = True) -> Game:
    return _inspect(source.parent.resolve(strict=True) / source.name, system, fresh, {})


def _inspect(source: Path, system: str, fresh: bool, index: _DirectoryIndex) -> Game:
    system = SYSTEMS.get(system.lower(), '')
    ext = source.suffix.lower()
    zipped = _local_zip(source, system)
    if not zipped and not is_input_name(source, system):
        raise ValueError(f'Unsupported system/format: {system}/{ext}')
    if source.is_symlink() or source.resolve(strict=True) != source:
        raise ValueError(f'Symlink in source path: {source}')
    safe_relative_name(source.name)
    stamps = {source: _stamp(source, fresh)}
    if zipped:
        return _archived_disc(source, system, stamps[source])
    if system in _SINGLE_MODES:
        mode = _SINGLE_MODES[system]
        if mode == 'rvz':
            from .rvz import validate
        elif mode == 'xiso':
            from .xiso import validate
        elif mode == 'trim3ds':
            from .trim3ds import validate
        else:
            validate = archive7z.validate
        validate(source, system)
        frames, lines = (), []
    elif ext == '.iso':
        with source.open('rb') as disc:
            def read_at(pos, length):
                disc.seek(pos)
                return disc.read(length)
            mode, frames, lines = _iso_geometry(source, stamps[source][2], system, read_at)
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
        frames, lines = _geometry(ext, tracks, lines, names, {p: stamp[2] for p, stamp in stamps.items()})
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
    if not library_root_allowed(root):
        return games, issues
    for basedir, dirs, names in os.walk(root, onerror=lambda e: issues.append(Issue(Path(e.filename), str(e)))):
        base = Path(basedir)
        children = []
        for d in sorted(d for d in dirs if visible_directory(d)):
            if (base / d).is_symlink():
                if d.casefold() in SYSTEMS:
                    issues.append(Issue(base / d, 'Linked system folder skipped; select its real location to scan.',
                                        'linked_folder'))
            else:
                children.append(d)
        dirs[:] = children
        # Resolve the library once per directory, rather than per file in large ROM sets.
        folder = _system_directory(root, base)
        system = SYSTEMS[folder.name.casefold()] if folder is not None else ''
        for name in sorted(names):
            path = base / name
            ext = path.suffix.lower()
            if name.startswith('.') or not system or _firmware_name(path):
                continue
            if ext in _ARCHIVES and not _local_zip(path, system):
                if system in archive7z.FORMATS and ext in ('.7z', '.zip'):
                    continue  # Single-ROM archives are already usable; never recompress them.
                issues.append(Issue(path, 'Needs unpacking before conversion', 'needs_unpacking'))
            elif ext in _PARTIAL:
                issues.append(Issue(path, 'Incomplete transfer', 'active_transfer'))
            elif _local_zip(path, system) or is_input_name(path, system):
                try:
                    games.append(_inspect(path, system, True, index))
                except (OSError, ValueError) as exc:
                    category = 'active_transfer' if str(exc).startswith('Active transfer') else 'invalid'
                    issues.append(Issue(path, str(exc), category))
    return games, issues


def live_track_references(root: Path):
    """Yield live sheets and their track paths. Unknown ownership raises, never guesses."""
    index: _DirectoryIndex = {}

    def fail(error: OSError) -> None:
        raise error

    for base, dirs, files in os.walk(root, onerror=fail):
        # Quarantined originals and application-owned scratch files aren't live games.
        dirs[:] = [d for d in dirs if d.lower() not in {'_couchchd_backup', '.couchchd_tmp'}]
        parent = Path(base)
        for name in files:
            if Path(name).suffix.lower() not in ('.cue', '.gdi'):
                continue
            desc = parent / name
            for tracks, _ in _descriptors(desc):
                try:
                    resolved = {_resolve(parent, t.path, index) for t in tracks}
                except ValueError:
                    continue
                yield desc, resolved
                break
            else:
                raise ValueError(f'Cannot establish track ownership for {desc}')


def unreferenced(path: Path, root: Path) -> bool:
    """Check all live descriptors after quarantine; any unreadable sheet means retain."""
    try:
        root = root.resolve(strict=True)
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            return False
        path = path.resolve(strict=True)
        return not any(path in refs for _, refs in live_track_references(root))
    except (OSError, ValueError):
        return False
