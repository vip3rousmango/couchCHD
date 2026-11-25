import hashlib
import os
import struct
import subprocess
import threading
from pathlib import Path

import pytest

from couchchd import rvz


def write_disc(path: Path, system='gc') -> Path:
    """Small homebrew filesystem/boot image; no commercial data, signatures or keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    disc = bytearray(1024 * 1024)
    wii = system == 'wii'
    base = 0x70000 if wii else 0
    def word(at, value):
        struct.pack_into('>I', disc, at, value)
    def header(at):
        disc[at:at + 6] = b'RTST01' if wii else b'GTST01'
        word(at + (0x18 if wii else 0x1c), 0x5D1C9EA3 if wii else 0xC2339F3D)
        disc[at + 0x20:at + 0x25] = b'Probe'
    header(0)
    if wii:
        # A single plaintext/unhashed development partition. The Wii disc
        # header, partition table, partition header and boot header are distinct.
        disc[0x60:0x62] = b'\x01\x01'
        struct.pack_into('>4I', disc, 0x40000, 1, 0x40020 // 4, 0, 0)
        struct.pack_into('>2I', disc, 0x40020, 0x50000 // 4, 0)
        word(0x50000, 0x10001)  # ticket type, not signing material
        struct.pack_into('>2I', disc, 0x502a4, 0x208, 0x2c0 // 4)
        word(0x502c0, 0x10001)  # unsigned, structurally sized title metadata
        struct.pack_into('>QQ', disc, 0x502c0 + 0x184, 0x100000009, 0x1000052545354)
        struct.pack_into('>H', disc, 0x502c0 + 0x1de, 1)
        struct.pack_into('>2I', disc, 0x502b8, (base - 0x50000) // 4, (len(disc) - base) // 4)
        header(base)
    unit = 4 if wii else 1
    struct.pack_into('>4I', disc, base + 0x420, 0x3000 // unit, 0x4000 // unit, 32 // unit, 32 // unit)
    disc[base + 0x2440:base + 0x244a] = b'2001/01/01'
    word(base + 0x3000, 0x100)
    word(base + 0x3048, 0x80003100)
    word(base + 0x3090, 4)
    word(base + 0x30e0, 0x80003100)
    word(base + 0x3100, 0x4e800020)  # PowerPC blr
    struct.pack_into('>6I', disc, base + 0x4000, 1 << 24, 0, 2, 0, 0x5000 // unit, 5)
    disc[base + 0x4018:base + 0x401e] = b'hello\x00'
    disc[base + 0x5000:base + 0x5005] = b'hello'
    # Bytes outside the filesystem distinguish real lossless encoding from scrubbing.
    disc[-8192:] = bytes(range(256)) * 32
    path.write_bytes(disc)
    return path


@pytest.fixture
def dolphin():
    try:
        return rvz.tool_path()
    except FileNotFoundError:
        if 'COUCHCHD_DOLPHIN_TOOL' in os.environ:
            raise
        pytest.skip('DolphinTool not installed (release packaging exercises the real helper)')


@pytest.mark.parametrize(('system', 'ext'), [('gc', 'gcm'), ('gc', 'iso'), ('wii', 'iso')])
def test_headers(tmp_path, system, ext):
    image = write_disc(tmp_path / f'disc.{ext}', system)
    rvz.validate(image, system)
    with pytest.raises(ValueError, match='expected'):
        rvz.validate(image, 'gc' if system == 'wii' else 'wii')


@pytest.mark.parametrize(('system', 'offset', 'data'), [
    ('gc', 0x1c, bytes(4)), ('gc', 0x18, bytes.fromhex('5d1c9ea3')),
    ('gc', 0x200, b'NKIT'), ('gc', 0x424, bytes.fromhex('ffffffff')),
    ('gc', 0x4000, bytes(4)), ('wii', 0x40000, bytes(4)),
    ('wii', 0x40000, b'\xff' * 4), ('wii', 0x502bc, b'\xff' * 4),
    ('wii', 0x502ac, b'\xff' * 4),
])
def test_bad_headers(tmp_path, system, offset, data):
    image = write_disc(tmp_path / 'disc.iso', system)
    with image.open('r+b') as file:
        file.seek(offset)
        file.write(data)
    with pytest.raises(ValueError):
        rvz.validate(image, system)


@pytest.mark.parametrize('system', ['gc', 'wii'])
def test_real_lossless_roundtrip(tmp_path, dolphin, system):
    image = write_disc(tmp_path / 'raw disc.iso', system)
    target, decoded = tmp_path / 'compressed.rvz', tmp_path / 'extracted.iso'
    stages = []
    before = hashlib.sha256(image.read_bytes()).digest()
    rvz.convert(image, target, threads=1, cancel=threading.Event(), on_stage=lambda *args: stages.append(args))
    # Additional independent comparison: reconstruct a real ISO with the CLI and
    # check every byte, including the deliberately nonzero unreferenced sectors.
    subprocess.run([dolphin, 'convert', '-u', tmp_path / 'fresh-user', '-i', target, '-o', decoded,
                    '-f', 'iso'], check=True, timeout=60)
    assert before == hashlib.sha256(decoded.read_bytes()).digest() == hashlib.sha256(image.read_bytes()).digest()
    assert stages[0][0] == 'compressing' and stages[-1] == ('verifying', 100)
    assert target.stat().st_size < image.stat().st_size


def test_real_digest_disagreement_never_succeeds(tmp_path, dolphin):
    image = write_disc(tmp_path / 'source.iso')
    target = tmp_path / 'result.rvz'
    def change_original(stage, pct):
        if stage == 'verifying' and pct == 0:
            with image.open('r+b') as file:
                file.seek(-1, 2)
                file.write(b'X')
    with pytest.raises(ValueError, match='differs from original'):
        rvz.convert(image, target, cancel=threading.Event(), threads=1, on_stage=change_original)
    assert image.exists() and not target.exists()


def test_cancel_while_real_process_is_live(tmp_path, dolphin, monkeypatch):
    image = write_disc(tmp_path / 'disc.iso')
    original = image.read_bytes()
    cancel = threading.Event()
    real_popen = subprocess.Popen
    def launch_then_cancel(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        cancel.set()
        return proc
    monkeypatch.setattr(rvz.subprocess, 'Popen', launch_then_cancel)
    target = tmp_path / 'aborted.rvz'
    with pytest.raises(InterruptedError):
        rvz.convert(image, target, cancel=cancel, threads=1)
    assert image.read_bytes() == original and list(tmp_path.iterdir()) == [image]


def test_existing_target_untouched(tmp_path, dolphin):
    image = write_disc(tmp_path / 'disc.iso')
    target = tmp_path / 'existing.rvz'
    target.write_bytes(b'existing')
    with pytest.raises(FileExistsError):
        rvz.convert(image, target, cancel=threading.Event(), threads=1)
    assert target.read_bytes() == b'existing'
