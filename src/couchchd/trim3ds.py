"""Trim a full CTR CARD1 image; its header and erased (0xff) fill reconstruct it exactly."""
from __future__ import annotations

import os
import stat
import struct
import time
from pathlib import Path


_CHUNK = 1024 * 1024
_U32 = struct.Struct('<I')


def _source(source: Path, system: str) -> None:
    if (system != '3ds' or source.suffix.lower() not in {'.3ds', '.cci'}
            or source.name.lower().endswith('.trim.3ds') or not stat.S_ISREG(source.lstat().st_mode)):
        raise ValueError('Trimming requires an original regular 3DS/CCI cartridge image')


def _layout(f, *, trimmed: bool) -> tuple[int, int]:
    # NCSD and CARD info header: https://www.3dbrew.org/wiki/NCSD
    f.seek(0)
    header = f.read(0x308)
    if (len(header) != 0x308 or header[0x100:0x104] != b'NCSD'
            or not any(header[0x108:0x110]) or any(header[0x110:0x120])
            or header[0x18c] != 1):
        raise ValueError('Not a CTR game cartridge NCSD (NAND and non-cartridge images are unsupported)')
    if header[0x18d] != 1 or header[0x200:0x204] != b'\xff' * 4:
        # Even an all-FF save region can be allocated for future saves. Never
        # treat the writable part of a CARD2 image as removable padding.
        raise ValueError('Only CARD1 with no writable save area can be trimmed safely')
    if any(header[0x180:0x188]):
        raise ValueError('Unsupported extended NCSD header layout')
    unit = 512 << header[0x18e]
    capacity = _U32.unpack_from(header, 0x104)[0] * unit
    actual = os.fstat(f.fileno()).st_size
    if not trimmed and actual != capacity:
        raise ValueError('Expected the full untrimmed NCSD media capacity')

    # Preserve card information, development data, gaps, and EVERY partition,
    # including manual, Download Play, and update partitions in late slots.
    end = 0x4000
    for i, (offset, length) in enumerate(struct.iter_unpack('<II', header[0x120:0x160])):
        if not length:
            if offset or i == 0:
                raise ValueError('Empty or inconsistent NCSD partition entry')
            continue
        start, span = offset * unit, length * unit
        if start < end or start + span > min(actual, capacity):
            raise ValueError('Overlapping or out-of-bounds NCSD partition')
        f.seek(start + 0x100)
        ncch = f.read(0x100)
        if len(ncch) != 0x100 or ncch[:4] != b'NCCH':
            raise ValueError('NCSD partition is missing its NCCH header')
        ncch_size = _U32.unpack_from(ncch, 4)[0] * (512 << ncch[0x8e])
        if not 512 <= ncch_size <= span:
            raise ValueError('NCCH content size exceeds or contradicts its partition')
        end = start + span

    # Some mastering tools declare extra used bytes beyond the last NCCH.
    # Never discard them, even if they happen to be all FF. A zero field is
    # unspecified; no assumption about the remaining bytes is made here.
    filled, = struct.unpack_from('<Q', header, 0x300)
    if filled:
        if not end <= filled <= capacity:
            raise ValueError('Inconsistent used cartridge size')
        end = (filled + unit - 1) // unit * unit
    if end >= capacity:
        raise ValueError('NCSD image has no removable tail')
    if trimmed and actual != end:
        raise ValueError('Trimmed image does not end at its declared used extent')
    return end, capacity


def validate(source: Path, system: str) -> None:
    """Bounded preflight for discovery. Conversion checks every byte of the tail."""
    _source(source, system)
    with source.open('rb') as f:
        _layout(f, trimmed=False)


def _chunks(total, stage, cancel, on_stage):
    last = float('-inf')
    for off in range(0, total, _CHUNK):
        now = time.monotonic()
        if on_stage and now - last >= 1:
            on_stage(stage, 100 * off / total)
            last = now
        if cancel.is_set():
            raise InterruptedError('3DS trimming cancelled')
        yield off, min(_CHUNK, total - off)
    if on_stage:
        on_stage(stage, 100.0)
    if cancel.is_set():
        raise InterruptedError('3DS trimming cancelled')


def _verify(source, target, cancel, on_stage):
    with source.open('rb') as original, target.open('rb') as candidate:
        end, total = _layout(candidate, trimmed=True)
        if (end, total) != _layout(original, trimmed=False):
            raise ValueError('Trimmed cartridge geometry differs from original')
        candidate.seek(0)
        original.seek(0)
        for off, size in _chunks(total, 'verifying', cancel, on_stage):
            kept = max(0, min(size, end - off))
            if kept:
                data = candidate.read(kept)
                if len(data) != kept or data != original.read(kept):
                    raise ValueError('Trimmed cartridge data differs from original')
            tail = size - kept
            if tail and original.read(tail).count(b'\xff') != tail:
                raise ValueError('Trailing bytes are not reconstructable erased cartridge padding (0xff)')
        if candidate.read(1) or original.read(1):
            raise ValueError('Cartridge size changed during verification')


def convert(source: Path, target: Path, *, cancel, threads: int, on_stage=None) -> None:
    """Copy without decryption or header edits, then verify the entire reconstructed cartridge."""
    _source(source, '3ds')
    if not target.name.lower().endswith('.trim.3ds'):
        raise ValueError('Trimmed cartridge output must have a distinct .trim.3ds suffix')
    if cancel.is_set():
        raise InterruptedError('3DS trimming cancelled')
    with source.open('rb') as original:
        end, _ = _layout(original, trimmed=False)
        original.seek(0)
        out = target.open('xb')
        try:
            with out:
                for _, size in _chunks(end, 'compressing', cancel, on_stage):
                    data = original.read(size)
                    if len(data) != size:
                        raise ValueError('Truncated cartridge during copy')
                    out.write(data)
            _verify(source, target, cancel, on_stage)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
