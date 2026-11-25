"""Disc ZIPs retain one outer owner; discovery reads metadata, not track payloads."""
import io
import os
import stat
import struct
import time
import zipfile
from pathlib import Path

import pytest

from couchchd import disc_zip, scanner


def _iso():
    return bytes(32768) + b'\x01CD001\x01' + bytes(2048 - 7 + 2048 * 80)


def _zip(path, entries, method=zipfile.ZIP_DEFLATED):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w', compression=method) as out:
        for name, data in entries:
            out.writestr(name, data)
    then = time.time() - 60
    os.utime(path, (then, then))
    return path.resolve()


@pytest.mark.parametrize('method', [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_zip_one_owner_two_passes_mixed_folder(tmp_path, method):
    iso = _iso()
    archive = _zip(tmp_path / 'psp/Display Name.ZIP', [('a/different.ISO', iso)], method)
    loose = archive.with_name('Another.iso')
    loose.write_bytes(iso)
    os.utime(loose, (0, 0))
    games, issues = scanner.scan(tmp_path)
    assert not issues and [g.source for g in games] == [loose, archive]
    game = scanner.select_format(games[1], 'cso')
    assert game.files == (archive,) and len(game.stamps) == 1
    assert game.stamps[0][2] == archive.stat().st_size and game.size == len(iso)
    assert game.frames == (len(iso) // 2048,)
    assert scanner.output_path(game.source, game.mode) == archive.with_suffix('.cso')
    assert not scanner.is_input_name(archive, 'psp')  # remote ingest remains raw-only
    for stride in (3333, 65536):
        with disc_zip.open_member(game, game.archive.entry) as stream:
            assert b''.join(iter(lambda: stream.read(stride), b'')) == iso
    with pytest.raises(ValueError, match='Incomplete'):
        with disc_zip.open_member(game, game.archive.entry) as stream:
            stream.read(8)
    with pytest.raises(ValueError, match='Unrecognized'):
        with disc_zip.open_member(game, Path('../elsewhere')):
            pass


def test_forced_zip64_local_header(tmp_path):
    path = tmp_path / 'One.zip'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        with archive.open('One.iso', 'w', force_zip64=True) as member:
            member.write(_iso())
    game = scanner.inspect(path, 'psp', fresh=False)
    with disc_zip.open_member(game, game.archive.entry) as stream:
        assert stream.read(game.size) == _iso()


def test_scan_reads_no_track_payload(tmp_path, monkeypatch):
    cue = b'FILE "tracks/DEMO.bin" BINARY\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n'
    path = _zip(tmp_path / 'psx/Outer.zip', [('Disc/disc.cue', cue), ('Disc/Tracks/Demo.BIN', bytes(2352 * 7))])
    reads = []
    original = disc_zip._Reader.read

    def traced(reader, size):
        reads.append((reader.member.path, size))
        return original(reader, size)

    monkeypatch.setattr(disc_zip._Reader, 'read', traced)
    games, issues = scanner.scan(tmp_path)
    assert not issues and len(games) == 1 and reads == [(Path('Disc/disc.cue'), len(cue))]
    game = games[0]
    assert game.files == (path,) and game.frames == (7,) and game.mode == 'createcd'
    assert game.sheet[0].path == Path('Disc/Tracks/Demo.BIN')
    assert game.size == len(cue) + 2352 * 7


def test_archive_gdi_shared_member_extents(tmp_path):
    gdi = b'2\n1 0 4 2048 "Track.bin" 0\n2 1 4 2048 "Track.bin" 2048\n'
    path = _zip(tmp_path / 'dreamcast/Outer.zip', [('disc.gdi', gdi), ('Track.bin', bytes(4096))])
    game = scanner.inspect(path, 'dreamcast')
    assert game.frames == (1, 1)
    assert [(line.offset, line.length) for line in game.sheet[1:]] == [(0, 2048), (2048, 2048)]


@pytest.mark.parametrize(('extra', 'reason'), [
    ('../track.bin', 'Unsafe'), ('folder/../a.bin', 'Unsafe'), ('/a.bin', 'Unsafe'),
    ('C:/a.bin', 'Unsafe'), ('folder\\file', 'Unsafe'), ('CON.bin', 'Unsafe'),
    ('image.ISO', 'Colliding'), ('image.iso/more.bin', 'Colliding'), ('.hidden', 'Hidden'),
    ('other.zip', 'extra files'), ('second.iso', 'exactly one'), ('bios/track.bin', 'excluded'),
])
def test_bad_layout_is_not_a_job(tmp_path, extra, reason):
    path = _zip(tmp_path / 'psp/Game.zip', [('image.iso', _iso()), (extra, b'1')])
    games, issues = scanner.scan(tmp_path)
    assert not games and len(issues) == 1 and issues[0].path == path and reason in issues[0].reason


def test_normalized_directory_and_symlink_rejected(tmp_path):
    path = _zip(tmp_path / 'psp/Game.zip', [('Démo/image.iso', _iso()), ('De\u0301mo/track.bin', b'1')])
    with pytest.raises(ValueError, match='Colliding'):
        scanner.inspect(path, 'psp')
    link = zipfile.ZipInfo('game.iso')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    _zip(path, [(link, b'/somewhere')])
    with pytest.raises(ValueError, match='Linked'):
        scanner.inspect(path, 'psp')


@pytest.mark.parametrize('ref', ['lost.bin', '../external.bin'])
def test_descriptor_must_close_over_its_archive(tmp_path, ref):
    cue = f'FILE "{ref}" BINARY\nTRACK 01 AUDIO\nINDEX 01 00:00:00'.encode()
    path = _zip(tmp_path / 'saturn/Game.zip', [('game.cue', cue)])
    with pytest.raises(ValueError, match='Missing ZIP track|Unsafe'):
        scanner.inspect(path, 'saturn')


def test_bounds_and_encryption_checked_before_payload(tmp_path, monkeypatch):
    path = _zip(tmp_path / 'psp/Game.zip', [('one.iso', _iso())])
    with monkeypatch.context() as m:
        m.setattr(disc_zip, 'MAX_BYTES', len(_iso()) - 1)
        with pytest.raises(ValueError, match='expanded disc'):
            scanner.inspect(path, 'psp')
    with monkeypatch.context() as m:
        m.setattr(disc_zip, 'MAX_ENTRIES', 0)
        with pytest.raises(ValueError, match='entries'):
            scanner.inspect(path, 'psp')
    raw = bytearray(path.read_bytes())
    central = raw.index(b'PK\x01\x02')
    raw[central + 8] |= 1
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='Encrypted'):
        scanner.inspect(path, 'psp', fresh=False)
    end = raw.rindex(b'PK\x05\x06')
    struct.pack_into('<I', raw, end + 12, 0x7fffffff)
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='central directory'):
        scanner.inspect(path, 'psp', fresh=False)


def test_payload_crc_truncation_and_stamp_checked(tmp_path):
    path = _zip(tmp_path / 'psp/Game.zip', [('one.iso', _iso())], zipfile.ZIP_STORED)
    game = scanner.inspect(path, 'psp')
    m = game.archive.members[0]
    with path.open('r+b') as out:
        out.seek(m.offset + m.size - 1)
        out.write(b'x')
    with pytest.raises(ValueError, match='changed'):
        with disc_zip.open_member(game, m.path):
            pass
    game = scanner.inspect(path, 'psp', fresh=False)  # bounded header still valid
    with pytest.raises(ValueError, match='CRC'):
        with disc_zip.open_member(game, m.path) as stream:
            stream.read(m.size)
    _zip(path, [('one.iso', _iso())])
    raw = bytearray(path.read_bytes())
    central = raw.index(b'PK\x01\x02')
    packed, = struct.unpack_from('<I', raw, central + 20)
    struct.pack_into('<I', raw, central + 20, packed - 1)
    struct.pack_into('<I', raw, 18, packed - 1)
    path.write_bytes(raw)
    game = scanner.inspect(path, 'psp', fresh=False)
    with pytest.raises(ValueError, match='Truncated'):
        with disc_zip.open_member(game, game.archive.entry) as stream:
            stream.read(game.size)


def test_playable_archives_are_not_inspected(tmp_path):
    for path in ('nes/Playable.zip', 'arcade/psp/Arcade.zip', 'neogeocd/neocdz.zip'):
        _zip(tmp_path / path, [('nested.iso', b'not a disc')])
    assert scanner.scan(tmp_path) == ([], [])


def test_zip64_end_record_and_gc_header_discovery(tmp_path):
    disc = bytearray(0x3000)
    disc[0x1c:0x20] = bytes.fromhex('c2339f3d')
    struct.pack_into('>III', disc, 0x420, 0, 0x2460, 12)
    struct.pack_into('>III', disc, 0x2460, 0x01000000, 0, 1)
    path = _zip(tmp_path / 'gc/Game.zip', [('Internal.gcm', disc)])
    data = path.read_bytes()
    end = data.rindex(b'PK\x05\x06')
    size, offset = struct.unpack_from('<II', data, end + 12)
    end64 = struct.pack('<4sQHHIIQQQQ', b'PK\x06\x06', 44, 45, 45, 0, 0, 1, 1, size, offset)
    locator = struct.pack('<4sIQI', b'PK\x06\x07', 0, end, 1)
    legacy = struct.pack('<4s4H2IH', b'PK\x05\x06', 0, 0, 65535, 65535, 0xffffffff, 0xffffffff, 0)
    path.write_bytes(data[:end] + end64 + locator + legacy)
    game = scanner.inspect(path, 'gc', fresh=False)
    assert game.mode == 'rvz' and game.archive.entry == Path('Internal.gcm')
    with disc_zip.open_member(game, game.archive.entry) as stream:
        assert stream.read(game.size) == disc


def test_gdi_padding_cannot_exceed_output_limit(tmp_path):
    gdi = b'2\n1 0 4 2048 a.bin 0\n2 10000000 4 2048 b.bin 0\n'
    path = _zip(tmp_path / 'dreamcast/Huge.zip', [('disc.gdi', gdi), ('a.bin', bytes(2048)), ('b.bin', bytes(2048))])
    with pytest.raises(ValueError, match='track geometry'):
        scanner.inspect(path, 'dreamcast')


@pytest.mark.parametrize('zip64', [False, True])
def test_stream_writer_data_descriptor(tmp_path, zip64):
    class Pipe(io.BytesIO):
        def seek(self, *args):
            raise io.UnsupportedOperation

    output = Pipe()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        with package.open('disc.iso', 'w', force_zip64=zip64) as member:
            member.write(_iso())
    path = tmp_path / 'Stream.zip'
    path.write_bytes(output.getvalue())
    game = scanner.inspect(path, 'psp', fresh=False)
    with disc_zip.open_member(game, game.archive.entry) as stream:
        assert stream.read(game.size) == _iso()
