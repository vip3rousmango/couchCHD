"""Read individual disc members from an immutable, bounded ZIP. Never extract by name."""
from __future__ import annotations

import os
import stat
import struct
import unicodedata
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .scanner import Game

# All supported optical discs fit, including dual-layer media. Do not impose a
# compression ratio limit: real discs can contain gigabytes of genuine zeroes.
MAX_BYTES = 16 << 30
MAX_ENTRIES = 256
MAX_DIRECTORY = 256 << 10
_BLOCK = 64 << 10
_LOCAL = struct.Struct('<4s5H3I2H')


@dataclass(frozen=True, slots=True)
class Member:
    path: Path
    size: int
    crc: int
    packed: int
    offset: int
    method: int


@dataclass(frozen=True, slots=True)
class Archive:
    entry: Path
    members: tuple[Member, ...]


def _identity(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


@contextmanager
def _file(path, stamp):
    def check(st):
        if not stat.S_ISREG(st.st_mode) or _identity(st) != stamp:
            raise ValueError(f'ZIP source changed since scan: {path}')

    if path.resolve(strict=True) != path:
        raise ValueError(f'Linked ZIP source: {path}')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                 | getattr(os, 'O_BINARY', 0))
    with os.fdopen(fd, 'rb') as file:
        check(os.fstat(fd))
        yield file
        check(os.fstat(fd))
        check(path.lstat())
        if path.resolve(strict=True) != path:
            raise ValueError(f'ZIP source changed since scan: {path}')


class _MetadataFile:
    """ZipFile otherwise reads the untrusted EOCD central-directory size in one allocation."""
    def __init__(self, file, size):
        self.file = file
        self.size = size
        self.seek = file.seek
        self.tell = file.tell
        self.seekable = file.seekable

    def read(self, size=-1):
        if size < 0:
            size = self.size - self.tell()
        if not 0 <= size <= MAX_DIRECTORY:
            raise ValueError('ZIP central directory exceeds 256 KiB; repack just one disc')
        return self.file.read(size)


def _key(path):
    return unicodedata.normalize('NFC', path.as_posix()).casefold()


def _local_sizes(extra, raw, packed):
    """Decode only the ZIP64 fields replacing actual 32-bit sentinels."""
    pos = 0
    while pos + 4 <= len(extra):
        kind, length = struct.unpack_from('<HH', extra, pos)
        pos += 4
        if pos + length > len(extra):
            break
        if kind == 1:
            data = memoryview(extra)[pos:pos + length]
            if raw == 0xffffffff:
                raw, = struct.unpack_from('<Q', data)
                data = data[8:]
            if packed == 0xffffffff:
                packed, = struct.unpack_from('<Q', data)
            return raw, packed
        pos += length
    raise ValueError('Missing ZIP64 local size fields')


