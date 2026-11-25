import errno
import os
import xml.etree.ElementTree as ET

import pytest

from couchchd import ledger as journal
from couchchd import multidisc
from couchchd.ledger import Ledger, SafetyError
from couchchd.multidisc import organize


def publish(ledger, batch, path, body=b'verified output'):
    return ledger.publish(batch, path, lambda private: private.write_bytes(body))


def test_quarantine_and_undo_survive_restart(tmp_path):
    cue = tmp_path / 'psx' / 'Game.cue'
    cue.parent.mkdir()
    cue.write_bytes(b'original cuesheet')
    with Ledger(tmp_path) as ledger:
        batch = ledger.begin()
        chd = publish(ledger, batch, cue.with_suffix('.chd'))
        backup = ledger.quarantine(batch, cue, outputs=[chd])
        ledger.finish(batch)
        assert backup == tmp_path / '_couchchd_backup' / 'psx' / 'Game.cue'
        assert not cue.exists()
    with Ledger(tmp_path) as ledger:
        assert ledger.history()[0]['operations'] == 2
        assert ledger.verified() == [chd]
        assert ledger.undo() == 2
        assert ledger.history()[0]['status'] == 'undone'
        assert ledger.verified() == []
        assert ledger.undo() == 0
    assert cue.read_bytes() == b'original cuesheet'
    assert not backup.exists() and not chd.exists()
    retained = tmp_path / '_couchchd_backup' / '.undo' / str(batch) / 'psx' / 'Game.chd'
    assert retained.read_bytes() == b'verified output'


def test_failed_or_racing_publication_never_claims_foreign_output(tmp_path):
    final = tmp_path / 'psx' / 'Game.chd'
    with Ledger(tmp_path) as ledger:
        batch = ledger.begin()

        def failing(stage):
            assert not stage.exists() and not final.exists()
            stage.write_bytes(b'incomplete')
            raise OSError(errno.ENOSPC, 'device full')

        with pytest.raises(OSError):
            ledger.publish(batch, final, failing)
        assert not final.exists()

        def racer(stage):
            stage.write_bytes(b'verified')
            final.write_bytes(b'other program')

        with pytest.raises(FileExistsError):
            ledger.publish(batch, final, racer)
        ledger.finish(batch)
        ledger.undo()
    assert final.read_bytes() == b'other program'


def test_crash_after_quarantine_move_recovers_by_identity(tmp_path, monkeypatch):
    cue = tmp_path / 'Game.cue'
    cue.write_bytes(b'original')
    real = journal.install_new
    with Ledger(tmp_path) as ledger:
        batch = ledger.begin()
        chd = publish(ledger, batch, cue.with_suffix('.chd'))

        def interrupted(src, dst):
            real(src, dst)
            raise OSError('simulated loss after rename, before done record')

        with monkeypatch.context() as m:
            m.setattr(journal, 'install_new', interrupted)
            with pytest.raises(OSError):
                ledger.quarantine(batch, cue, outputs=[chd])
        assert not cue.exists()
    with Ledger(tmp_path) as ledger:
        assert ledger.history()[0]['status'] == 'interrupted'
        with pytest.raises(SafetyError):
            ledger.purge()
        cue.write_bytes(b'new file owned by user')
        with pytest.raises(SafetyError):
            ledger.undo()
        assert cue.read_bytes() == b'new file owned by user' and chd.exists()
        cue.rename(cue.with_suffix('.saved'))
        ledger.undo()
    assert cue.read_bytes() == b'original'


def test_replacement_metadata_not_removed_even_if_bytes_identical(tmp_path):
    with Ledger(tmp_path) as ledger:
        batch = ledger.begin()
        output = publish(ledger, batch, tmp_path / 'Game.m3u', b'custom content')
        ledger.finish(batch)
        other = tmp_path / 'replacement'
        other.write_bytes(output.read_bytes())
        os.replace(other, output)
        with pytest.raises(SafetyError):
            ledger.undo()
        assert output.read_bytes() == b'custom content'


