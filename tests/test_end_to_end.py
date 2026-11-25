"""A public-interface round trip on generated audio sectors, never user media."""

import os
import json
import shutil
from pathlib import Path
import subprocess

import pytest

from couchchd.app import Backend
from couchchd import cli, hardware
from couchchd.converter import tool_path
from couchchd.locking import library_lock


def disc(folder, title, track='Track.bin'):
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / track
    payload = bytes(range(196)) * 12 * 300  # 300 integral CD-DA sectors
    raw.write_bytes(payload)
    cue = folder / (title + '.cue')
    cue.write_text(f'FILE "{track}" BINARY\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n', encoding='ascii')
    for p in (raw, cue):
        os.utime(p, (1600000000, 1600000000))
    return cue, raw, payload


def test_locked_library_does_not_start_second_scan(tmp_path):
    with library_lock(tmp_path), pytest.raises(RuntimeError, match='lock'):
        Backend(tmp_path).scan()


real_chdman = pytest.mark.skipif(not (os.getenv('COUCHCHD_CHDMAN') or shutil.which('chdman')),
                                 reason='requires a real supported MAME chdman')


@real_chdman
def test_fast_dvd_preserves_every_iso_sector(tmp_path):
    folder = tmp_path / 'psp'
    folder.mkdir()
    source = folder / 'disc.iso'
    image = bytes(range(256)) * 128 + b'\x01CD001\x01' + bytes(2048 - 7) + bytes(range(128)) * 64
    source.write_bytes(image)
    os.utime(source, (1600000000, 1600000000))
    result = Backend(tmp_path).run(systems={'psp'}, quarantine=False)
    chd = source.with_suffix('.chd')
    assert not result['errors'] and result['converted'] == [str(chd)], result
    restored = tmp_path / 'restored.iso'
    subprocess.run([tool_path(), 'extractdvd', '-i', chd, '-o', restored], check=True)
    assert restored.read_bytes() == source.read_bytes() == image


@real_chdman
def test_verified_multidisc_quarantine_extract_and_undo(tmp_path):
    root = tmp_path / 'roms'
    cue1, raw, payload = disc(root / 'psx', 'Archive (Disc 1 of 2)')
    cue2, _, _ = disc(root / 'psx', 'Archive (Disc 2 of 2)')
    originals = {p: p.read_bytes() for p in (cue1, cue2, raw)}
    app = Backend(root)
    result = app.run(systems={'psx'})
    assert not result['errors'] and not result['paused'], result
    playlist = root / 'psx/Archive.m3u'
    entries = [playlist.parent / p for p in playlist.read_text(encoding='utf-8-sig').splitlines() if p and not p.startswith('#')]
    assert entries == [root / 'psx/.chd_discs' / p.with_suffix('.chd').name for p in (cue1, cue2)]
    assert all(p.is_file() for p in entries) and not any(p.exists() for p in originals)
    assert set(app.scan()['verified']) == {str(p) for p in entries}
    destination = tmp_path / 'extract'
    app.extract(entries[0], destination)
    assert next(destination.glob('*.bin')).read_bytes() == payload
    app.undo()
    assert not playlist.exists()
    assert all(p.read_bytes() == data for p, data in originals.items())


@real_chdman
def test_ingest_never_changes_mounted_source(tmp_path):
    origin = tmp_path / 'nas'
    root = tmp_path / 'roms'
    root.mkdir()
    cue, raw, payload = disc(origin / 'saturn', 'Read Only')
    cue_bytes = cue.read_bytes()
    app = Backend(root)
    result = app.run(systems={'saturn'}, ingest=str(origin))
    assert not result['errors'] and not result['paused'], result
    assert result['converted'] == [str(root / 'saturn/Read Only.chd')]
    assert cue.read_bytes() == cue_bytes and raw.read_bytes() == payload
    assert not list(root.rglob('*.bin')) and not list(root.rglob('*.cue'))


@real_chdman
def test_immediate_cleanup_nested_source_preserves_earlier_backups(tmp_path):
    root = tmp_path / 'roms'
    folder = root / 'psx'
    prior, _, _ = disc(folder / 'earlier', 'Earlier')
    app = Backend(root)
    assert not app.run(systems={'psx'})['errors']
    old_backup = root / '_couchchd_backup' / prior.relative_to(root)
    kept = old_backup.read_bytes()
    current, track, _ = disc(folder / 'new dump', 'Current')
    result = app.run(systems={'psx'}, immediate=True)
    assert not result['errors'] and not result['paused'], result
    assert result['converted'] == [str(folder / 'Current.chd')]
    assert not current.exists() and not track.exists()
    assert not (root / '_couchchd_backup' / current.relative_to(root)).exists()
    assert old_backup.read_bytes() == kept
    with pytest.raises(ValueError, match='purged'):
        app.undo()