def _members(file, size):
    from .scanner import safe_relative_name, visible_directory

    members = []
    nodes = {}
    seen = set()
    total = 0
    metadata = _MetadataFile(file, size)
    # Use the same EOCD/ZIP64 decoder as ZipFile. The public constructor doesn't
    # enforce the advertised entry count, split-disk fields or an allocation limit.
    end = zipfile._EndRecData(metadata)
    if (not end or end[zipfile._ECD_DISK_NUMBER] or end[zipfile._ECD_DISK_START]
            or end[zipfile._ECD_ENTRIES_THIS_DISK] != end[zipfile._ECD_ENTRIES_TOTAL]):
        raise ValueError('Not a complete single-volume ZIP; repack just one disc')
    count = end[zipfile._ECD_ENTRIES_TOTAL]
    if not 1 <= count <= MAX_ENTRIES:
        raise ValueError(f'ZIP requires 1–{MAX_ENTRIES} entries; repack just one disc')
    if end[zipfile._ECD_SIZE] > MAX_DIRECTORY:
        raise ValueError('ZIP central directory exceeds 256 KiB; repack just one disc')
    with zipfile.ZipFile(metadata) as archive:
        entries = archive.infolist()
        if len(entries) != count:
            raise ValueError('Inconsistent ZIP central-directory entry count')
        offsets = sorted(i.header_offset for i in entries)
        if offsets[0] < 0 or offsets[-1] >= archive.start_dir or len(set(offsets)) != count:
            raise ValueError('Invalid or overlapping ZIP local headers')
        boundaries = dict(zip(offsets, [*offsets[1:], archive.start_dir]))
        for info in entries:
            name = info.orig_filename
            if info.filename != name or '\0' in name or '\\' in name:
                raise ValueError('Unsafe or ambiguous ZIP member name')
            directory = name.endswith('/')
            path = safe_relative_name(name[:-1] if directory else name)
            if (len(path.parts) > 16 or len(name.encode('utf-8')) > 1024 or
                    any(len(p.encode('utf-8')) > 255 or not visible_directory(p) for p in path.parts)):
                raise ValueError(f'Hidden, excluded or oversized ZIP member path: {name!r}')
            # Implicit directories count too: A/x and a/y would extract differently across hosts.
            for node in (*reversed(path.parents), path):
                if node == Path('.'):
                    continue
                key = _key(node)
                value = node.as_posix(), directory or node != path
                if nodes.setdefault(key, value) != value:
                    raise ValueError(f'Colliding ZIP member path: {name!r}')
            key = _key(path)
            if key in seen:
                raise ValueError(f'Duplicate ZIP member path: {name!r}')
            seen.add(key)
            kind = stat.S_IFMT(info.external_attr >> 16)
            if (kind not in (0, stat.S_IFDIR if directory else stat.S_IFREG) or
                    info.external_attr & 0x408 or info.external_attr & 0x10 and not directory):
                raise ValueError(f'Linked or special ZIP entry: {name!r}')
            if info.volume or info.flag_bits & ~0x080e:
                raise ValueError(f'Encrypted, split or unsupported ZIP entry: {name!r}')
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValueError(f'Repack {name!r} as a stored or deflated ZIP member')
            if (directory and (info.file_size or info.compress_size > 2) or
                    not directory and (not info.file_size or not info.compress_size) or
                    info.compress_type == zipfile.ZIP_STORED and info.compress_size != info.file_size):
                raise ValueError(f'Empty or inconsistent ZIP entry: {name!r}')
            total += info.file_size
            if total > MAX_BYTES:
                raise ValueError('ZIP expanded disc exceeds 16 GiB; repack just one disc')
            # Check the local headers too. Never let one member consume another
            # member's bytes, even on Python versions predating ZipFile's overlap check.
            file.seek(info.header_offset)
            sig, _, flags, method, _, _, crc, packed, raw, nlen, xlen = _LOCAL.unpack(file.read(_LOCAL.size))
            if sig != b'PK\x03\x04' or flags != info.flag_bits or method != info.compress_type:
                raise ValueError(f'Inconsistent ZIP local header: {name!r}')
            offset = file.tell() + nlen + xlen
            end = offset + info.compress_size
            if end > boundaries[info.header_offset]:
                raise ValueError(f'Truncated or overlapping ZIP entry: {name!r}')
            if file.read(nlen) != name.encode('utf-8' if flags & 0x800 else 'cp437'):
                raise ValueError(f'Inconsistent ZIP local filename: {name!r}')
            if flags & 8:
                file.seek(end)
                descriptor = file.read(min(24, boundaries[info.header_offset] - end))
                layouts = ['<IQQ']
                if max(info.compress_size, info.file_size) <= 0xffffffff:
                    layouts.append('<III')
                expected = [struct.pack(fmt, info.CRC, info.compress_size, info.file_size) for fmt in layouts]
                if not any(descriptor.startswith(v) or descriptor.startswith(b'PK\x07\x08' + v) for v in expected):
                    raise ValueError(f'Invalid ZIP data descriptor: {name!r}')
            else:
                if 0xffffffff in (raw, packed):
                    raw, packed = _local_sizes(file.read(xlen), raw, packed)
                if (crc, raw, packed) != (info.CRC, info.file_size, info.compress_size):
                    raise ValueError(f'Inconsistent ZIP member size or CRC: {name!r}')
            if not directory:
                members.append(Member(path, info.file_size, info.CRC, info.compress_size, offset, method))
    return tuple(members)


