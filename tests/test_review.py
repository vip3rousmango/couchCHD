import os
from pathlib import Path

import pytest

from couchchd import multidisc
from couchchd.ledger import Ledger, SafetyError
from couchchd.locking import library_lock
from couchchd.review import reclaim_preview, reclaim_reviewed, review, set_play_tested


def _disc(db, batch, name, ext='.iso', data=b'original disc'):
    source = db.root / 'psx' / (name + ext)
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(data)
    out = db.publish(batch, source.with_suffix('.chd'), lambda p: p.write_bytes(name.encode()))
    return out, db.quarantine(batch, source, outputs=[out])


def _mark(db, path, value=True):
    row = next(r for r in review(db) if r['path'] == str(path))
    set_play_tested(db, row['id'], value)
    return row['id']


def test_scope_never_expands_and_marks_survive_restart(tmp_path):
    with library_lock(tmp_path), Ledger(tmp_path) as db:
        batch = db.begin()
        a, backup_a = _disc(db, batch, 'A')
        b, backup_b = _disc(db, batch, 'B')
        db.finish(batch)
        assert reclaim_preview(db)['count'] == 0
        _mark(db, a)
        old = reclaim_preview(db)
        assert old['files'] == [str(backup_a)] and old['batches'] == [batch]
    with library_lock(tmp_path), Ledger(tmp_path) as db:
        assert [r['tested'] for r in review(db)] == [True, False]
        _mark(db, b)
        with pytest.raises(SafetyError, match='preview'):
            reclaim_reviewed(db, old['token'])
        assert backup_a.exists() and backup_b.exists()
        _mark(db, a, False)
        scoped = reclaim_preview(db)
        assert scoped['bytes'] == len(b'original disc') and scoped['files'] == [str(backup_b)]
        assert reclaim_reviewed(db, scoped['token']) == 1
        assert db.history()[0]['status'] == 'partial_purge'
        assert a.exists() and b.exists() and backup_a.exists() and not backup_b.exists()
        with pytest.raises(SafetyError):
            db.undo()


@pytest.mark.parametrize('changed', ['output', 'backup', 'replacement', 'linked'])
def test_stale_preview_rejects_all_removals(tmp_path, changed):
    with Ledger(tmp_path) as db:
        batch = db.begin()
        a, original = _disc(db, batch, 'A')
        b, backup = _disc(db, batch, 'B')
        db.finish(batch)
        _mark(db, a)
        key = _mark(db, b)
        old = reclaim_preview(db)
        if changed == 'output':
            b.write_bytes(b'changed output')
        elif changed == 'backup':
            backup.write_bytes(b'other disc')
        elif changed == 'replacement':
            other = b.with_name('other')
            other.write_bytes(b.read_bytes())
            os.replace(other, b)
        else:
            backup.rename(backup.with_name('personal'))
            backup.symlink_to('personal')
        with pytest.raises(SafetyError):
            reclaim_reviewed(db, old['token'])
        if changed in {'output', 'replacement'}:
            with pytest.raises(SafetyError):
                set_play_tested(db, key, True)
        assert original.is_file() and backup.is_file() and db.history()[0]['status'] == 'finished'


def test_unmark_and_undo_do_not_transfer_a_test_to_republished_output(tmp_path):
    with Ledger(tmp_path) as db:
        batch = db.begin()
        a, backup = _disc(db, batch, 'A')
        db.finish(batch)
        key = _mark(db, a)
        set_play_tested(db, key, False)
        assert reclaim_preview(db)['count'] == 0
        set_play_tested(db, key, True)
        assert db.undo() == 2 and not backup.exists()
        again = db.begin()
        db.publish(again, a, lambda p: p.write_bytes(b'A'))
        db.finish(again)
        assert review(db)[0]['tested'] is False
        with pytest.raises(SafetyError):
            set_play_tested(db, key, True)


def test_all_discs_and_intact_playlist_required_across_batches(tmp_path):
    with Ledger(tmp_path) as db:
        for n in (1, 2):
            batch = db.begin()
            out, _ = _disc(db, batch, f'Game (Disc {n})')
            multidisc.organize(tmp_path, batch, db, [out])
            db.finish(batch)
            if n == 1:
                _mark(db, out)
        rows = review(db)
        assert rows[0]['tested'] and not rows[1]['tested']
        assert reclaim_preview(db)['count'] == 0
        set_play_tested(db, rows[1]['id'], True)
        plan = reclaim_preview(db)
        assert plan['count'] == 2
        playlist = tmp_path / 'psx' / 'Game.m3u'
        playlist.write_text('.chd_discs/Game (Disc 1).chd\n')
        with pytest.raises(SafetyError):
            reclaim_reviewed(db, plan['token'])
        assert reclaim_preview(db)['count'] == 0
        assert all(Path(p).is_file() for p in plan['files'])