def old(path):
    os.utime(path, (1600000000, 1600000000))
    return path


@pytest.mark.parametrize('location', ['local', 'network', 'ingest'])
def test_psp_cso_keeps_selected_format_through_staging(tmp_path, monkeypatch, location):
    from test_cso import _iso

    # CSO is independent of chdman, including when no local emulator is installed.
    monkeypatch.setenv('COUCHCHD_CHDMAN', str(tmp_path / 'absent'))
    root = tmp_path / 'library'
    root.mkdir()
    origin = tmp_path / 'mounted' if location == 'ingest' else root
    system = origin / 'PSP'
    system.mkdir(parents=True)
    source = system / 'Homebrew.iso'
    payload = _iso()
    source.write_bytes(payload)
    old(source)
    if location == 'network':
        fs = hardware.fs_type
        monkeypatch.setattr(hardware, 'fs_type', lambda p: 'nfs' if Path(p).is_relative_to(root) else fs(p))
    target_dir = root / 'psp' if location == 'ingest' else system
    target = target_dir / 'Homebrew.cso'
    app = Backend(root)
    events, space = [], []
    result = app.run(systems={'psp'}, psp_format='cso', quarantine=False, on_item=events.append,
                     on_space=space.append, ingest=str(origin) if location == 'ingest' else None)
    assert result['converted'] == [str(target)] and not result['errors'] and not result['paused'], result
    assert target.read_bytes()[:4] == b'CISO' and source.read_bytes() == payload
    assert [e.stage for e in events][-1] == 'done' and all(e.key == str(source) for e in events)
    assert space and space[0] == result['free_before'] > 0
    snapshot = app.scan()
    assert snapshot['chds'] == [] and snapshot['verified'] == [str(target)]
    assert not any(g['path'] == str(target) for g in snapshot['games'])
    if location != 'ingest':
        assert snapshot['games'][0]['verified_modes'] == ['cso']
        repeat = app.run(systems={'psp'}, psp_format='cso')
        assert not repeat['converted'] and not repeat['errors'] and source.read_bytes() == payload
    with pytest.raises(ValueError, match='Select a CHD'):
        app.extract(target, tmp_path / 'no-extract')


def test_cli_format_choice_and_reversible_mixed_output_batch(tmp_path, monkeypatch, capsys):
    from test_cso import _iso

    monkeypatch.setenv('COUCHCHD_CHDMAN', str(tmp_path / 'absent'))
    root = tmp_path / 'roms'
    psp = root / 'psp' / 'PSX' / 'Example.iso'  # a title's subfolder must not reroute it
    nes = root / 'nes' / 'Example.nes'
    sources = {psp: _iso(), nes: b'NES\x1a' + bytes([1, 1]) + bytes(10 + 16384 + 8192)}
    for path, data in sources.items():
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        old(path)
    assert cli.main(['--root', str(root), '--auto', '--system', 'psp', '--system', 'nes',
                     '--psp-format', 'cso']) == 0
    result = json.loads(capsys.readouterr().out)
    cso, archive = root / 'psp/Example.cso', nes.with_suffix('.7z')
    assert set(result['converted']) == {str(cso), str(archive)} and not result['errors']
    assert cso.read_bytes().startswith(b'CISO') and archive.read_bytes().startswith(b'7z\xbc\xaf\x27\x1c')
    assert all(not p.exists() for p in sources)
    app = Backend(root)
    assert set(app.scan()['verified']) == {str(cso), str(archive)}
    assert app.undo() == 4
    assert not archive.exists() and not cso.exists()
    assert all(path.read_bytes() == original for path, original in sources.items())


