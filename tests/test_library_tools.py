import os
import time
from pathlib import Path

import pytest

from couchchd import library_tools
from couchchd.app import Backend
from couchchd.batch import GIB


def _file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    old = time.time() - 60
    os.utime(path, (old, old))
    return path


def _iso(path, sectors=20, signature=b'\x01CD001\x01'):
    image = bytearray(b'Z' * (sectors * 2048))
    image[16 * 2048:16 * 2048 + 7] = signature
    return _file(path, image)


def _cue(path, track):
    return _file(path, f'FILE "{track}" BINARY\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'.encode())


def _scan(root):
    result = Backend(root).scan()
    # Plan from a deterministic capacity snapshot, independent of the test host's disk.
    result['free'] = 10 * GIB
    return result


def test_plan_scopes_target_and_reserves_published_outputs_without_purge_credit(tmp_path):
    root = tmp_path.resolve()
    psx = root / 'psx'
    large = _iso(psx / 'First alphabetically.iso', 28)
    small = _iso(psx / 'Small.iso')
    tied = _iso(psx / 'Tiny.iso')
    _iso(root / 'psp' / 'Other.iso')
    scan = _scan(root)
    for order in (scan['games'], list(reversed(scan['games']))):
        all_items = library_tools.plan(root, {**scan, 'games': order}, {'psx'}, 'chd', 0)
        assert all_items['sources'] == [str(small), str(tied), str(large)]
        assert all_items['freed_bytes'] == 0
        assert all_items['estimated_savings_bytes'] == sum(i['estimated_savings_bytes'] for i in all_items['items'])
        assert all_items['required_free_bytes'] > max(i['required_free_bytes'] for i in all_items['items'])
    target = library_tools.plan(root, scan, {'psx'}, 'chd', 1)
    assert target['target_met'] and target['sources'] == [str(small)]
    no_space = library_tools.plan(root, {**scan, 'free': 2 * GIB - 1}, {'psx'}, 'chd', 1)
    assert not no_space['target_met'] and no_space['items'] == []
    one_room = library_tools.plan(root, {**scan, 'free': 2 * GIB}, {'psx'}, 'chd', GIB)
    assert not one_room['target_met'] and one_room['sources'] == [str(small)]
    assert len(one_room['excluded']) == 2


def test_plan_avoids_verified_colliding_shared_and_playlist_inputs(tmp_path):
    root = tmp_path.resolve()
    psx = root / 'psx'
    _iso(psx / 'edition a' / 'Same.iso')
    _iso(psx / 'edition b' / 'same.iso')
    _iso(psx / 'Foreign.iso')
    _file(psx / '.chd_discs' / 'FOREIGN.CHD', b'not ours')
    _file(psx / 'music.bin', b'a' * 2352)
    _cue(psx / 'Left.cue', 'music.bin')
    _cue(psx / 'Right.cue', 'music.bin')
    listed = _iso(psx / 'On playlist.iso')
    _file(psx / 'set.m3u', listed.name.encode())
    link = psx / 'Unsafe.iso'
    link.symlink_to(listed)
    clean = _iso(psx / 'Clean.iso')
    verified = _iso(psx / 'Finished.iso')
    scan = _scan(root)
    next(g for g in scan['games'] if g['path'] == str(verified))['verified_modes'] = ['createcd']
    result = library_tools.plan(root, scan, {'psx'}, 'chd', 0)
    assert result['sources'] == [str(clean)]
    reasons = {Path(i['path']).name: i['reason'] for i in result['excluded']}
    assert all('Multiple inputs' in reasons[name] for name in ('Same.iso', 'same.iso'))
    assert all('Shared track' in reasons[name] for name in ('Left.cue', 'Right.cue'))
    assert 'organized' in reasons['Foreign.iso'] and 'playlist' in reasons[listed.name]
    assert 'verified' in reasons[verified.name] and 'Symlink' in reasons[link.name]


def test_retained_archive_can_conflict_with_reclaimable_loose_disc(tmp_path):
    root = tmp_path.resolve()
    source = _iso(root / 'psp' / 'Disc.iso')
    scan = _scan(root)
    scan['games'].append({**scan['games'][0], 'archive': True, 'path': str(source.with_suffix('.zip'))})
    result = library_tools.plan(root, scan, {'psp'}, 'cso', 1)
    assert not result['sources'] and not result['target_met']
    by_file = {Path(i['path']).suffix: i['reason'] for i in result['excluded']}
    assert 'always kept' in by_file['.zip'] and 'Multiple inputs' in by_file['.iso']