class _Reader:
    """Size-limited sequential reader. Verify the real DEFLATE end, not only a truncated prefix."""
    def __init__(self, file, member):
        self.file = file
        self.member = member
        self.file.seek(member.offset)
        self.packed_left = member.packed
        self.left = member.size
        self.decoder = zlib.decompressobj(-15) if member.method == zipfile.ZIP_DEFLATED else None
        self.crc = 0

    def _inflate(self, count):
        decoder = self.decoder
        data = decoder.unconsumed_tail
        if not data and self.packed_left:
            if self.packed_left <= self.read_floor:
                raise ValueError(f'Excess compressed input for bounded ZIP read: {self.member.path}')
            data = self.file.read(min(self.packed_left, _BLOCK))
            if not data:
                raise ValueError(f'Truncated ZIP member: {self.member.path}')
            self.packed_left -= len(data)
        try:
            result = decoder.decompress(data, count)
        except zlib.error as exc:
            raise ValueError(f'Invalid DEFLATE stream: {self.member.path}') from exc
        if not result and not data and not decoder.eof:
            raise ValueError(f'Truncated DEFLATE stream: {self.member.path}')
        return result

    def read(self, size):
        if not 0 <= size <= 8 << 20:
            raise ValueError('ZIP member reads require an explicit limit of at most 8 MiB')
        count = min(size, self.left)
        if not count:
            return b''
        # Prevent a tiny header scan (or cancelled worker) from traversing a
        # gigabyte of deliberately empty DEFLATE blocks to obtain the next byte.
        self.read_floor = max(0, self.packed_left - count - 2 * _BLOCK)
        if self.decoder is None:
            block = self.file.read(count)
            self.packed_left -= len(block)
        else:
            block = self._inflate(count)
            if len(block) < count:
                chunks = [block]
                obtained = len(block)
                while obtained < count and not self.decoder.eof:
                    block = self._inflate(count - obtained)
                    chunks.append(block)
                    obtained += len(block)
                block = b''.join(chunks)
        if len(block) != count:
            raise ValueError(f'Truncated ZIP member: {self.member.path}')
        self.left -= count
        self.crc = zlib.crc32(block, self.crc)
        if not self.left:
            self.verify()
        return block

    def verify(self):
        if self.left:
            raise ValueError(f'Incomplete read of ZIP member: {self.member.path}')
        if self.decoder is not None:
            while not self.decoder.eof:
                if self._inflate(1):
                    raise ValueError(f'ZIP member expands beyond its declared size: {self.member.path}')
            if self.decoder.unused_data:
                raise ValueError(f'Trailing data in ZIP member: {self.member.path}')
        if self.packed_left or self.crc != self.member.crc:
            raise ValueError(f'ZIP member CRC or size mismatch: {self.member.path}')


@contextmanager
def catalog(path, stamp):
    """Validate metadata and allow bounded descriptor / disc header reads only."""
    if stamp[2] > MAX_BYTES + (128 << 20):
        raise ValueError('ZIP exceeds the supported single-disc size limit')
    with _file(path, stamp) as file:
        try:
            members = _members(file, stamp[2])
        except (zipfile.BadZipFile, EOFError, OverflowError, NotImplementedError, struct.error) as exc:
            raise ValueError(f'Unreadable ZIP; check or repack the original: {exc}') from exc

        def prefix(member, limit):
            if not 0 <= limit <= 1 << 20:
                raise ValueError('ZIP scan header exceeds 1 MiB')
            return _Reader(file, member).read(limit)

        yield members, prefix


@contextmanager
def open_member(game: Game, member_path: Path):
    """Open an independently reopenable stream. Successful exit requires exact length and CRC.

    Offsets and sizes were validated against the same device/inode/size/mtime/ctime
    at scan. Bind each new descriptor to that stamp before trusting them again.
    Read in bounded chunks; the conversion backend owns SHA/readback verification.
    """
    if game.archive is None:
        raise ValueError('Expected a scanned disc ZIP')
    member = next((m for m in game.archive.members if m.path == member_path), None)
    if member is None:
        raise ValueError(f'Unrecognized ZIP member: {member_path}')
    with _file(game.source, game.stamps[0]) as file:
        reader = _Reader(file, member)
        yield reader
        reader.verify()
