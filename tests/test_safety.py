"""Safety contracts: originals survive all failures; independently count source sectors."""
import errno
import os
import threading
import time
from pathlib import Path

import pytest

from couchchd import converter, scanner


def aged(path, data):
    path.write_bytes(data)
    t = time.time() - 60
    os.utime(path, (t, t))
    return path


@pytest.fixture
def disc(tmp_path):
    system = tmp_path.resolve()/'ps1'
    system.mkdir()
    folder = system/'Tracks'
    folder.mkdir()
    binary = aged(folder/'Démo.bin', bytes(range(256)) * 147)  # 16 raw sectors
    sheet = aged(system/'Title (USA).cue', ('FILE "tracks/DÉMO.BIN" BINARY\n'
                ' TRACK 01 MODE2/2352\n  INDEX 01 00:00:00\n TRACK 02 AUDIO\n'
                '  INDEX 00 00:00:06\n  INDEX 01 00:00:08\n').encode('cp1252'))
    return sheet, binary


def test_case_resolution_pregaps_and_shared_ownership(disc, tmp_path):
    cue, binary = disc
    other = aged(cue.with_name('Other.cue'), b'FILE "Tracks/D\xe9mo.bin" BINARY\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n')
    games, issues = scanner.scan(tmp_path)
    assert not issues
    assert {g.source for g in games} == {cue, other}
    game = next(g for g in games if g.source == cue)
    assert (game.system, game.mode, game.files, game.frames) == ('psx', 'createcd', (cue, binary), (6, 10))
    assert game.size == cue.stat().st_size + binary.stat().st_size
    cue.unlink()
    assert not scanner.unreferenced(binary, tmp_path)
    other.write_text('FILE "oops.bin" BINARY\n')
    assert not scanner.unreferenced(binary, tmp_path)  # even an invalid sheet protects data
    other.unlink()
    assert scanner.unreferenced(binary, tmp_path)


def test_sjis_remote_listing_disambiguates_encoding(tmp_path):
    cue = aged(tmp_path/'j.cue', 'FILE "テスト.bin" BINARY\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n'.encode('shift_jis'))
    binary = aged(tmp_path/'テスト.bin', bytes(2352))
    assert scanner.references(cue, available=lambda p: p.name == binary.name) == (Path(binary.name),)
    assert scanner.inspect(cue, 'saturn').files[-1] == binary.resolve()


def test_partial_transfers_archives_and_unsafe_paths(disc, tmp_path):
    cue, binary = disc
    archive = aged(cue.with_suffix('.7z'), b'not a disc')
    part = aged(cue.with_suffix('.crdownload'), b'pending')
    os.utime(binary, None)
    games, issues = scanner.scan(tmp_path)
    assert not games
    assert {i.path: i.category for i in issues} == {cue: 'active_transfer', part: 'active_transfer', archive: 'needs_unpacking'}
    assert scanner.inspect(cue, 'psx', fresh=False).frames == (6, 10)
    for ref in ('../outside.bin', '/etc/a.bin', r'C:\\a.bin', 'folder/../../a.bin', 'disc:stream.bin'):
        aged(cue, f'FILE "{ref}" BINARY\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n'.encode())
        with pytest.raises(ValueError, match='Unsafe'):
            scanner.references(cue)


def test_symlinks_ambiguity_and_truncation(disc):
    cue, binary = disc
    binary.rename(binary.with_name('outside.bin'))
    try:
        binary.symlink_to(binary.with_name('outside.bin'))
    except OSError:
        pytest.skip('Symlinks not available')
    with pytest.raises(ValueError, match='Symlink'):
        scanner.inspect(cue, 'ps1')
    binary.unlink()
    aged(binary, bytes(2351))
    with pytest.raises(ValueError):
        scanner.inspect(cue, 'ps1')
    other = binary.with_name('déMo.bin')
    if not other.exists():
        aged(other, bytes(2352))
        with pytest.raises(ValueError, match='Ambiguous'):
            scanner.inspect(cue, 'ps1')


def test_gdi_lba_padding_spaces_offsets_and_overlap(tmp_path):
    gdi = aged(tmp_path/'demo.gdi', b'3\n1 0 4 2352 track 1.bin 0\n2 6 0 2352 "track two.raw" 2\n3 45000 4 2048 last.img 0\n')
    aged(tmp_path/'track 1.bin', bytes(2352 * 4))
    aged(tmp_path/'track two.raw', b'xx' + bytes(2352 * 8))
    aged(tmp_path/'last.img', bytes(2048 * 10))
    game = scanner.inspect(gdi, 'dreamcast')
    assert game.frames == (6, 44994, 10)  # CHGD FRAMES includes synthesized LBA gaps.
    assert scanner.references(gdi) == (Path('track 1.bin'), Path('track two.raw'), Path('last.img'))
    aged(gdi, gdi.read_bytes().replace(b'2 6 0', b'2 3 0'))
    with pytest.raises(ValueError, match='Overlapping'):
        scanner.inspect(gdi, 'dreamcast')


