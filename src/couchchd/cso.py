"""PSP CISO v1: independent raw-DEFLATE sectors, verified against the input ISO."""
from __future__ import annotations

import os
import stat
import struct
import time
import zlib
from pathlib import Path


_SECTOR = 2048
_BATCH = 512  # at most 1 MiB of input, 2 KiB of indices
_HEADER = struct.Struct('<4sIQIBB2x')
_INDEX = struct.Struct('<I')
_PLAIN = 1 << 31
_MASK = _PLAIN - 1


def _layout(size: int) -> tuple[int, int]:
    sectors = size // _SECTOR
    end_table = _HEADER.size + (sectors + 1) * _INDEX.size
    # Raw sectors are a hard upper bound, including padding: the alignment
    # divides 2048 and we store raw whenever DEFLATE wouldn't save a unit.
    for shift in range(12):
        align = 1 << shift
        first = (end_table + align - 1) & -align
        if (first + size) >> shift <= _MASK:
            return shift, first
    raise ValueError('ISO too large for CSO v1 sector indices')


def _geometry(size: int):
    if size < 17 * _SECTOR or size % _SECTOR:
        raise ValueError('PSP ISO has incomplete or missing 2048-byte sectors')
    return _layout(size)


def _signature(data):
    if len(data) != 7 or data[1:] != b'CD001\x01' or data[0] not in range(4):
        raise ValueError('PSP ISO lacks an ISO 9660 volume descriptor')


def validate(source: Path, system: str) -> None:
    if system != 'psp' or source.suffix.lower() != '.iso' or not stat.S_ISREG(source.lstat().st_mode):
        raise ValueError('CSO requires a regular PSP ISO')
    with source.open('rb') as disc:
        _geometry(os.fstat(disc.fileno()).st_size)
        disc.seek(16 * _SECTOR)
        _signature(disc.read(7))


def _read(stream, count: int) -> bytes:
    data = stream.read(count)
    if len(data) != count:
        raise ValueError('Truncated ISO or CSO')
    return data


def _batches(sectors, stage, cancel, on_stage):
    last = float('-inf')
    for done in range(0, sectors, _BATCH):
        now = time.monotonic()
        if on_stage and now - last >= 1:
            on_stage(stage, 100 * done / sectors)
            last = now
        if cancel.is_set():
            raise InterruptedError('CSO conversion cancelled')
        yield done, min(_BATCH, sectors - done)
    if on_stage:
        on_stage(stage, 100.0)
    if cancel.is_set():
        raise InterruptedError('CSO conversion cancelled')


def _encode(disc, out, size, cancel, on_stage):
    shift, first = _geometry(size)
    align = 1 << shift
    out.write(_HEADER.pack(b'CISO', _HEADER.size, size, _SECTOR, 1, shift))
    out.seek(first)
    for done, count in _batches(size // _SECTOR, 'compressing', cancel, on_stage):
        chunk = memoryview(_read(disc, count * _SECTOR))
        if done == 0:
            _signature(chunk[16 * _SECTOR:16 * _SECTOR + 7])
        entries = bytearray(count * _INDEX.size)
        for i in range(count):
            sector = chunk[i * _SECTOR:(i + 1) * _SECTOR]
            payload = zlib.compress(sector, level=6, wbits=-15)
            pad = -len(payload) % align
            entry = out.tell() >> shift
            if len(payload) + pad >= _SECTOR:
                entry |= _PLAIN
                out.write(sector)
            else:
                out.write(payload)
                if pad:
                    out.write(bytes(pad))
            _INDEX.pack_into(entries, i * _INDEX.size, entry)
        # Backfill the reserved table one batch at a time, not one seek per
        # sector or an in-memory table proportional to the size of the disc.
        end = out.tell()
        out.seek(_HEADER.size + done * _INDEX.size)
        out.write(entries)
        if (done + count) * _SECTOR == size:
            out.write(_INDEX.pack(end >> shift))
        out.seek(end)
    if disc.read(1):
        raise ValueError('ISO grew during encoding')


def _verify(disc, size, target, cancel, on_stage):
    # Decode the on-disk header and two sequential streams (table, payload).
    # Never allocate based on a length from the candidate file.
    with target.open('rb') as index, target.open('rb') as data:
        magic, header_size, total, block, version, shift = _HEADER.unpack(_read(index, _HEADER.size))
        if (magic, header_size, total, block, version) != (b'CISO', _HEADER.size, size, _SECTOR, 1) or shift > 11:
            raise ValueError('Invalid CSO v1 header')
        align = 1 << shift
        sectors = size // _SECTOR
        table_end = header_size + (sectors + 1) * _INDEX.size
        data.seek((table_end + align - 1) & -align)
        entry, = _INDEX.unpack(_read(index, _INDEX.size))
        for _, count in _batches(sectors, 'verifying', cancel, on_stage):
            source_chunk = memoryview(_read(disc, count * _SECTOR))
            following = _read(index, count * _INDEX.size)
            for i, (next_entry,) in enumerate(_INDEX.iter_unpack(following)):
                start = (entry & _MASK) << shift
                span = ((next_entry & _MASK) << shift) - start
                if start != data.tell() or not 0 < span <= _SECTOR:
                    raise ValueError('Invalid CSO sector offsets')
                payload = _read(data, span)
                if entry & _PLAIN:
                    sector = payload
                else:
                    inflater = zlib.decompressobj(-15)
                    try:
                        sector = inflater.decompress(payload, _SECTOR + 1)
                    except zlib.error as exc:
                        raise ValueError('Invalid raw-DEFLATE CSO sector') from exc
                    if not inflater.eof or len(inflater.unused_data) >= align or any(inflater.unused_data):
                        raise ValueError('Invalid CSO sector terminator or alignment padding')
                if sector != source_chunk[i * _SECTOR:(i + 1) * _SECTOR]:
                    raise ValueError('CSO sector differs from original ISO')
                entry = next_entry
        if entry & _PLAIN or data.read(1) or disc.read(1):
            raise ValueError('Invalid CSO end index or trailing data')


def convert_stream(open_disc, size, target, *, cancel, on_stage=None):
    """Two independent sequential reads: encode, then compare every decoded sector."""
    if cancel.is_set():
        raise InterruptedError('CSO conversion cancelled')
    out = target.open('xb')  # cleanup may only remove a file this invocation created
    try:
        with out, open_disc() as disc:
            _encode(disc, out, size, cancel, on_stage)
        with open_disc() as original:
            _verify(original, size, target, cancel, on_stage)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def convert(source: Path, target: Path, *, cancel, threads: int, on_stage=None) -> None:
    """Use one compression worker regardless of the available CPU budget."""
    info = source.lstat()
    if source.suffix.lower() != '.iso' or not stat.S_ISREG(info.st_mode):
        raise ValueError('CSO requires a regular PSP ISO')
    convert_stream(lambda: source.open('rb'), info.st_size, target, cancel=cancel, on_stage=on_stage)