def test_quarantine_refuses_stale_inputs_and_missing_proofs(tmp_path):
    with Ledger(tmp_path) as ledger:
        batch = ledger.begin()
        img = tmp_path / 'disc.iso'
        img.write_bytes(b'original')
        with pytest.raises(SafetyError):
            ledger.quarantine(batch, img)
        output = publish(ledger, batch, img.with_suffix('.chd'))
        st = img.stat()
        before = st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns
        img.write_bytes(b'replaced after verification')
        with pytest.raises(SafetyError):
            ledger.quarantine(batch, img, outputs=[output], expected=before)
        assert img.read_bytes() == b'replaced after verification'
        assert not (tmp_path / '_couchchd_backup').exists()


def test_purge_preflights_all_batches_and_ignores_untracked_backups(tmp_path):
    backups = []
    with Ledger(tmp_path) as ledger:
        for n in range(2):
            batch = ledger.begin()
            src = tmp_path / f'{n}.iso'
            src.write_bytes(b'original')
            converted = publish(ledger, batch, src.with_suffix('.chd'))
            backups.append(ledger.quarantine(batch, src))
            ledger.finish(batch)
        foreign = backups[0].parent / 'personal.iso'
        foreign.write_bytes(b'not from CouchCHD')
        converted.write_bytes(b'corrupt')
        with pytest.raises(SafetyError):
            ledger.purge()
        assert all(p.read_bytes() == b'original' for p in backups)
        assert foreign.read_bytes() == b'not from CouchCHD'


def test_cross_device_move_copies_verifies_and_restores(tmp_path, monkeypatch):
    real = journal.install_new
    root = tmp_path / '_couchchd_backup'

    def different_devices(src, dst):
        if src.is_relative_to(root) != dst.is_relative_to(root):
            raise OSError(errno.EXDEV, 'simulated mount boundary')
        real(src, dst)

    monkeypatch.setattr(journal, 'install_new', different_devices)
    with Ledger(tmp_path) as ledger:
        batch = ledger.begin()
        iso = tmp_path / 'Game.iso'
        iso.write_bytes(b'original disc')
        publish(ledger, batch, iso.with_suffix('.chd'))
        backup = ledger.quarantine(batch, iso)
        assert backup.read_bytes() == b'original disc' and not iso.exists()
        ledger.finish(batch)
        ledger.undo()
        assert iso.read_bytes() == b'original disc' and not backup.exists()


