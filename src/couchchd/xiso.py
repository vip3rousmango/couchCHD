"""Repack the original Xbox XGD1 game filesystem, not the physical disc.

Directory trees, raw names, attributes and file payloads survive. Video,
security/filler sectors and original LBAs do not. Never patch XBE files.
Layout: https://multimedia.cx/xdvdfs.html ; XGD1 geometry: LibXGD/XGD.cs
in https://github.com/Deterous/XboxKit . No names are extracted onto the host.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

_SECTOR = 2048
_DESCRIPTOR = 32 * _SECTOR
_MAGIC = b'MICROSOFT*XBOX*MEDIA'
_XGD1 = 0x18300000
_GAME_BYTES = 0x1A2DB0000
_XBOX360 = (0x0FD90000, 0x02080000, 0x89D80000)
_DIR = 0x10
_RECORD = struct.Struct('<HHIIBB')
_CHUNK = 1024 * 1024
# A DWORD-indexed 16-bit pointer plus the largest record, rounded to sectors.
_MAX_TABLE = 129 * _SECTOR
_MAX_ENTRIES = 65536
_MAX_TABLE_BYTES = 32 * 1024 * 1024


@dataclass(slots=True)
class _Entry:
    parent: int
    slot: int
    name: bytes
    attr: int
    sector: int
    size: int
    first_child: int = 0
    end_child: int = 0


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError('XISO conversion cancelled; original retained')


def _read(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise ValueError('Truncated XDVDFS image')
    return data


def _ceil(value, unit):
    return (value + unit - 1) // unit


def _children(table, parent, cancel):
    """Follow relative DWORD links, then prove all record ranges are disjoint."""
    if not table.strip(b'\xff') or not table.strip(b'\x00'):
        return
    pending = [(0, None, None)]
    ranges = {}
    names = set()
    # Published tools use both upper- and lowercase folding for ASCII ordering.
    # Preserve the original tree and non-ASCII names without host-locale decoding.
    upper_order = lower_order = True
    while pending:
        _cancelled(cancel)
        slot, lo, hi = pending.pop()
        if slot in ranges or slot + _RECORD.size > len(table):
            raise ValueError('Invalid or cyclic XDVDFS directory link')
        left, right, sector, size, attr, length = _RECORD.unpack_from(table, slot)
        end = slot + _RECORD.size + length
        if not length or end > len(table) or slot // _SECTOR != (end - 1) // _SECTOR:
            raise ValueError('Invalid XDVDFS directory record size')
        name = table[slot + _RECORD.size:end]
        if name in (b'.', b'..') or any(c in name for c in (b'\x00', b'/', b'\\')):
            raise ValueError('Invalid XDVDFS filename')
        if name.upper() in names:
            raise ValueError('Duplicate XDVDFS filename')
        names.add(name.upper())
        if name.isascii():
            if lo is not None and lo.isascii():
                upper_order &= lo.upper() < name.upper()
                lower_order &= lo.lower() < name.lower()
            if hi is not None and hi.isascii():
                upper_order &= name.upper() < hi.upper()
                lower_order &= name.lower() < hi.lower()
            if not (upper_order or lower_order):
                raise ValueError('Unsearchable XDVDFS directory ordering')
        ranges[slot] = end
        yield _Entry(parent, slot, name, attr, sector, size)
        if right:
            pending.append((right * 4, name, hi))
        if left:
            pending.append((left * 4, lo, name))
    end = 0
    for start in sorted(ranges):
        if start < end or table[end:start].strip(b'\xff\x00'):
            raise ValueError('Overlapping or unreachable XDVDFS directory record')
        end = ranges[start]
    if table[end:].strip(b'\xff\x00'):
        raise ValueError('Unreachable XDVDFS directory record')


def _image(stream, base, length, cancel):
    stream.seek(base + _DESCRIPTOR)
    header = _read(stream, _SECTOR)
    if not (header.startswith(_MAGIC) and header.endswith(_MAGIC)):
        raise ValueError('Invalid XDVDFS volume descriptor')
    sector, size = struct.unpack_from('<II', header, 20)
    entries = [_Entry(-1, 0, b'', _DIR, sector, size)]
    extents = {}
    table_bytes = 0
    xbe = None
    for index, entry in enumerate(entries):  # Children append to this breadth-first queue.
        _cancelled(cancel)
        start = entry.sector * _SECTOR
        if start + _ceil(entry.size, _SECTOR) * _SECTOR > length:
            raise ValueError('XDVDFS extent outside game partition')
        if entry.parent == 0 and entry.name.upper() == b'DEFAULT.XEX':
            raise ValueError('Xbox 360 game images are not supported')
        if entry.parent == 0 and entry.name.upper() == b'DEFAULT.XBE' and not entry.attr & _DIR:
            xbe = entry
        if not entry.size:
            continue
        if entry.sector < 33:
            raise ValueError('XDVDFS extent overlaps reserved sectors')
        prev = extents.get(entry.sector)
        if prev is not None:
            if (entry.attr | prev.attr) & _DIR or prev.size != entry.size:
                raise ValueError('Aliased or cyclic XDVDFS extent')
            continue  # Exact file aliases share storage, never recurse into a directory twice.
        extents[entry.sector] = entry
        if entry.attr & _DIR:
            table_bytes += entry.size
            if entry.size > _MAX_TABLE or table_bytes > _MAX_TABLE_BYTES:
                raise ValueError('XDVDFS directory metadata exceeds safe bounds')
            stream.seek(base + start)
            table = _read(stream, entry.size)
            entry.first_child = len(entries)
            for child in _children(table, index, cancel):
                if len(entries) >= _MAX_ENTRIES:
                    raise ValueError('Too many XDVDFS directory entries')
                entries.append(child)
            entry.end_child = len(entries)
    end = 33
    for sector in sorted(extents):
        if sector < end:
            raise ValueError('Overlapping XDVDFS extents')
        end = sector + _ceil(extents[sector].size, _SECTOR)
    if xbe is None or xbe.size < 0x178:
        raise ValueError('Missing original Xbox root default.xbe')
    stream.seek(base + xbe.sector * _SECTOR)
    if _read(stream, 4) != b'XBEH':
        raise ValueError('Invalid original Xbox default.xbe signature')
    return header, entries, extents


def _source(stream, cancel):
    size = os.fstat(stream.fileno()).st_size
    if size % _SECTOR:
        raise ValueError('Xbox image is not sector-aligned')
    for base in (0, *_XBOX360, _XGD1):
        stream.seek(base + _DESCRIPTOR)
        if stream.read(len(_MAGIC)) != _MAGIC:
            continue
        if base in _XBOX360:
            raise ValueError('Xbox 360 game images are not supported')
        if base == 0:
            _image(stream, 0, size, cancel)
            raise ValueError('Already a standalone Xbox XISO; nothing to convert')
        if size < base + _GAME_BYTES:
            raise ValueError('Incomplete Xbox XGD1 game partition')
        return _image(stream, base, _GAME_BYTES, cancel)
    raise ValueError('Not an original Xbox XGD1 disc image')


def validate(source: Path, system: str) -> None:
    if system != 'xbox':
        raise ValueError('XISO conversion is only for original Xbox images')
    with source.open('rb') as stream:
        _source(stream, None)


class _Progress:
    def __init__(self, stage, callback, total):
        self.stage, self.callback, self.total = stage, callback, total
        self.done = self.percent = 0
        if callback:
            callback(stage, 0)

    def advance(self, count):
        if self.callback:
            self.done += count
            percent = self.done * 100 // self.total
            if percent != self.percent:
                self.percent = percent
                self.callback(self.stage, percent)


def _pack(inp, out, header, entries, extents, relocated, cancel, progress):
    descriptor = bytearray(header)
    struct.pack_into('<I', descriptor, 20, relocated[entries[0].sector])
    out.seek(_DESCRIPTOR)
    out.write(descriptor)
    for old in relocated:
        _cancelled(cancel)
        e = extents[old]
        inp.seek(_XGD1 + old * _SECTOR)
        out.seek(relocated[old] * _SECTOR)
        if e.attr & _DIR:
            table = bytearray(_read(inp, e.size))
            for i in range(e.first_child, e.end_child):
                child = entries[i]
                struct.pack_into('<I', table, child.slot + 4, relocated[child.sector] if child.size else 0)
            out.write(table)
        else:
            left = e.size
            while left:
                _cancelled(cancel)
                block = _read(inp, min(_CHUNK, left))
                out.write(block)
                left -= len(block)
                progress.advance(len(block))


def _verify(inp, out, entries, relocated, cancel, progress):
    _, actual, _ = _image(out, 0, os.fstat(out.fileno()).st_size, cancel)
    if len(entries) != len(actual):
        raise ValueError('XISO verification: directory entry count changed')
    checked = set()
    for a, b in zip(entries, actual):
        _cancelled(cancel)
        if ((a.parent, a.name, a.attr, a.size) != (b.parent, b.name, b.attr, b.size)
                or b.sector != (relocated[a.sector] if a.size else 0)):
            raise ValueError('XISO verification: directory entry changed')
        if a.attr & _DIR or not a.size or a.sector in checked:
            continue
        checked.add(a.sector)
        inp.seek(_XGD1 + a.sector * _SECTOR)
        out.seek(b.sector * _SECTOR)
        left = a.size
        while left:
            _cancelled(cancel)
            count = min(left, _CHUNK)
            if _read(inp, count) != _read(out, count):
                raise ValueError('XISO verification: file data changed')
            left -= count
            progress.advance(count)


def convert(source: Path, target: Path, *, cancel, threads: int, on_stage=None) -> None:
    """Create one new compact XISO and compare its parsed tree and file bytes."""
    # Single-threaded streaming; never consume all of a worker's CPU allowance.
    _cancelled(cancel)
    created = False
    try:
        with source.open('rb') as inp:
            before = os.fstat(inp.fileno())
            header, entries, extents = _source(inp, cancel)
            relocated = {}
            next_sector = 34
            for sector in sorted(extents):
                relocated[sector] = next_sector
                next_sector += _ceil(extents[sector].size, _SECTOR)
            image_size = _ceil(next_sector * _SECTOR, 0x10000) * 0x10000
            payload = sum(e.size for e in extents.values() if not e.attr & _DIR)
            with target.open('xb') as out:
                created = True
                progress = _Progress('compressing', on_stage, payload)
                _pack(inp, out, header, entries, extents, relocated, cancel, progress)
                out.truncate(image_size)
            progress = _Progress('verifying', on_stage, payload)
            with target.open('rb') as out:
                if os.fstat(out.fileno()).st_size != image_size:
                    raise ValueError('XISO verification: wrong image length')
                _verify(inp, out, entries, relocated, cancel, progress)
            _cancelled(cancel)
            after = source.stat()
            if (not os.path.samestat(before, after) or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns):
                raise ValueError('Source changed during XISO conversion')
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
