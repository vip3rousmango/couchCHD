"""Single-cartridge 7z archives, using the same native LZMA2 codec as Python's lzma.

Only emit one simple 7z profile: one packed stream, one LZMA2 coder, one
nonempty regular file, no external names, timestamps, paths or attributes.
Container layout: https://github.com/ip7z/7zip/blob/main/DOC/7zFormat.txt
"""
from __future__ import annotations

import hashlib
import lzma
import os
import re
import stat
import struct
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from zlib import crc32


FORMATS = {
    'nes': frozenset({'.nes'}), 'snes': frozenset({'.sfc', '.smc'}),
    'gb': frozenset({'.gb'}), 'gbc': frozenset({'.gbc'}), 'gba': frozenset({'.gba'}),
    'n64': frozenset({'.z64', '.n64', '.v64'}), 'megadrive': frozenset({'.md', '.gen'}),
    'mastersystem': frozenset({'.sms'}), 'gamegear': frozenset({'.gg'}),
}
_SYSTEM_FOR_SUFFIX = {ext: system for system, exts in FORMATS.items() for ext in exts}
_SIGNATURE = b'7z\xbc\xaf\x27\x1c\x00\x04'
_DICT_SIZE = 1 << 20  # LZMA2 coder property 16. Single CPU, also on low-memory devices.
_CHUNK = 64 * 1024


def _nes_size(low: int, high: int, unit: int) -> int:
    # NES 2.0 also supports exponent/multiplier sizes, e.g. 24 KiB ROMs.
    if high == 15:
        return (2 * (low & 3) + 1) << (low >> 2)
    return ((high << 8) | low) * unit


def _cartridge(file, size: int, system: str) -> bool:
    h = file.read(512)
    if len(h) != min(size, 512):
        return False
    if h.startswith((b'PK\x03\x04', b'PK\x05\x06', b'Rar!\x1a\x07', _SIGNATURE[:6],
                     b'\x1f\x8b', b'BZh', b'\xfd7zXZ\x00', b'MComprHD')):
        return False
    if system == 'nes':
        if len(h) < 16 or h[:4] != b'NES\x1a':
            return False
        high = h[9] if (h[7] & 12) == 8 else 0
        prg = _nes_size(h[4], high & 15, 16384)
        chr_size = _nes_size(h[5], high >> 4, 8192)
        return prg > 0 and size >= 16 + (512 if h[6] & 4 else 0) + prg + chr_size
    if system in ('gb', 'gbc'):
        return size >= 0x4000 and h[0x14d] == (-sum(h[0x134:0x14d]) - 25) & 255
    if system == 'gba':
        return size >= 0xc0 and h[0xb2] == 0x96 and h[0xbd] == (-sum(h[0xa0:0xbd]) - 0x19) & 255
    if system == 'n64':
        return size >= 4096 and size % 4 == 0 and h[:4] in (
            b'\x80\x37\x12\x40', b'\x37\x80\x40\x12', b'\x40\x12\x37\x80')
    if system == 'megadrive':
        return len(h) == 512 and h[0x100:0x104] == b'SEGA'
    if system in ('mastersystem', 'gamegear'):
        for offset in (0x1ff0, 0x3ff0, 0x7ff0):
            if size >= offset + 16:
                file.seek(offset)
                if file.read(8) == b'TMR SEGA':
                    return True
        return False
    # SNES internal header at the canonical LoROM, HiROM, or extended HiROM
    # location, optionally shifted by a 512-byte copier prefix. Do not depend
    # on the cartridge checksum (many homebrew or patched ROMs don't set it).
    copier = size % 0x8000
    if copier not in (0, 512):
        return False
    for offset in (0x7fc0, 0xffc0, 0x40ffc0, 0x407fc0):
        if size >= copier + offset + 64:
            file.seek(copier + offset)
            hdr = file.read(64)
            if (len(hdr) == 64 and hdr[0x3d] & 0x80
                    and hdr[0x15] in (0x20, 0x21, 0x22, 0x23, 0x25, 0x30, 0x31, 0x32, 0x35, 0x3a)):
                return True
    return False


@contextmanager
def _open_rom(source: Path, system: str):
    from .scanner import safe_relative_name  # scanner discovers formats from this module
    if source.suffix.lower() not in FORMATS.get(system, ()):
        raise ValueError(f'Unsupported cartridge system/extension: {system}/{source.suffix}')
    safe_relative_name(source.name)
    source.name.encode('utf-16le')  # Reject undecodable/surrogate filesystem names.
    if re.search(r'(?:^|[^a-z0-9])(?:bios|firmware|bootrom|ipl)(?:$|[^a-z0-9])', source.stem, re.I):
        raise ValueError(f'Not archiving firmware: {source}')
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or not info.st_size:
        raise ValueError(f'Not a nonempty regular cartridge file: {source}')
    # A rename between inspection and open must not redirect to a link or
    # block the worker on a FIFO. Validate and compress from this same fd.
    flags = getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with open(source, 'rb', opener=lambda path, mode: os.open(path, mode | flags)) as file:
        if not os.path.samestat(info, os.fstat(file.fileno())):
            raise ValueError(f'Cartridge changed while opening: {source}')
        if not _cartridge(file, info.st_size, system):
            raise ValueError(f'Unrecognized or incomplete {system} cartridge: {source}')
        file.seek(0)
        yield file, info.st_size


def validate(source: Path, system: str) -> None:
    with _open_rom(source, system):
        pass


