import random
import struct
import zlib
from threading import Event

import pytest

from couchchd import cso


_SECTOR = 2048


def _iso() -> bytes:
    # Both raw and DEFLATE sectors, spanning more than one index-writing batch.
    data = bytearray(533 * _SECTOR)
    noise = random.Random(77).randbytes(_SECTOR)
    data[:_SECTOR] = noise
    data[-_SECTOR:] = noise[::-1]
    data[16 * _SECTOR:16 * _SECTOR + 7] = b'\x01CD001\x01'
    return bytes(data)


def test_v1_stream_decodes_as_iso(tmp_path):
    source = tmp_path / 'Homebrew.ISO'
    target = tmp_path / 'Homebrew.cso'
    original = _iso()
    source.write_bytes(original)
    progress = []
    cso.convert(source, target, threads=1, cancel=Event(), on_stage=lambda *args: progress.append(args))
    blob = target.read_bytes()
    magic, header, size, block, version, shift, reserved = struct.unpack_from('<4sIQIBB2s', blob)
    assert (magic, header, size, block, version, reserved) == (b'CISO', 24, len(original), _SECTOR, 1, b'\0\0')
    entries = struct.unpack_from(f'<{size // block + 1}I', blob, header)
    assert entries[-1] << shift == len(blob) < len(original)
    assert entries[0] & (1 << 31) and not entries[1] & (1 << 31)
    decoded = bytearray()
    for a, b in zip(entries, entries[1:]):
        start, end = (a & 0x7fffffff) << shift, (b & 0x7fffffff) << shift
        assert header + 4 * len(entries) <= start < end <= len(blob)
        sector = blob[start:end] if a >> 31 else zlib.decompress(blob[start:end], -15)
        assert len(sector) == _SECTOR
        decoded.extend(sector)
    assert decoded == original == source.read_bytes()
    assert ('compressing', 0.0) in progress and ('verifying', 100.0) in progress


@pytest.mark.parametrize('size,shift', [(2**31, 1), (2**32, 2), (2**32 + _SECTOR, 2)])
def test_large_iso_preflight_includes_index_size_in_worst_case_bound(tmp_path, size, shift):
    path = tmp_path / 'large.iso'
    with path.open('wb') as f:
        f.seek(16 * _SECTOR)
        f.write(b'\x01CD001\x01')
        f.truncate(size)  # sparse, never read or allocate the entire disc
    cso.validate(path, 'psp')
    actual, start = cso._layout(size)
    assert actual == shift
    assert start >= 24 + 4 * (size // _SECTOR + 1) and start % (1 << shift) == 0
    assert (start + size) >> shift < 1 << 31


def test_unrepresentable_size_rejected():
    with pytest.raises(ValueError, match='too large'):
        cso._layout(1 << 42)


def test_shifted_raw_deflate_and_padding_verified(tmp_path):
    # A hand-assembled two-byte-aligned v1 file exercises shifted decoding
    # without compressing multiple gigabytes in the unit suite.
    original = _iso()[:_SECTOR * 17]
    source = tmp_path / 'Shifted.iso'
    source.write_bytes(original)
    target = tmp_path / 'Shifted.cso'
    sectors = len(original) // _SECTOR
    start = 24 + 4 * (sectors + 1)
    data = bytearray()
    entries = []
    for n in range(sectors):
        sector = original[n * _SECTOR:(n + 1) * _SECTOR]
        compressed = zlib.compress(sector, wbits=-15)
        flag = 0
        if len(compressed) + len(compressed) % 2 >= _SECTOR:
            flag, compressed = 1 << 31, sector
        entries.append((start + len(data)) // 2 | flag)
        data.extend(compressed)
        if len(data) % 2:
            data.append(0)
    entries.append((start + len(data)) // 2)
    target.write_bytes(struct.pack('<4sIQIBB2x', b'CISO', 24, len(original), _SECTOR, 1, 1)
                       + struct.pack(f'<{len(entries)}I', *entries) + data)
    cso._verify(source, target, Event(), None)


@pytest.mark.parametrize('mutation', ['header', 'index-backward', 'index-huge', 'raw', 'deflate', 'end', 'trailing'])
def test_verifier_rejects_corrupt_artifact(tmp_path, mutation):
    source = tmp_path / 'Game.iso'
    target = tmp_path / 'Game.cso'
    source.write_bytes(_iso())
    cso.convert(source, target, threads=1, cancel=Event())
    data = bytearray(target.read_bytes())
    first, second = struct.unpack_from('<II', data, 24)
    if mutation == 'header':
        data[20] = 2
    elif mutation == 'index-backward':
        struct.pack_into('<I', data, 28, 0)
    elif mutation == 'index-huge':
        struct.pack_into('<I', data, 28, 0x7fffffff)
    elif mutation == 'raw':
        data[first & 0x7fffffff] ^= 1
    elif mutation == 'deflate':
        data[second] = 0xff  # impossible DEFLATE block type
    elif mutation == 'end':
        i = 24 + len(_iso()) // _SECTOR * 4
        struct.pack_into('<I', data, i, 0x80000000 | len(data))
    else:
        data.extend(b'extra')
    target.write_bytes(data)
    with pytest.raises(ValueError):
        cso._verify(source, target, Event(), None)


def test_invalid_iso_and_wrong_system_rejected(tmp_path):
    source = tmp_path / 'Game.iso'
    for invalid in (bytes(17 * _SECTOR), _iso()[:-1], b'CISO'):
        source.write_bytes(invalid)
        with pytest.raises(ValueError):
            cso.validate(source, 'psp')
    source.write_bytes(_iso())
    with pytest.raises(ValueError):
        cso.validate(source, 'ps2')


@pytest.mark.parametrize('stage', ['compressing', 'verifying'])
def test_cancel_removes_private_output_but_never_replaces_existing_file(tmp_path, stage):
    original = _iso()
    source = tmp_path / 'Game.iso'
    source.write_bytes(original)
    target = tmp_path / 'Game.cso'
    target.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        cso.convert(source, target, cancel=Event(), threads=1)
    assert target.read_bytes() == b'keep'
    target.unlink()
    cancel = Event()
    with pytest.raises(InterruptedError):
        cso.convert(source, target, threads=1, cancel=cancel,
                    on_stage=lambda current, percent: cancel.set() if stage == current else None)
    assert not target.exists() and source.read_bytes() == original