def test_derivatives_preserve_sources_even_with_immediate_cleanup(tmp_path, monkeypatch):
    from test_trim3ds import _card
    from test_xiso import write_disc

    xbox, n3ds = tmp_path / 'xbox', tmp_path / 'n3ds'
    xbox.mkdir(); n3ds.mkdir()
    disc = old(write_disc(xbox / 'Game.iso'))
    cart = n3ds / 'Game.cci'
    card_bytes = _card()
    cart.write_bytes(card_bytes)
    old(cart)
    stamp = disc.stat()
    app = Backend(tmp_path)
    # Sparse XGD1 media: exercise the conservative logical-size reservation
    # without requiring CI to provide 8 GiB of real temporary storage.
    monkeypatch.setattr(hardware, 'free_bytes', lambda _: 6 * 1024**3)
    paused = app.run(systems={'xbox'}, immediate=True)
    assert paused['paused'] and not paused['converted']
    monkeypatch.setattr(hardware, 'free_bytes', lambda _: 20 * 1024**3)
    outputs = {str(xbox / 'Game.xiso.iso'), str(n3ds / 'Game.trim.3ds')}
    result = app.run(systems={'xbox', '3ds'}, immediate=True)
    assert not result['errors'] and not result['paused'] and set(result['converted']) == outputs, result
    snapshot = app.scan()
    assert set(snapshot['verified']) == outputs and not snapshot['issues']
    assert {g['path'] for g in snapshot['games']} == {str(disc), str(cart)}
    assert not app.run(systems={'xbox', '3ds'}, immediate=True)['converted']
    assert app.purge() == 0 and not (tmp_path / '_couchchd_backup' / 'xbox' / disc.name).exists()
    assert app.undo() == 2 and not any(Path(p).exists() for p in outputs)
    assert disc.stat().st_size == stamp.st_size and disc.stat().st_mtime_ns == stamp.st_mtime_ns
    assert cart.read_bytes() == card_bytes


def test_cancel_selected_cso_keeps_sources_unpublished(tmp_path):
    from threading import Event
    from test_cso import _iso

    folder = tmp_path / 'psp'
    folder.mkdir()
    selected = folder / 'Game.iso'
    original = _iso()
    selected.write_bytes(original)
    old(selected)
    skipped = folder / 'Unselected UDF.iso'
    udf = bytearray(original)
    udf[16 * 2048:16 * 2048 + 7] = b'\x00BEA01\x01'
    skipped.write_bytes(udf)
    old(skipped)
    cancel = Event()
    events = []
    def stop(item):
        events.append(item)
        if item.stage == 'verifying':
            cancel.set()
    app = Backend(tmp_path)
    result = app.run(systems={'psp'}, sources={str(selected)}, psp_format='cso', cancel=cancel, on_item=stop)
    assert result['paused'] and not result['converted'] and not app.scan()['verified']
    assert all(e.key == str(selected) for e in events) and events[-1].stage == 'cancelled'
    assert selected.read_bytes() == original and skipped.read_bytes() == udf
    assert not list(tmp_path.rglob('*.part'))


@real_chdman
def test_new_cd_system_alias_publishes_chd_without_hiding_discs(tmp_path):
    folder = tmp_path / 'tg-cd'
    first, _, _ = disc(folder / 'psx', 'Adventure (Disc 1)')
    second, _, _ = disc(folder / 'psx', 'Adventure (Disc 2)')
    app = Backend(tmp_path)
    result = app.run(systems={'pcenginecd'}, quarantine=False)
    expected = {str(folder / p.with_suffix('.chd').name) for p in (first, second)}
    assert not result['errors'] and set(result['converted']) == expected, result
    snapshot = app.scan()
    assert set(snapshot['verified']) == expected and not snapshot['playlists']


def test_verified_rvz_transactions_with_installed_dolphintool(tmp_path):
    from couchchd import rvz
    from test_rvz import write_disc

    try:
        rvz.tool_path()
    except FileNotFoundError:
        if os.environ.get('COUCHCHD_DOLPHIN_TOOL'):
            raise
        pytest.skip('Requires DolphinTool; setting COUCHCHD_DOLPHIN_TOOL makes this gate mandatory')
    folder = tmp_path / 'gamecube'
    folder.mkdir()
    source = old(write_disc(folder / 'Homebrew.gcm'))
    app = Backend(tmp_path)
    result = app.run(systems={'gc'}, quarantine=False)
    out = folder / 'Homebrew.rvz'
    assert not result['errors'] and result['converted'] == [str(out)], result
    assert source.exists() and app.scan()['verified'] == [str(out)]
    assert app.undo() == 1 and not out.exists() and source.exists()
