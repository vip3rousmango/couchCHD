import struct
from threading import Event

import pytest

from couchchd import trim3ds


def _card() -> bytes:
    # A small, structurally valid CARD1 with non-contiguous content and an
    # update in slot 7. All declared content, including gaps, must survive.
    data = bytearray(b'\xff' * 0x10000)
    data[:0x4000] = bytes(0x4000)
    data[0x100:0x104] = b'NCSD'
    struct.pack_into('<IQ', data, 0x104, len(data) // 512, 0x0102030405060708)
    data[0x18c:0x18e] = b'\x01\x01'
    data[0x200:0x204] = b'\xff' * 4
    for i, offset, length in [(0, 32, 8), (7, 49, 2)]:
        struct.pack_into('<II', data, 0x120 + 8 * i, offset, length)
        start = offset * 512
        data[start:start + length * 512] = bytes([i + 1]) * (length * 512)
        struct.pack_into('<4sI', data, start + 0x100, b'NCCH', length)
        data[start + 0x18e] = 0
    data[40 * 512:49 * 512] = b'\x7e' * (9 * 512)
    return bytes(data)


@pytest.mark.parametrize('extension', ['.3ds', '.CCI'])
@pytest.mark.parametrize('filled', [0, 51 * 512, 52 * 512 + 1])
def test_recovers_every_byte_including_update_and_declared_extras(tmp_path, extension, filled):
    source = tmp_path / ('Kart' + extension)
    target = tmp_path / 'Kart.trim.cci'
    data = bytearray(_card())
    struct.pack_into('<Q', data, 0x300, filled)
    source.write_bytes(data)
    trim3ds.validate(source, '3ds')
    progress = []
    trim3ds.convert(source, target, threads=1, cancel=Event(), on_stage=lambda *args: progress.append(args))
    trimmed = target.read_bytes()
    assert len(trimmed) == max(51, (filled + 511) // 512) * 512
    assert trimmed + b'\xff' * (len(data) - len(trimmed)) == data
    assert source.read_bytes() == data
    assert ('compressing', 0.0) in progress and ('verifying', 100.0) in progress
    with pytest.raises(ValueError, match='original'):
        trim3ds.validate(target, '3ds')
    target.rename(tmp_path / 'renamed.3ds')
    with pytest.raises(ValueError, match='full untrimmed'):
        trim3ds.validate(tmp_path / 'renamed.3ds', '3ds')


@pytest.mark.parametrize('offset,field', [
    (0x100, b'NCCH'), (0x108, bytes(8)), (0x110, b'\x01'), (0x118, b'\x01'),
    (0x18c, b'\x00'), (0x18d, b'\x02'), (0x200, struct.pack('<I', 80)),
    (0x18e, b'\x02'), (0x180, b'\x01'), (0x120, bytes(8)),
    (0x120, struct.pack('<I', 31)), (0x128, struct.pack('<II', 42, 0)),
    (0x158, struct.pack('<II', 39, 1)), (0x158, struct.pack('<II', 127, 2)),
    (0x158, struct.pack('<II', 0xffffffff, 4)), (0x300, struct.pack('<Q', 40 * 512)),
    (0x300, struct.pack('<Q', (1 << 32) + 51 * 512)), (49 * 512 + 0x100, b'WRNG'),
    (49 * 512 + 0x104, struct.pack('<I', 3)), (49 * 512 + 0x104, bytes(4)),
    (49 * 512 + 0x18e, b'\x03'),
])
def test_rejects_ambiguous_cartridge_and_partition_bounds(tmp_path, offset, field):
    source = tmp_path / 'Bad.3ds'
    target = tmp_path / 'Bad.trim.cci'
    data = bytearray(_card())
    data[offset:offset + len(field)] = field
    source.write_bytes(data)
    with pytest.raises(ValueError):
        trim3ds.validate(source, '3ds')
    with pytest.raises(ValueError):
        trim3ds.convert(source, target, threads=8, cancel=Event())
    assert not target.exists() and source.read_bytes() == data


@pytest.mark.parametrize('bad', [0, 255 * 1024, 2 * 1024 * 1024 - 51 * 512 - 1])
def test_rejects_non_erased_tail_anywhere_and_cleans_output(tmp_path, bad):
    data = bytearray(_card())
    data += b'\xff' * (2 * 1024 * 1024 - len(data))
    struct.pack_into('<I', data, 0x104, len(data) // 512)
    data[51 * 512 + bad] = 0
    source = tmp_path / 'BadTail.cci'
    source.write_bytes(data)
    target = tmp_path / 'BadTail.trim.cci'
    with pytest.raises(ValueError, match='not reconstructable'):
        trim3ds.convert(source, target, cancel=Event(), threads=1)
    assert not target.exists() and source.read_bytes() == data


def test_never_replaces_files_and_cancellation_removes_partial_output(tmp_path):
    source = tmp_path / 'Title.cci'
    source.write_bytes(_card())
    target = tmp_path / 'Title.trim.cci'
    target.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        trim3ds.convert(source, target, cancel=Event(), threads=1)
    assert target.read_bytes() == b'keep'
    with pytest.raises(ValueError):
        trim3ds.convert(source, source, cancel=Event(), threads=1)
    target.unlink()
    cancel = Event()
    with pytest.raises(InterruptedError):
        trim3ds.convert(source, target, cancel=cancel, threads=1,
                        on_stage=lambda stage, percent: cancel.set() if stage == 'verifying' else None)
    assert not target.exists() and source.read_bytes() == _card()


def test_verification_compares_retained_bytes_not_only_geometry(tmp_path):
    source = tmp_path / 'Title.3ds'
    target = tmp_path / 'Title.trim.cci'
    source.write_bytes(_card())
    trim3ds.convert(source, target, cancel=Event(), threads=1)
    with target.open('r+b') as out:
        out.seek(45 * 512)  # a gap not covered by either NCCH
        out.write(b'!')
    with pytest.raises(ValueError, match='differs from original'):
        trim3ds._verify(source, target, Event(), None)