def test_system_specific_iso_headers_and_threshold(tmp_path):
    image = tmp_path/'test.iso'
    aged(image, bytes(16 * 2048) + b'\x01CD001\x01' + bytes(2048-7))
    assert scanner.inspect(image, 'psx').frames == (17,)
    assert scanner.inspect(image, 'psp').mode == 'createdvd'
    with pytest.raises(ValueError, match='threshold'):
        scanner.inspect(image, 'ps2')
    with pytest.raises(ValueError, match='Unsupported'):
        scanner.inspect(image, 'dreamcast')
    with image.open('r+b') as f:
        f.truncate(700 * 1024 * 1024)  # sparse, does not consume a DVD's worth of disk
    assert scanner.inspect(image, 'ps2', fresh=False).mode == 'createdvd'
    aged(image, bytes(17 * 2048))
    with pytest.raises(ValueError, match='volume descriptor'):
        scanner.inspect(image, 'psp')


@pytest.fixture
def tool(tmp_path):
    # Exercise the actual subprocess boundary, including MAME's zero-status checksum
    # errors. No mock of the scanner, verification gate, or filesystem transactions.
    if os.name == 'nt':
        pytest.skip('Script executable fixture requires POSIX')
    script = tmp_path/'chdman'
    script.write_text('''#!/usr/bin/env python3
import os, sys, time, pathlib, re
from pathlib import Path
cmd, *args = sys.argv[1:]
mode = os.environ.get('TEST_CHDMAN_MODE', '')
print('chdman - MAME Compressed Hunks of Data (CHD) manager', '0.255' if mode == 'old' else '0.267')
if cmd == 'help': sys.exit(1)
if cmd.startswith('create'):
    source = Path(args[args.index('-i')+1])
    output = Path(args[args.index('-o')+1])
    if cmd == 'createcd':
        assert '-hs' not in args
        if source.suffix.lower() == '.cue':
            text = source.read_text(encoding='utf-8')
            assert 'DÉMO.BIN' not in text
            ref = re.search('FILE "([^"]+)"', text)[1]
            assert (source.parent/ref).is_file()
    else:
        assert args[args.index('-hs')+1] == '2048'
    output.write_bytes(b'' if mode == 'empty' else b'verified test image')
    if mode == 'enospc': print('No space left on device'); sys.exit(1)
    if mode == 'cancel':
        Path(os.environ['TEST_CHILD_PID']).write_text(str(os.getpid()))
        time.sleep(60)
if cmd == 'verify':
    if mode == 'verify': print('Error: Raw SHA1 in header = 123'); sys.exit(0)
    print('Raw SHA1 verification successful!')
    if mode != 'metadata_sha': print('Overall SHA1 verification successful!')
if cmd == 'info':
    if mode == 'race': Path(os.environ['TEST_TARGET']).write_bytes(b'other process')
    if mode.startswith('dvd'):
        print("Metadata:     Tag='DVD '  Index=0  Length=1 bytes")
        print('Logical size: 34,816 bytes\\nHunk Size: 2,048 bytes\\nUnit Size: 2,048 bytes')
        print('Total Units:', '18' if mode == 'dvd_wrong' else '17')
    else:
        for index,frames in enumerate((5,11) if mode == 'geometry' else (6,10)):
            print(f"Metadata:     Tag='CHT2'  Index={index}  Length=96 bytes")
            print(f'              TRACK:{index+1} TYPE:AUDIO SUBTYPE:NONE FRAMES:{frames} PREGAP:0')
if cmd == 'extractcd':
    Path('disc.cue').write_text('FILE "disc.bin" BINARY\\nTRACK 01 AUDIO\\nINDEX 01 00:00:00\\nTRACK 02 AUDIO\\nINDEX 01 00:00:06\\n')
    Path('disc.bin').write_bytes(bytes(2352*16))
''', encoding='utf-8')
    script.chmod(0o700)
    return script


def run(game, output, tool, cancel=None):
    converter.convert(game, output, tool, cancel=cancel or threading.Event(), threads=2)


@pytest.mark.parametrize('fault', ['old', 'empty', 'enospc', 'verify', 'metadata_sha', 'geometry'])
def test_failed_stage_never_publishes_or_changes_inputs(disc, tool, monkeypatch, fault):
    cue, binary = disc
    before = [p.read_bytes() for p in disc]
    game = scanner.inspect(cue, 'ps1')
    monkeypatch.setenv('TEST_CHDMAN_MODE', fault)
    target = cue.with_suffix('.chd')
    cancel = threading.Event()
    with pytest.raises((RuntimeError, OSError, ValueError)) as error:
        run(game, target, tool, cancel)
    if fault == 'enospc':
        assert error.value.errno == errno.ENOSPC and cancel.is_set()
    assert not target.exists() and not list(cue.parent.rglob('*.part'))
    assert [p.read_bytes() for p in disc] == before