def _uint(n: int) -> bytes:
    """7z's variable-length unsigned integer (not LEB128)."""
    for extra in range(8):
        if n < 1 << (7 * (extra + 1)):
            prefix = (256 - (256 >> extra)) | (n >> (8 * extra))
            return bytes([prefix]) + (n & ((1 << (8 * extra)) - 1)).to_bytes(extra, 'little')
    return b'\xff' + n.to_bytes(8, 'little')


def _header(name: str, packed: int, size: int, crc: int) -> bytes:
    filename = b'\0' + name.encode('utf-16le') + b'\0\0'  # inline, NUL-terminated
    return b''.join((
        b'\x01\x04',                                   # Header, MainStreamsInfo
        b'\x06\x00\x01\x09', _uint(packed), b'\x00',    # PackInfo: one stream at offset zero
        b'\x07\x0b\x01\x00',                           # UnpackInfo: one inline folder
        b'\x01\x21\x21\x01\x10',                       # one coder, LZMA2, 1 MiB dictionary
        b'\x0c', _uint(size), b'\x0a\x01', struct.pack('<I', crc), b'\x00',
        b'\x08\x00\x00',                               # default single substream, end streams
        b'\x05\x01\x11', _uint(len(filename)), filename, b'\x00\x00',  # FilesInfo, end header
    ))


def _check(cancel: Event) -> None:
    if cancel.is_set():
        raise InterruptedError('Conversion cancelled; originals retained')


class _Stage:
    def __init__(self, name, callback, cancel, total):
        self.name, self.callback, self.cancel, self.total = name, callback, cancel, total
        self.next = 0.0
        self.update(0)

    def update(self, done):
        _check(self.cancel)
        if self.callback is not None and (done == self.total or time.monotonic() >= self.next):
            self.callback(self.name, 100 * done / self.total)
            self.next = time.monotonic() + 1
            _check(self.cancel)


def _pack(name: str, file, out, size: int, cancel, on_stage):
    progress = _Stage('compressing', on_stage, cancel, size)
    digest = hashlib.sha256()
    encoder = lzma.LZMACompressor(format=lzma.FORMAT_RAW, filters=[
        {'id': lzma.FILTER_LZMA2, 'preset': 1, 'dict_size': _DICT_SIZE}])
    out.seek(32)  # write/backpatch the signature header only when all extents are known
    done = crc = 0
    while chunk := file.read(_CHUNK):
        done += len(chunk)
        if done > size:
            raise ValueError('Cartridge grew during compression')
        digest.update(chunk)
        crc = crc32(chunk, crc)
        out.write(encoder.compress(chunk))
        progress.update(done)
    if done != size:
        raise ValueError('Cartridge shrank during compression')
    out.write(encoder.flush())
    packed = out.tell() - 32
    header = _header(name, packed, size, crc)
    out.write(header)
    start = struct.pack('<QQI', packed, len(header), crc32(header))
    out.seek(0)
    out.write(_SIGNATURE + struct.pack('<I', crc32(start)) + start)
    return crc, digest.digest()


def _verify(target, name, size, crc, wanted, cancel, on_stage):
    progress = _Stage('verifying', on_stage, cancel, size)
    with target.open('rb') as file:
        signature = file.read(32)
        if (len(signature) != 32 or signature[:8] != _SIGNATURE
                or crc32(signature[12:]) != struct.unpack_from('<I', signature, 8)[0]):
            raise ValueError('Invalid 7z signature or start header checksum')
        packed, length, header_crc = struct.unpack_from('<QQI', signature, 12)
        expected = _header(name, packed, size, crc)
        if length != len(expected) or file.seek(0, os.SEEK_END) != 32 + packed + length:
            raise ValueError('Invalid 7z archive extent')
        file.seek(32 + packed)
        header = file.read(length)
        # Exact match excludes external streams, duplicate entries, directory
        # attributes and any substitution of the source basename or coder.
        if header != expected or crc32(header) != header_crc:
            raise ValueError('7z metadata does not match the single source ROM')
        file.seek(32)
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[
            {'id': lzma.FILTER_LZMA2, 'dict_size': _DICT_SIZE}])
        digest = hashlib.sha256()
        remaining, done = packed, 0
        while not decoder.eof:
            if decoder.needs_input:
                chunk = file.read(min(_CHUNK, remaining))
                if not chunk:
                    raise ValueError('Truncated 7z packed stream')
                remaining -= len(chunk)
            else:
                chunk = b''
            try:
                data = decoder.decompress(chunk, max_length=_CHUNK)
            except lzma.LZMAError as exc:
                raise ValueError('Invalid LZMA2 data in the 7z packed stream') from exc
            done += len(data)
            if done > size:
                raise ValueError('7z member exceeds original size')
            digest.update(data)
            progress.update(done)
        if remaining or decoder.unused_data or done != size or digest.digest() != wanted:
            raise ValueError('7z member failed SHA256 roundtrip or extent check')


def convert(source: Path, target: Path, *, cancel: Event, threads: int, on_stage=None) -> None:
    """Encode on one worker (never exceed threads); remove private output on failure."""
    _check(cancel)
    if threads < 1:
        raise ValueError('7z requires a positive CPU budget')
    with _open_rom(source, _SYSTEM_FOR_SUFFIX.get(source.suffix.lower(), '')) as (file, size):
        out = target.open('xb')
        try:
            with out:
                crc, digest = _pack(source.name, file, out, size, cancel, on_stage)
            _verify(target, source.name, size, crc, digest, cancel, on_stage)
            _check(cancel)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