def test_symbolic_backup_directory_is_never_followed(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    roms = tmp_path / 'roms'
    roms.mkdir()
    (roms / '_couchchd_backup').symlink_to(outside, target_is_directory=True)
    with Ledger(roms) as ledger:
        batch = ledger.begin()
        iso = roms / 'disc.iso'
        iso.write_bytes(b'original')
        publish(ledger, batch, iso.with_suffix('.chd'))
        with pytest.raises(SafetyError):
            ledger.quarantine(batch, iso)
        assert iso.exists() and list(outside.iterdir()) == []


def test_metadata_and_art_roundtrip_and_numeric_playlist(tmp_path):
    folder = tmp_path / 'psx'
    media = folder / 'media' / 'covers'
    media.mkdir(parents=True)
    first_art = media / 'Game (Disc 1).png'
    first_art.write_bytes(b'scraped cover')
    xml = folder / 'gamelist.xml'
    original = (b'<?xml version="1.0"?>\n<gameList><!-- keep this -->\n'
                b'<game><path>./Game (Disc 1).cue</path><name>Game (Disc 1)</name>'
                b'<image>./media/covers/Game (Disc 1).png</image><rating>0.75</rating></game>\n'
                b'</gameList><!-- literal </gameList> -->\n')
    xml.write_bytes(original)
    with Ledger(tmp_path) as ledger:
        batch = ledger.begin()
        discs = [publish(ledger, batch, folder / f'Game (Disc {n}).chd') for n in range(10, 0, -1)]
        organize(tmp_path, batch, ledger, discs)
        playlist = folder / 'Game.m3u'
        assert playlist.read_text().splitlines() == [f'.chd_discs/Game (Disc {i}).chd' for i in range(1, 11)]
        assert all(not p.exists() for p in discs)
        clone = ET.parse(xml).findall('game')[1]
        assert clone.findtext('path') == './Game.m3u'
        assert clone.findtext('rating') == '0.75' and clone.findtext('name') == 'Game'
        assert clone.findtext('image') == './media/covers/Game.png'
        assert (media / 'Game.png').read_bytes() == b'scraped cover'
        ledger.finish(batch)
        ledger.undo()
        assert not playlist.exists() and not (media / 'Game.png').exists()
        assert xml.read_bytes() == original and first_art.read_bytes() == b'scraped cover'


@pytest.fixture
def psx(tmp_path):
    folder = tmp_path / 'psx'
    folder.mkdir()
    return folder


def test_purge_verifies_cross_batch_organized_locations(psx):
    with Ledger(psx) as ledger:
        for n in (1, 2):
            batch = ledger.begin()
            cue = psx / f'Game (CD {n} of 2).cue'
            cue.write_bytes(b'original')
            disc = publish(ledger, batch, cue.with_suffix('.chd'))
            ledger.quarantine(batch, cue, outputs=[disc])
            organize(psx, batch, ledger, [disc])
            ledger.finish(batch)
        assert (psx / 'Game.m3u').is_file()
        assert set(p.parent.name for p in ledger.verified()) == {'.chd_discs'}
        foreign = psx / '_couchchd_backup' / 'other'
        foreign.write_bytes(b'mine')
        assert ledger.purge() == 2 and ledger.purge() == 0
        assert foreign.read_bytes() == b'mine'
        with pytest.raises(SafetyError):
            ledger.undo()
        assert len(ledger.verified()) == 2


@pytest.mark.parametrize('tags', [('Disc 1',), ('Disc 1', 'Disc 3'), ('Disc 1 of 3', 'Disc 2 of 3'), ('Disc 1', 'Disk 1')])
def test_ambiguous_or_incomplete_sets_untouched(psx, tags):
    with Ledger(psx) as ledger:
        batch = ledger.begin()
        discs = [publish(ledger, batch, psx / f'Game ({tag}).chd') for tag in tags]
        organize(psx, batch, ledger, discs)
        assert not (psx / 'Game.m3u').exists()
        assert all(p.is_file() for p in discs)


def test_preexisting_playlist_and_external_chds_never_adopted(psx):
    with Ledger(psx) as ledger:
        batch = ledger.begin()
        discs = [publish(ledger, batch, psx / f'Game (Disc {n}).chd') for n in [1, 2]]
        external = psx / 'Game (Disc 3).chd'
        external.write_bytes(b'not owned')
        organize(psx, batch, ledger, [*discs, external])
        assert not (psx / 'Game.m3u').exists()
        external.rename(psx / 'other.chd')
        custom = psx / 'favorites.m3u'
        custom.write_text('./Game (Disc 1).chd\n')
        organize(psx, batch, ledger, discs)
        assert all(p.is_file() for p in discs) and custom.is_file()


def test_recalbox_running_frontend_warns_before_metadata_changes(psx, monkeypatch):
    monkeypatch.setattr(multidisc, '_recalbox', lambda _: True)
    monkeypatch.setattr(multidisc, '_frontend_running', lambda: True)
    xml = psx / 'gamelist.xml'
    xml.write_text('<gameList/>')
    with Ledger(psx) as ledger:
        batch = ledger.begin()
        discs = [publish(ledger, batch, psx / f'Game (Disc {n}).chd') for n in (1, 2)]
        with pytest.warns(UserWarning, match='Recalbox'):
            messages = organize(psx, batch, ledger, discs)
        assert any('stop EmulationStation' in message for message in messages)
        assert xml.read_text() == '<gameList/>' and all(p.is_file() for p in discs)


@pytest.mark.parametrize(('system', 'suffix'), [('ps2', '.chd'), ('psp', '.chd'), ('3do', '.chd'),
                                                ('gc', '.rvz'), ('psp', '.cso'), ('snes', '.7z')])
def test_incompatible_playlist_formats_remain_visible(tmp_path, system, suffix):
    folder = tmp_path / system
    folder.mkdir()
    with Ledger(tmp_path) as ledger:
        batch = ledger.begin()
        discs = [publish(ledger, batch, folder / f'Game (Disc {n}){suffix}') for n in (1, 2)]
        assert organize(tmp_path, batch, ledger, discs) == []
        assert all(p.is_file() for p in discs) and not list(folder.glob('*.m3u'))


def test_job_purge_preserves_other_backups_and_potentially_shared_track(tmp_path):
    with Ledger(tmp_path) as ledger:
        earlier = ledger.begin()
        cue = tmp_path / 'Earlier.cue'
        cue.write_bytes(b'FILE "tracks/shared.bin" BINARY\n')
        publish(ledger, earlier, cue.with_suffix('.chd'))
        old_backup = ledger.quarantine(earlier, cue)
        ledger.finish(earlier)

        current = ledger.begin()
        new_cue = tmp_path / 'Current.cue'
        new_cue.write_bytes(b'FILE "tracks/shared.bin" BINARY\n')
        track = tmp_path / 'tracks' / 'shared.bin'
        track.parent.mkdir()
        track.write_bytes(b'original track')
        out = publish(ledger, current, new_cue.with_suffix('.chd'))
        cue_backup = ledger.quarantine(current, new_cue, outputs=[out])
        track_backup = ledger.quarantine(current, track, outputs=[out])
        unrelated = tmp_path / 'Other.iso'
        unrelated.write_bytes(b'unrelated')
        other_output = publish(ledger, current, unrelated.with_suffix('.chd'))
        unrelated_backup = ledger.quarantine(current, unrelated, outputs=[other_output])

        with pytest.raises(SafetyError):
            ledger.purge_job(current, [cue_backup, old_backup])
        assert cue_backup.exists()
        assert ledger.purge_job(current, [track_backup, cue_backup]) == 1
        assert not cue_backup.exists()
        assert track_backup.read_bytes() == b'original track'
        assert old_backup.is_file() and unrelated_backup.read_bytes() == b'unrelated'
        with pytest.raises(SafetyError):
            ledger.undo()
        ledger.finish(current)
        assert ledger.history()[0]['status'] == 'partial_purge'
        with pytest.raises(SafetyError):
            ledger.undo()
        assert ledger.purge() == 3


def test_job_purge_resumes_after_unlink_failure(tmp_path, monkeypatch):
    with Ledger(tmp_path) as ledger:
        batch = ledger.begin()
        backups = []
        for n in range(2):
            src = tmp_path / f'{n}.iso'
            src.write_bytes(b'original')
            output = publish(ledger, batch, src.with_suffix('.chd'))
            backups.append(ledger.quarantine(batch, src, outputs=[output]))
        real = type(tmp_path).unlink

        def fail_second(path, *args, **kwargs):
            if path == backups[1]:
                raise OSError('lost mount')
            return real(path, *args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(type(tmp_path), 'unlink', fail_second)
            with pytest.raises(OSError):
                ledger.purge_job(batch, backups)
        assert not backups[0].exists() and backups[1].is_file()
        with pytest.raises(SafetyError):
            ledger.undo()
    with Ledger(tmp_path) as ledger:
        with pytest.raises(SafetyError):
            ledger.undo()
        assert ledger.purge() == 1
        assert ledger.history()[0]['status'] == 'purged'
