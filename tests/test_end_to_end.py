"""A public-interface round trip on generated audio sectors, never user media."""

import os
import shutil
from pathlib import Path

import pytest

from couchchd.app import Backend
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
