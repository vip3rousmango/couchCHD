import os
from io import BytesIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from couchchd import converter, cso, hardware, profiles, scanner, zip_stage
from couchchd.app import Backend
from couchchd.batch import Batch, GIB, Job, space_requirements
from couchchd.ledger import Ledger
from test_cso import _iso


def _zip(path, members, method=ZIP_DEFLATED):
    path.parent.mkdir(exist_ok=True, parents=True)
    with ZipFile(path, 'x', compression=method) as z:
        for name, data in members.items():
            z.writestr(name, data)
    os.utime(path, (1600000000, 1600000000))
    return path


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setattr(hardware, 'free_bytes', lambda _: 32 * GIB)
    monkeypatch.setattr(hardware, 'fs_type', lambda _: 'ext4')
    monkeypatch.setattr(hardware.Watchdog, 'pause_reason', lambda _: '')
    monkeypatch.setattr(hardware.Watchdog, 'limit', lambda _: 4)
    monkeypatch.setattr(profiles, 'compression_plan', lambda _: profiles.CompressionPlan(4, False))


def test_mixed_library_streams_cso_retains_archives_even_with_immediate_purge(tmp_path, desktop, monkeypatch):
    root = tmp_path.resolve()
    original = _iso()
    folder = root / 'psp'
    archives = [_zip(folder / name, {'inner/Other.iso': original}) for name in ('A.zip', 'B.zip')]
    before = [p.read_bytes() for p in archives]
    loose = folder / 'Loose.iso'
    loose.write_bytes(original)
    os.utime(loose, (1600000000, 1600000000))
    # There is deliberately no permitted volume for a temporary raw input.
    monkeypatch.setattr(hardware, 'fs_type', lambda _: 'tmpfs')
    events, messages = [], []
    result = Backend(root).run(systems={'psp'}, psp_format='cso', immediate=True,
                               on_item=events.append, progress=messages.append)
    outputs = [p.with_suffix('.cso') for p in (*archives, loose)]
    assert not result['errors'] and set(result['converted']) == set(map(str, outputs)), result
    assert not loose.exists() and [p.read_bytes() for p in archives] == before
    assert result['saved'] == 3 * len(original) - sum(p.stat().st_size for p in outputs)
    assert sum('ZIP retained' in line and 'additional bytes' in line for line in messages) == 2
    assert [e.stage for e in events if e.key in set(map(str, archives)) and e.stage in {'preparing', 'done'}] == [
        'preparing', 'done', 'preparing', 'done']
    for p in outputs:
        cso._verify(BytesIO(original), len(original), p, Event(), None)
    assert not list(root.rglob('*.part')) and not list(root.rglob('zip-source*'))


@pytest.mark.parametrize('fault', ['crc', 'cancel'])
def test_bad_or_cancelled_archive_never_publishes_partial_cso(tmp_path, desktop, fault):
    root = tmp_path.resolve()
    original = _iso()
    source = _zip(root / 'psp/Game.zip', {'Title.iso': original}, ZIP_STORED)
    if fault == 'crc':
        # Corrupt only a payload byte beyond the scan prefix; directory, header,
        # geometry and declared sizes remain valid. Failure must be at conversion.
        with source.open('r+b') as f:
            pos = f.read().index(original) + len(original) - 1
            f.seek(pos)
            f.write(bytes([original[-1] ^ 1]))
        os.utime(source, (1600000000, 1600000000))
    before = source.read_bytes()
    cancel, events = Event(), []

    def observe(item):
        events.append(item)
        if fault == 'cancel' and item.stage == 'verifying':
            cancel.set()

    result = Backend(root).run(systems={'psp'}, psp_format='cso', immediate=True, cancel=cancel, on_item=observe)
    assert any(e.stage == 'compressing' for e in events)
    assert not result['converted'] and not source.with_suffix('.cso').exists()
    if fault == 'crc':
        assert any('CRC' in e for e in result['errors'])
    else:
        assert result['paused'] and any(e.stage == 'cancelled' for e in events)
    assert source.read_bytes() == before and not list(root.rglob('*.part'))


def test_gdi_shared_track_materializes_only_the_two_requested_extents(tmp_path):
    root = tmp_path.resolve()
    track1, track2 = b'A' * 2352, b'B' * 4704
    source = _zip(root / 'dc/game.zip', {'folder/game.gdi': b'2\n1 0 4 2352 both.bin 2\n2 1 0 2352 both.bin 2354\n',
                                         'folder/both.bin': b'xx' + track1 + track2})
    archive = scanner.inspect(source, 'dreamcast')
    scratch = root / 'scratch'
    scratch.mkdir()
    staged = zip_stage.prepare(archive, scratch, Event())
    assert staged.frames == (1, 2)
    assert [p.read_bytes() for p in staged.files if p != staged.source] == [track1, track2]
    assert set(scratch.iterdir()) == set(staged.files)
    assert all(p.parent == scratch for p in staged.files)