def test_plan_does_not_count_originals_the_runner_must_retain(tmp_path):
    root = tmp_path.resolve()
    _iso(root / 'psp' / 'Ready.iso')
    broken = _cue(root / 'other-system' / 'Lost.cue', 'missing.bin')
    # Even an unsupported directory may have an ambiguous live sheet; the
    # actual quarantine proof would retain originals, not free the estimated bytes.
    result = library_tools.plan(root, _scan(root), {'psp'}, 'chd', 1)
    assert not result['sources'] and not result['target_met']
    assert str(broken) in result['excluded'][0]['reason']


def test_psp_format_validation_and_foreign_cso_do_not_block_chd(tmp_path):
    root = tmp_path.resolve()
    game = _iso(root / 'psp' / 'Game.iso')
    udf = _iso(root / 'psp' / 'UDF.iso', signature=b'\x00BEA01\x01')
    _file(game.with_suffix('.CSO'), b'foreign')
    scan = _scan(root)
    assert library_tools.plan(root, scan, {'psp'}, 'chd', 0)['sources'] == [str(game), str(udf)]
    cso = library_tools.plan(root, scan, {'psp'}, 'cso', 0)
    assert cso['items'] == []
    assert any('ISO 9660' in i['reason'] for i in cso['excluded'])
    with pytest.raises(ValueError, match='target'):
        library_tools.plan(root, scan, {'psp'}, 'cso', -1)


def test_derivatives_never_count_as_cleanup(tmp_path):
    root = tmp_path.resolve()
    source = root / 'xbox' / 'Disc.iso'
    # These scan rows have already passed scanner inspection. No real Xbox/3DS image needed.
    scan = dict(games=[dict(path=str(source), system='xbox', size=GIB, mode='xiso', tracks=[], verified_modes=[]),
                       dict(path=str(root / '3ds' / 'Game.3ds'), system='3ds', size=GIB, mode='trim3ds',
                            tracks=[], verified_modes=[])], issues=[], playlists=[], free=10 * GIB)
    result = library_tools.plan(root, scan, {'xbox', '3ds'}, 'chd', 1)
    assert result['sources'] == [] and not result['target_met']
    assert len(result['excluded']) == 2 and all('always retains' in row['reason'] for row in result['excluded'])


def test_network_stage_needs_space_on_local_temp_volume(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    _iso(root / 'psp' / 'Remote.iso')
    scan = _scan(root)
    # Different destination and scratch devices; staging must not spend destination free on scratch.
    class Scratch:
        st_dev = root.stat().st_dev + 1

    original = Path.stat
    scratch = root / 'local-temp'
    monkeypatch.setattr(library_tools.hardware, 'fs_type', lambda _: 'cifs')
    monkeypatch.setattr(library_tools.tempfile, 'gettempdir', lambda: str(scratch))
    monkeypatch.setattr(Path, 'stat', lambda self, *a, **kw: Scratch() if self == scratch else original(self, *a, **kw))
    monkeypatch.setattr(library_tools.hardware, 'free_bytes', lambda p: GIB if p == scratch else 10 * GIB)
    result = library_tools.plan(root, scan, {'psp'}, 'chd', 1)
    assert result['sources'] == [] and 'Local staging' in result['excluded'][0]['reason']


def test_health_finds_disc_gaps_shared_tracks_and_broken_references_without_edits(tmp_path):
    root = tmp_path.resolve()
    psx = root / 'psx'
    _file(psx / 'music.bin', b'B' * 2352)
    _cue(psx / 'First.cue', 'music.bin')
    _cue(psx / 'Second.cue', 'music.bin')
    _cue(psx / 'Orphan.cue', 'lost.bin')
    _file(psx / 'Bad.cue', b'not a disc sheet')
    _file(psx / '.chd_discs' / 'Saga (Disc 1 of 3).chd', b'1')
    _file(psx / '.chd_discs' / 'Saga (Disc 3 of 3).chd', b'3')
    _file(psx / 'Saga.m3u', b'#EXTM3U\n.chd_discs/Saga (Disc 1 of 3).chd\n.chd_discs/Saga (Disc 2 of 3).chd\n')
    _file(psx / 'Unsafe.m3u', b'../outside.chd\n')
    scan = _scan(root)
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    report = library_tools.health(root, scan)
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
    assert report['count'] == len(report['issues']) == 6
    assert all(i['action'] and i['system'] == 'psx' for i in report['issues'])
    by_key = {(Path(i['path']).name, i['category']): i for i in report['issues']}
    assert 'Disc 2 is missing' in by_key['Saga (Disc 1 of 3).chd', 'disc_gap']['reason']
    assert 'lost.bin' in by_key['Orphan.cue', 'missing_track']['reason']
    assert by_key['Bad.cue', 'broken_descriptor']['action']
    assert 'Second.cue' in by_key['music.bin', 'shared_track']['reason']
    assert 'Disc 2' in by_key['Saga.m3u', 'playlist_member']['reason']
    assert 'Unsafe' in by_key['Unsafe.m3u', 'unsafe_playlist_member']['reason']