def test_publish_extract_and_never_overwrite(disc, tool, tmp_path):
    cue, binary = disc
    before = cue.read_bytes()
    target = cue.with_suffix('.chd')
    run(scanner.inspect(cue, 'psx'), target, tool)
    assert target.read_bytes() == b'verified test image'
    assert cue.read_bytes() == before
    with pytest.raises(FileExistsError):
        run(scanner.inspect(cue, 'psx'), target, tool)
    dest = tmp_path/'extracted'
    extracted = converter.extract(target, dest, tool)
    assert extracted == dest/cue.name
    assert f'FILE "{cue.stem}.bin" BINARY' in extracted.read_text()
    assert scanner.inspect(extracted, 'psx', fresh=False).frames == (6, 10)
    extracted.unlink()
    blocker = dest/(cue.stem + '.bin')
    blocker.write_bytes(b'keep this')
    with pytest.raises(FileExistsError):
        converter.extract(target, dest, tool)
    assert blocker.read_bytes() == b'keep this' and not extracted.exists()


def test_destination_appears_during_conversion(disc, tool, monkeypatch):
    cue, _ = disc
    target = cue.with_suffix('.chd')
    monkeypatch.setenv('TEST_CHDMAN_MODE', 'race')
    monkeypatch.setenv('TEST_TARGET', str(target))
    with pytest.raises(FileExistsError):
        run(scanner.inspect(cue, 'psx'), target, tool)
    assert target.read_bytes() == b'other process'
    assert not list(cue.parent.rglob('*.part'))


def test_raw_sector_iso_uses_header_not_file_size(tmp_path):
    image = bytearray(2352 * 128)  # also exactly divisible by 2048
    start = 16 * 2352
    image[start:start+12] = b'\x00' + b'\xff' * 10 + b'\x00'
    image[start+15] = 2
    image[start+24:start+31] = b'\x01CD001\x01'
    source = aged(tmp_path/'raw.iso', image)
    assert scanner.inspect(source, 'psx').frames == (128,)
    with pytest.raises(ValueError):
        scanner.inspect(source, 'psp')


def test_dvd_sector_comparison(tmp_path, tool, monkeypatch):
    source = aged(tmp_path/'disc.iso', bytes(16 * 2048) + b'\x01CD001\x01' + bytes(2048-7))
    game = scanner.inspect(source, 'psp')
    target = source.with_suffix('.chd')
    monkeypatch.setenv('TEST_CHDMAN_MODE', 'dvd_wrong')
    with pytest.raises(ValueError, match='DVD metadata'):
        run(game, target, tool)
    monkeypatch.setenv('TEST_CHDMAN_MODE', 'dvd')
    run(game, target, tool)
    assert target.is_file()


def test_cancel_reaps_process(disc, tmp_path, tool, monkeypatch):
    cue, _ = disc
    target = cue.with_suffix('.chd')
    pidfile = tmp_path/'child-pid'
    monkeypatch.setenv('TEST_CHDMAN_MODE', 'cancel')
    monkeypatch.setenv('TEST_CHILD_PID', str(pidfile))
    cancel = threading.Event()
    def stop_when_started():
        end = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < end:
            time.sleep(.01)
        cancel.set()
    thread = threading.Thread(target=stop_when_started)
    thread.start()
    try:
        with pytest.raises(InterruptedError):
            run(scanner.inspect(cue, 'psx'), target, tool, cancel)
    finally:
        cancel.set()
        thread.join()
    assert not target.exists() and not list(cue.parent.rglob('*.part'))
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_changed_inputs_and_precise_crash_recovery(disc, tmp_path, tool):
    cue, binary = disc
    game = scanner.inspect(cue, 'psx')
    binary.write_bytes(b'other input')
    with pytest.raises(ValueError, match='changed'):
        run(game, cue.with_suffix('.chd'), tool)
    temp = cue.parent/'.couchchd_tmp'
    temp.mkdir()
    stale = aged(temp/'orphan.chd.part', b'incomplete')
    live = aged(cue.parent/'downloading.chd.part', b'not ours')
    unrelated = aged(temp/'notes.txt', b'keep')
    converter.recover(tmp_path)
    assert not stale.exists() and live.exists() and unrelated.exists()


def test_real_chdman_audio_roundtrip_if_installed(tmp_path):
    try:
        tool = converter.tool_path()
    except FileNotFoundError:
        pytest.skip('Install or bundle chdman for a real codec round trip')
    cue = aged(tmp_path/'sound.cue', b'FILE "SOUND.BIN" BINARY\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n')
    payload = bytes(range(256)) * 588  # 64 aligned audio frames
    aged(tmp_path/'sound.bin', payload)
    target = tmp_path/'sound.chd'
    run(scanner.inspect(cue, 'segacd'), target, tool)
    restored = converter.extract(target, tmp_path/'restored', tool)
    assert restored.with_suffix('.bin').read_bytes() == payload