def test_archive_without_disk_scratch_does_not_block_feasible_loose_game(tmp_path, desktop, monkeypatch):
    root = tmp_path.resolve()
    archive = _zip(root / 'dreamcast/Blocked.zip', {'a.cue': b'FILE "a.bin" BINARY\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n',
                                                    'a.bin': bytes(2352 * 4)})
    loose = root / 'psp/Game.iso'
    loose.parent.mkdir()
    loose.write_bytes(_iso())
    os.utime(loose, (1600000000, 1600000000))
    monkeypatch.setattr(hardware, 'fs_type', lambda _: 'tmpfs')
    events = []
    result = Backend(root).run(systems={'dreamcast', 'psp'}, psp_format='cso', quarantine=False, on_item=events.append)
    assert result['converted'] == [str(loose.with_suffix('.cso'))]
    assert any('RAM-backed' in e for e in result['errors']) and not result['paused']
    assert [e.stage for e in events if e.key == str(archive)] == ['queued', 'skipped']
    assert archive.exists() and not archive.with_suffix('.chd').exists()


def test_mounted_archive_too_large_skips_without_stopping_ingest(tmp_path, desktop, monkeypatch):
    root = (tmp_path / 'dest').resolve()
    root.mkdir()
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    source = _zip(tmp_path.resolve() / 'read-only/Game.zip', {'Inner.iso': _iso()})
    game = scanner.inspect(source, 'psp')
    monkeypatch.setattr(hardware, 'free_bytes', lambda _: GIB)
    events = []
    with Ledger(root) as db:
        batch = db.begin()
        worker = Batch(root, scratch, db, batch, None, hardware.Watchdog(), profiles.CompressionPlan(4, False),
                       False, False, None, Event(), events.append)
        worker.process([Job(game, root / 'Game.chd', False, str(source))])
        db.finish(batch)
    assert worker.errors and not worker.paused and not worker.outputs
    assert events[-1].stage == 'skipped' and list(source.parent.iterdir()) == [source]


def test_space_reserves_output_once_and_only_on_volumes_that_need_it(tmp_path, monkeypatch):
    destination, scratch, network = [tmp_path / p for p in ('out', 'tmp', 'net')]
    devices = {destination: 1, scratch: 2, network: 3}
    original = Path.stat
    monkeypatch.setattr(Path, 'stat', lambda self, *a, **k: SimpleNamespace(st_dev=devices[self])
                        if self in devices else original(self, *a, **k))
    def amounts(prepared, raw, count):
        return {dev: n for dev, (_, n) in space_requirements(destination, prepared, raw, output_bound=5 * GIB,
                                                             scratch_bytes=count * GIB).items()}
    assert amounts(destination, scratch, 0) == {1: 6 * GIB}  # streamed CSO
    assert amounts(destination, scratch, 4) == {1: 6 * GIB, 2: 5 * GIB}
    assert amounts(destination, destination, 4) == {1: 10 * GIB}
    assert amounts(network, scratch, 4) == {1: 6 * GIB, 2: 5 * GIB, 3: 6 * GIB}


@pytest.mark.parametrize('ram_scratch', [False, True])
def test_archived_audio_round_trip_if_chdman_available(tmp_path, desktop, monkeypatch, ram_scratch):
    try:
        tool = converter.tool_path()
    except FileNotFoundError:
        pytest.skip('Install or bundle chdman for a real codec round trip')
    root = tmp_path.resolve() / 'roms'
    payload = bytes(range(196)) * 12 * 80
    source = _zip(root / 'psx/Outer.zip', {'With Spaces/inner.cue': b'FILE "Tracks/01.bin" BINARY\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n',
                                          'With Spaces/Tracks/01.bin': payload})
    scratch = root.parent / 'work'
    scratch.mkdir()
    monkeypatch.setattr(hardware, 'fs_type', lambda path: 'tmpfs' if ram_scratch and path.is_relative_to(scratch) else 'ext4')
    seen = []
    def observe(item):
        if item.stage == 'verifying' and not seen:
            # At the peak, a single prepared CHD is on the destination, and
            # just the track is on the chosen non-RAM scratch filesystem.
            raw, = root.parent.rglob('zip-track*.bin')
            output, = root.parent.rglob('output.chd')
            seen.append((raw.is_relative_to(scratch), output.is_relative_to(root)))
    result = Backend(root).run(systems={'psx'}, scratch_dir=scratch, immediate=True, on_item=observe)
    assert seen == [(not ram_scratch, True)]
    target = source.with_suffix('.chd')
    assert result['converted'] == [str(target)] and not result['errors'], result
    cue = converter.extract(target, root / 'restore', tool)
    assert cue.with_suffix('.bin').read_bytes() == payload
    assert source.exists() and not list(scratch.rglob('*'))
