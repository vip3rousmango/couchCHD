import hashlib
import os
import random
import shutil
import struct
import subprocess
import threading
from zlib import crc32

import pytest

from couchchd import archive7z


def ines(banks=1):
    return b'NES\x1a' + bytes([banks]) + bytes(11) + random.Random(7).randbytes(16384 * banks)


def convert(source, target, callback=None, cancel=None):
    archive7z.convert(source, target, cancel=cancel or threading.Event(), threads=1, on_stage=callback)


@pytest.mark.parametrize('name', ['@input.nes', '-mx=0 (日本語).NES'])
def test_one_literal_member_external_reader(tmp_path, name):
    tar = shutil.which('bsdtar')
    if not tar:
        pytest.skip('libarchive-tools required for independent format interoperability test')
    source = tmp_path / name
    source.write_bytes(ines(8))
    (tmp_path / 'Nintendo sibling.nes').write_bytes(ines())
    (tmp_path / 'saves').mkdir()
    (tmp_path / 'saves' / 'sram').write_bytes(b'save data')
    target = tmp_path / 'archive.7z'
    stages = []
    convert(source, target, lambda name, percent: stages.append((name, percent)))
    assert subprocess.check_output([tar, '-tf', target]).decode().splitlines() == [name]
    assert subprocess.check_output([tar, '-xOf', target]) == source.read_bytes() == ines(8)
    assert stages[0] == ('compressing', 0)
    assert ('compressing', 100) in stages and ('verifying', 0) in stages
    assert stages[-1] == ('verifying', 100)


def test_compressible_cartridge_shrinks(tmp_path):
    source = tmp_path / 'demo.nes'
    source.write_bytes(b'NES\x1a' + bytes([8]) + bytes(11 + 8 * 16384))
    target = tmp_path / 'demo.7z'
    convert(source, target)
    assert target.stat().st_size < source.stat().st_size // 2


@pytest.mark.parametrize('stage', ['compressing', 'verifying'])
def test_cancel_cleans_only_own_output(tmp_path, stage):
    source = tmp_path / 'demo.nes'
    target = tmp_path / 'demo.7z'
    source.write_bytes(ines(6))
    cancel = threading.Event()

    def stop(name, percent):
        if name == stage:
            cancel.set()

    with pytest.raises(InterruptedError):
        convert(source, target, stop, cancel)
    assert not target.exists() and source.read_bytes() == ines(6)
    target.write_bytes(b'already exists')
    with pytest.raises(FileExistsError):
        convert(source, target)
    assert target.read_bytes() == b'already exists'


def test_wrong_sha_rejected(tmp_path):
    source = tmp_path / 'demo.nes'
    original = ines(4)
    source.write_bytes(original)
    target = tmp_path / 'demo.7z'
    convert(source, target)
    with pytest.raises(ValueError, match='SHA256'):
        archive7z._verify(target, source.name, len(original), crc32(original),
                          hashlib.sha256(original[:-1]).digest(), threading.Event(), None)


@pytest.mark.parametrize('tamper', ['member', 'count', 'tail'])
def test_reject_wrong_archive_even_with_repaired_checksums(tmp_path, tamper):
    source = tmp_path / 'demo.nes'
    source.write_bytes(ines())
    target = tmp_path / 'demo.7z'

    def change(stage, percent):
        if (stage, percent) != ('verifying', 0):
            return
        archive = bytearray(target.read_bytes())
        offset, length, _ = struct.unpack_from('<QQI', archive, 12)
        header = archive[32 + offset:]
        if tamper == 'tail':
            target.write_bytes(archive + b'unlisted bytes')
            return
        if tamper == 'member':
            header[header.index('demo'.encode('utf-16le'))] = ord('x')
        else:
            header[header.index(b'\x05\x01\x11') + 1] = 2
        archive[32 + offset:] = header
        struct.pack_into('<QQI', archive, 12, offset, length, crc32(header))
        struct.pack_into('<I', archive, 8, crc32(archive[12:32]))
        target.write_bytes(archive)

    with pytest.raises(ValueError, match='7z'):
        convert(source, target, change)
    assert not target.exists() and source.read_bytes() == ines()


def test_headered_systems(tmp_path):
    snes = bytearray(32768)
    snes[0x7fd5], snes[0x7ffd] = 0x20, 0x80
    gb = bytearray(32768)
    gb[0x14d] = 231  # checksum over title through ROM version
    gba = bytearray(256)
    gba[0xb2], gba[0xbd] = 0x96, 0x51
    sms = bytearray(8192)
    sms[0x1ff0:0x1ff8] = b'TMR SEGA'
    for system, ext, data in (
        ('nes', '.nes', ines()), ('snes', '.smc', bytes(512) + snes), ('snes', '.sfc', snes),
        ('gb', '.gb', gb), ('gbc', '.gbc', gb), ('gba', '.gba', gba),
        ('n64', '.z64', b'\x80\x37\x12\x40' + bytes(4092)),
        ('megadrive', '.md', bytes(256) + b'SEGA' + bytes(252)),
        ('mastersystem', '.sms', sms), ('gamegear', '.gg', sms),
    ):
        source = tmp_path / ('title' + ext)
        source.write_bytes(data)
        archive7z.validate(source, system)
        source.write_bytes(bytes(len(data)))
        with pytest.raises(ValueError, match='cartridge'):
            archive7z.validate(source, system)


@pytest.mark.parametrize(('name', 'system', 'data'), [
    ('demo.nes', 'nes', b'NES\x1a' + bytes([2]) + bytes(11 + 16384)),
    ('demo.nes', 'nes', b'PK\x03\x04' + bytes(32768)),
    ('demo.nes', 'nes', b''), ('bios (USA).nes', 'nes', ines()),
    ('unknown.bin', 'nes', ines()), ('demo.nes', 'arcade', ines()),
    ('demo.7z', 'nes', ines()),
])
def test_reject_unsupported_cartridges(tmp_path, name, system, data):
    source = tmp_path / name
    source.write_bytes(data)
    with pytest.raises(ValueError):
        archive7z.validate(source, system)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX permits backslashes in filenames')
def test_reject_embedded_path_and_symlink(tmp_path):
    source = tmp_path / '..\\outside.nes'
    source.write_bytes(ines())
    with pytest.raises(ValueError, match='Unsafe'):
        archive7z.validate(source, 'nes')
    link = tmp_path / 'link.nes'
    link.symlink_to(source)
    with pytest.raises(ValueError, match='regular'):
        archive7z.validate(link, 'nes')
