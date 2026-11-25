"""Generated sparse XGD1 media: no game assets, BIOS or private keys."""
import os
import struct
import threading
from pathlib import Path

import pytest

from couchchd import xiso


SECTOR = 2048
BASE = 0x18300000
MAGIC = b'MICROSOFT*XBOX*MEDIA'
ROOT = 300
ASSETS = 520
EXEC = 901
DATA = 1800
TITLE = 1900
EMPTY = 1901
TRACK = bytes(range(251)) * 27
XBE = bytearray(0x180)
XBE[:4] = b'XBEH'
struct.pack_into('<IIII', XBE, 0x104, 0x10000, 0x180, 0x1000, 0x178)


def _table(*nodes):
    table = bytearray(b'\xff' * SECTOR)
    for pos, left, right, sector, length, attr, name in nodes:
        struct.pack_into('<HHIIBB', table, pos, left // 4, right // 4, sector, length, attr, len(name))
        table[pos+14:pos+14+len(name)] = name
    return table


def write_disc(path: Path) -> Path:
    """Shared integration fixture; sparse file, not a buffer of 7+ GiB."""
    descriptor = bytearray(SECTOR)
    descriptor[:20] = descriptor[-20:] = MAGIC
    struct.pack_into('<IIQ', descriptor, 20, ROOT, SECTOR, 0x0123456789abcdef)
    root = _table((0, 32, 64, EXEC, len(XBE), 0x20, b'Default.xbe'),
                  (32, 0, 0, ASSETS, SECTOR, 0x10, b'Assets'),
                  (64, 0, 0, TITLE, 4, 1, b'title.txt'))
    assets = _table((0, 32, 64, DATA, len(TRACK), 0x20, b'm\xfcsic.dat'),
                    (32, 0, 0, EMPTY, SECTOR, 0x10, b'empty'),
                    (64, 0, 0, 0, 0, 0x80, b'zero'))
    with path.open('xb') as out:
        out.write(b'video partition data, not present in XISO')
        for sector, content in [(32, descriptor), (ROOT, root), (ASSETS, assets),
                                (EXEC, XBE), (DATA, TRACK), (TITLE, b'Test'),
                                (EMPTY, b'\xff' * SECTOR)]:
            out.seek(BASE + sector * SECTOR)
            out.write(content)
        out.truncate(0x1D26A8000)  # Retail Redump XGD1 logical size.
    return path


def _change(path, pos, data):
    with path.open('r+b') as stream:
        stream.seek(pos)
        stream.write(data)


def _inspect_image(path):
    """Read expected paths by their actual directory pointers, not converter state."""
    with path.open('rb') as stream:
        def read_at(pos, size):
            stream.seek(pos)
            return stream.read(size)

        def record(sector, dword_offset=0):
            pos = sector * SECTOR + dword_offset * 4
            left, right, sector, size, attr, n = struct.unpack('<HHIIBB', read_at(pos, 14))
            return left, right, sector, size, attr, read_at(pos + 14, n)

        header = read_at(0x10000, SECTOR)
        assert header[:20] == header[-20:] == MAGIC
        root, size, timestamp = struct.unpack_from('<IIQ', header, 20)
        assert size == SECTOR and timestamp == 0x0123456789abcdef
        left, right, sector, size, attr, name = record(root)
        assert (name, attr, read_at(sector * SECTOR, size)) == (b'Default.xbe', 0x20, XBE)
        _, _, sector, size, attr, name = record(root, right)
        assert (name, attr, read_at(sector * SECTOR, size)) == (b'title.txt', 1, b'Test')
        _, _, assets, _, attr, name = record(root, left)
        assert (name, attr) == (b'Assets', 0x10)
        left, right, sector, size, attr, name = record(assets)
        assert (name, attr, read_at(sector * SECTOR, size)) == (b'm\xfcsic.dat', 0x20, TRACK)
        _, _, sector, size, attr, name = record(assets, left)
        assert (name, attr, read_at(sector * SECTOR, size)) == (b'empty', 0x10, b'\xff' * SECTOR)
        assert record(assets, right)[2:] == (0, 0, 0x80, b'zero')


def test_repack_verified_and_deterministic(tmp_path):
    source = write_disc(tmp_path / 'Example.iso')
    stamp = source.stat()
    target = tmp_path / 'Example.xiso.iso'
    stages = []
    xiso.validate(source, 'xbox')
    xiso.convert(source, target, cancel=threading.Event(), threads=1,
                 on_stage=lambda stage, percent=None: stages.append((stage, percent)))
    _inspect_image(target)
    assert ('compressing', 0) in stages and ('verifying', 100) in stages
    assert target.stat().st_size < BASE and target.stat().st_size % 0x10000 == 0
    assert b'video partition' not in target.read_bytes()
    second = tmp_path / 'second.iso'
    xiso.convert(source, second, cancel=threading.Event(), threads=4)
    assert second.read_bytes() == target.read_bytes()
    assert source.stat().st_size == stamp.st_size and source.stat().st_mtime_ns == stamp.st_mtime_ns
    with pytest.raises(ValueError, match='Already.*XISO'):
        xiso.validate(second, 'xbox')
    with pytest.raises(FileExistsError):
        xiso.convert(source, source, cancel=threading.Event(), threads=1)
    assert source.stat().st_mtime_ns == stamp.st_mtime_ns


@pytest.mark.parametrize(('offset', 'new', 'reason'), [
    (32 * SECTOR + SECTOR - 1, b'!', 'descriptor'),
    (ROOT * SECTOR + 32, struct.pack('<H', 8), 'cyclic'),
    (ROOT * SECTOR + 32 + 4, struct.pack('<I', ROOT), 'cyclic'),
    (ROOT * SECTOR, struct.pack('<H', 60000), 'link'),
    (ROOT * SECTOR, b'\0\0', 'unreachable|Unreachable'),
    (ROOT * SECTOR + 32 + 14, b'../bad', 'filename'),
    (ROOT * SECTOR + 64 + 4, struct.pack('<I', 0x1A2DB0000 // SECTOR), 'outside'),
    (ROOT * SECTOR + 64 + 4, struct.pack('<I', DATA + 1), 'Overlapping'),
    (ROOT * SECTOR + 64 + 4, struct.pack('<I', 32), 'reserved'),
    (ROOT * SECTOR + 14, b'Default.xex', 'Xbox 360'),
    (EXEC * SECTOR, b'XEX2', 'signature'),
])
def test_refuses_malformed_or_wrong_console(tmp_path, offset, new, reason):
    source = write_disc(tmp_path / 'broken.iso')
    _change(source, BASE + offset, new)
    with pytest.raises(ValueError, match=reason):
        xiso.validate(source, 'xbox')


@pytest.mark.parametrize('base', [0xFD90000, 0x02080000, 0x89D80000])
def test_excludes_other_xgd_layouts(tmp_path, base):
    source = write_disc(tmp_path / 'other.iso')
    _change(source, base + 32 * SECTOR, MAGIC)
    with pytest.raises(ValueError, match='Xbox 360'):
        xiso.validate(source, 'xbox')


def test_truncated_disc_and_wrong_system(tmp_path):
    source = write_disc(tmp_path / 'part.iso')
    os.truncate(source, BASE + (EMPTY + 1) * SECTOR)
    with pytest.raises(ValueError, match='Incomplete'):
        xiso.validate(source, 'xbox')
    with pytest.raises(ValueError, match='original Xbox'):
        xiso.validate(source, 'psp')


def test_file_alias_is_preserved(tmp_path):
    source = write_disc(tmp_path / 'alias.iso')
    # Two distinct names for the same immutable payload.
    _change(source, BASE + ROOT * SECTOR + 64 + 4, struct.pack('<II', DATA, len(TRACK)))
    target = tmp_path / 'out.iso'
    xiso.convert(source, target, threads=1, cancel=threading.Event())
    data = target.read_bytes()
    assert data.count(TRACK) == 1
    root = struct.unpack_from('<I', data, 0x10014)[0] * SECTOR
    assets = struct.unpack_from('<I', data, root + 32 + 4)[0] * SECTOR
    assert data[root+64+4:root+64+12] == data[assets+4:assets+12]


@pytest.mark.parametrize('action', ['payload', 'name', 'cancel'])
def test_verification_and_cancellation_discard_only_candidate(tmp_path, action):
    source = write_disc(tmp_path / 'original.iso')
    target = tmp_path / 'candidate.xiso.iso'
    cancel = threading.Event()
    stamp = source.stat()

    def intervene(stage, percent=None):
        if action == 'cancel' and stage == 'compressing' and percent:
            cancel.set()
        if action != 'cancel' and stage == 'verifying' and percent == 0:
            data = target.read_bytes()
            pos = data.index(TRACK if action == 'payload' else b'title.txt')
            _change(target, pos, b'Z' if action == 'payload' else b'T')

    with pytest.raises(InterruptedError if action == 'cancel' else ValueError,
                       match='cancelled|file data changed|directory entry changed'):
        xiso.convert(source, target, cancel=cancel, threads=1, on_stage=intervene)
    assert source.exists() and source.stat().st_mtime_ns == stamp.st_mtime_ns
    assert not target.exists()