def test_shared_track_stays_until_every_owner_is_currently_tested(tmp_path):
    sheet = b'FILE "shared.bin" BINARY\n TRACK 01 AUDIO\n INDEX 01 00:00:00\n'
    with Ledger(tmp_path) as db:
        earlier = db.begin()
        a, backup_a = _disc(db, earlier, 'A', '.cue', sheet)
        db.finish(earlier)
        later = db.begin()
        b, backup_b = _disc(db, later, 'B', '.cue', sheet)
        track = a.parent / 'shared.bin'
        track.write_bytes(b'audio')
        track_backup = db.quarantine(later, track, outputs=[b])
        db.finish(later)
        foreign = backup_a.with_name('unrelated.bin')
        foreign.write_bytes(b'unrelated')
        _mark(db, a)
        plan = reclaim_preview(db)
        assert plan['files'] == [str(backup_a)]
        reclaim_reviewed(db, plan['token'])
        _mark(db, a, False)
        _mark(db, b)
        assert reclaim_preview(db)['files'] == [str(backup_b)]
        _mark(db, a)
        live_sheet = a.with_name('Unconverted.cue')
        live_sheet.write_bytes(sheet)
        assert reclaim_preview(db)['files'] == [str(backup_b)]
        live_sheet.unlink()
        plan = reclaim_preview(db)
        assert set(plan['files']) == {str(track_backup), str(backup_b)}
        assert reclaim_reviewed(db, plan['token']) == 2
        assert {r['status'] for r in db.history()} == {'purged'}
        assert foreign.read_bytes() == b'unrelated'


def test_custom_playlist_referencing_original_or_untested_output_preserves_it(tmp_path):
    with Ledger(tmp_path) as db:
        batch = db.begin()
        out, _ = _disc(db, batch, 'A')
        db.finish(batch)
        _mark(db, out)
        playlist = out.parent / 'custom.m3u8'
        playlist.write_text('./A.iso\n')
        assert reclaim_preview(db)['count'] == 0
        playlist.write_text('A.chd\nforeign.chd\n')
        assert reclaim_preview(db)['count'] == 0


def test_legacy_proofs_do_not_authorize_unrelated_files_or_always_keep_media(tmp_path):
    with Ledger(tmp_path) as db:
        batch = db.begin()
        out, backup = _disc(db, batch, 'A')
        unrelated = out.parent / 'saves.dat'
        unrelated.write_bytes(b'personal')
        personal = db.quarantine(batch, unrelated, outputs=[out])
        xbox = tmp_path / 'xbox'
        xbox.mkdir()
        original = xbox / 'A.iso'
        original.write_bytes(b'physical layout')
        saved = db.quarantine(batch, original, outputs=[out])
        db.finish(batch)
        _mark(db, out)
        plan = reclaim_preview(db)
        assert plan['files'] == [str(backup)]
        assert reclaim_reviewed(db, plan['token']) == 1
        assert personal.read_bytes() == b'personal' and saved.read_bytes() == b'physical layout'


def test_interrupted_track_cleanup_requires_fresh_confirmation(tmp_path, monkeypatch):
    with Ledger(tmp_path) as db:
        batch = db.begin()
        out, sheet = _disc(db, batch, 'Music', '.cue',
                           b'FILE \"audio.bin\" BINARY\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n')
        track = out.with_name('audio.bin')
        track.write_bytes(b'soundtrack')
        backup = db.quarantine(batch, track, outputs=[out])
        db.finish(batch)
        _mark(db, out)
        plan = reclaim_preview(db)
        assert plan['files'] == [str(sheet), str(backup)]
        unlink = Path.unlink

        def lose_mount(path, *args, **kwargs):
            if path == backup:
                raise OSError('mount unavailable')
            unlink(path, *args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(Path, 'unlink', lose_mount)
            with pytest.raises(OSError):
                reclaim_reviewed(db, plan['token'])
        assert not sheet.exists() and backup.exists() and out.exists()
    with Ledger(tmp_path) as db:
        assert db.history()[0]['status'] == 'partial_purge'
        with pytest.raises(SafetyError):
            db.undo()
        with pytest.raises(SafetyError, match='preview'):
            reclaim_reviewed(db, plan['token'])
        remaining = reclaim_preview(db)
        assert remaining['files'] == [str(backup)]
        assert reclaim_reviewed(db, remaining['token']) == 1
        assert db.history()[0]['status'] == 'purged' and out.exists()
