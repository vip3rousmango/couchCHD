import errno
import json
import os
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from couchchd import cloud_ingest, hardware, profiles, scanner


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.skipif(os.name == 'nt', reason='Linux mount namespace fixture')
def test_mount_type_deepest_matching_component(tmp_path, monkeypatch):
    card = tmp_path / 'card two'
    nas = card / 'nas'
    nas.mkdir(parents=True)
    mounts = tmp_path / 'mountinfo'
    def record(path, fstype):
        escaped = str(path).replace(' ', r'\040')
        return f'42 1 8:0 / {escaped} rw,relatime shared:2 - {fstype} /dev/sda rw\n'
    mounts.write_text(record('/', 'ext4') + record(nas, 'fuse.sshfs') + record(card, 'vfat'))
    monkeypatch.setattr(hardware, '_MOUNTINFO', mounts)
    monkeypatch.setattr(hardware.os, 'statvfs', lambda p: SimpleNamespace())
    assert hardware.fs_type(nas / 'missing') == 'sshfs'
    assert hardware.fs_type(card) == 'fat32'
    assert hardware.fs_type(tmp_path / 'card two-more') == 'ext4'


def test_free_space_for_future_target(tmp_path):
    assert hardware.free_bytes(tmp_path / 'not-yet' / 'output') > 0


@pytest.mark.skipif(os.name == 'nt', reason='Linux sysfs fixture')
def test_watchdog_power_temperature_and_sleep(tmp_path, monkeypatch):
    monkeypatch.setattr(hardware, '_SYS', tmp_path)
    monkeypatch.setattr(hardware, 'cpu_count', lambda: 8)
    monkeypatch.setattr(hardware, '_suspend_clock_offset', lambda: 0)
    now = [100.0]
    monkeypatch.setattr(hardware.time, 'monotonic', lambda: now[0])
    bat = tmp_path / 'class/power_supply/BAT1'
    put(bat / 'type', b'Battery'); put(bat / 'status', b'Discharging'); put(bat / 'capacity', b'15')
    zone = tmp_path / 'class/thermal/thermal_zone2'
    put(zone / 'type', b'cpu-thermal'); put(zone / 'temp', b'88000')
    watch = hardware.Watchdog()
    assert watch.pause_reason().startswith('Battery') and profiles.default_preset() == 'silent'
    assert watch.limit() == 8
    put(bat / 'status', b'Charging'); put(zone / 'temp', b'88001')
    assert watch.pause_reason() == '' and profiles.default_preset() == 'balanced'
    assert watch.limit() == 1
    watch._prepare_for_sleep(True)
    assert watch.pause_reason().startswith('Suspend')
    watch._prepare_for_sleep(False)
    now[0] += 2.99
    assert watch.pause_reason().startswith('Resume')
    now[0] += .02
    assert not watch.pause_reason()
    monkeypatch.setattr(hardware, '_suspend_clock_offset', lambda: 60)
    assert watch.pause_reason().startswith('Resume')  # no logind on this host


def test_discover_library_roots(tmp_path, monkeypatch):
    home, media, share = (tmp_path / p for p in ('home', 'media', 'recalbox'))
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setattr(profiles, '_MEDIA', media)
    monkeypatch.setattr(profiles, '_RECALBOX', share)
    paths = [home / 'RetroDECK/roms', media / 'deck/SD/Emulation/roms', share / 'roms']
    for p in paths:
        p.mkdir(parents=True)
    assert set(profiles.roots()) == {p.resolve() for p in paths}


@pytest.mark.parametrize('version,enabled', [(b'v1.16.6\n', False), (b'v1.17\n', True),
                                              (b'PPSSPP v1.20.3\n', True), (b'Unknown version\n', False)])
def test_pi_systems_and_psp_floor(monkeypatch, version, enabled):
    monkeypatch.setattr(profiles, 'is_pi', lambda: True)
    monkeypatch.setattr(profiles.shutil, 'which', lambda exe: '/usr/bin/PPSSPPSDL')
    def probe(args, **kwargs):
        assert args == ['/usr/bin/PPSSPPSDL', '--version']
        return subprocess.CompletedProcess(args, 0, stdout=version)
    monkeypatch.setattr(profiles.subprocess, 'run', probe)
    assert profiles.default_systems() == {'psx', 'segacd', 'saturn', 'dreamcast'} | ({'psp'} if enabled else set())
    monkeypatch.setattr(profiles, 'is_pi', lambda: False)
    monkeypatch.setattr(profiles, 'is_recalbox', lambda: False)
    assert len(profiles.default_systems()) == 6


_CUE = b'FILE "tracks/T01.BIN" BINARY\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'


def disc(base, title):
    descriptor = put(base / f'{title}.cue', _CUE)
    track = put(base / 'Tracks/t01.bin', bytes(range(49)) * 48)
    old = time.time() - 20
    for path in (descriptor, track):
        os.utime(path, (old, old))
    return descriptor, track


def test_mounted_ingest_one_disc_at_a_time_and_early_close(tmp_path):
    root, scratch = tmp_path / 'library', tmp_path / 'scratch'
    system = root / 'PS1/Nested'
    a, track = disc(system, 'Disc A (日本)'); b, _ = disc(system, 'Disc B (日本)')
    disc(root / '_couchchd_backup/PS1', 'unused')
    put(system / 'fresh.cue', b'unfinished')
    with closing(cloud_ingest.games(str(root), scratch, {'psx'}, threading.Event())) as iterator:
        game, output = next(iterator)
        assert output == Path('psx/Nested/Disc A (日本).chd') and game.frames == (1,)
        assert all(p.is_relative_to(scratch) for p in game.files)
        assert len(list(scratch.iterdir())) == 1
        game2, output2 = next(iterator)
        assert not game.source.exists() and game2.source.exists()
        assert output2 == Path('psx/Nested/Disc B (日本).chd')
    assert not list(scratch.iterdir())
    assert a.read_bytes() == b.read_bytes() == _CUE and track.stat().st_size == 2352


def test_stage_fails_closed_on_space_copy_mismatch_or_changed_source(tmp_path, monkeypatch):
    source, track = disc(tmp_path / 'remote/psx', 'Disc')
    game = scanner.inspect(source, 'psx')
    scratch = tmp_path / 'scratch'
    with pytest.raises(ValueError, match='outside'):
        with cloud_ingest.stage(game, source.parent / 'scratch'):
            pytest.fail('staged into source library')
    with monkeypatch.context() as patch:
        patch.setattr(hardware, 'free_bytes', lambda path: 0)
        with pytest.raises(OSError) as failure:
            with cloud_ingest.stage(game, scratch):
                pytest.fail('staged without space')
        assert failure.value.errno == errno.ENOSPC
    with monkeypatch.context() as patch:
        original = cloud_ingest._stream_digest
        def damage(src, buffer, cancel, dst=None):
            digest = original(src, buffer, cancel, dst)
            if dst is not None:
                dst.seek(0); dst.write(b'x')
            return digest
        patch.setattr(cloud_ingest, '_stream_digest', damage)
        with pytest.raises(ValueError, match='does not match'):
            with cloud_ingest.stage(game, scratch):
                pytest.fail('yielded corrupt staging')
    assert source.read_bytes() == _CUE and not list(scratch.iterdir())
    track.write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        with cloud_ingest.stage(game, scratch):
            pytest.fail('yielded changed input')


def remote_transport(monkeypatch, objects, *, recent=()):
    """Exercise routing/indexing/real scratch files; stand in only for the network transport."""
    actions = []
    monkeypatch.setattr(cloud_ingest, 'remotes', lambda: ['vault:'])
    def run(args, cancel):
        assert '--' in args
        if args[0] == 'lsjson':
            assert args[-1] == 'vault:'
            return json.dumps([{'Path': k, 'Size': len(data), 'IsDir': False,
                                'ModTime': ('2099' if k in recent else '2000') + '-01-01T00:00:00Z'}
                               for k, data in objects.items()]).encode()
        assert args[:2] == ['copyto', '--']
        source, destination = args[2:]
        assert source.startswith('vault:') and Path(destination).is_absolute()
        key = source.removeprefix('vault:')
        actions.append(key)
        Path(destination).write_bytes(objects[key])
        return b''
    monkeypatch.setattr(cloud_ingest, '_rclone', run)
    return actions


def test_cloud_pulls_only_referenced_tracks_then_cleans_up(tmp_path, monkeypatch):
    objects = {'PS1/One.cue': _CUE, 'PS1/Tracks/t01.bin': b'\0' * 2352,
               'PS1/Two.cue': _CUE, 'PS1/unused.bin': b'not referenced', 'PS1/partial.cue': b'partial'}
    actions = remote_transport(monkeypatch, objects, recent=['PS1/partial.cue'])
    scratch = tmp_path / 'scratch'
    with closing(cloud_ingest.games('vault:', scratch, {'psx'}, threading.Event())) as queue:
        first, path = next(queue)
        assert actions == ['PS1/One.cue', 'PS1/Tracks/t01.bin']
        assert path == Path('psx/One.chd') and first.frames == (1,)
        second, path = next(queue)
        assert not first.source.exists() and second.source.exists() and path == Path('psx/Two.chd')
        assert list(queue) == []
    assert actions == ['PS1/One.cue', 'PS1/Tracks/t01.bin', 'PS1/Two.cue', 'PS1/Tracks/t01.bin']
    assert not list(scratch.iterdir())


@pytest.mark.parametrize('ref', ['../private.bin', '/etc/private.bin', 'C:\\private.bin', 'absent.bin'])
def test_cloud_never_pulls_unsafe_or_missing_tracks(tmp_path, monkeypatch, ref):
    actions = remote_transport(monkeypatch, {'psx/Game.cue': _CUE.replace(b'tracks/T01.BIN', ref.encode())})
    with pytest.raises(ValueError):
        list(cloud_ingest.games('vault:', tmp_path, {'psx'}, threading.Event()))
    assert actions == ['psx/Game.cue'] and not list(tmp_path.iterdir())


def test_bad_listing_and_cancel_leave_no_payload(tmp_path, monkeypatch):
    actions = remote_transport(monkeypatch, {'../escaped.iso': b'unsafe'})
    with pytest.raises(ValueError, match='Unsafe'):
        list(cloud_ingest.games('vault:', tmp_path, {'psx'}, threading.Event()))
    event = threading.Event(); event.set()
    with pytest.raises(InterruptedError):
        next(cloud_ingest.games('vault:', tmp_path, {'psx'}, event))
    assert not actions and not list(tmp_path.iterdir())


def test_rclone_discovery_is_optional_and_accepts_only_named_remotes(monkeypatch):
    monkeypatch.setattr(cloud_ingest.shutil, 'which', lambda _: None)
    assert cloud_ingest.remotes() == []
    monkeypatch.setattr(cloud_ingest.shutil, 'which', lambda _: '/bin/rclone')
    def listing(args, **kwargs):
        assert args == ['/bin/rclone', 'listremotes']
        return subprocess.CompletedProcess(args, 0, stdout='vault:\nlocal nas:\n:backend:\n../bad:\nvault:\n')
    monkeypatch.setattr(cloud_ingest.subprocess, 'run', listing)
    assert cloud_ingest.remotes() == ['local nas:', 'vault:']


def test_inflight_rclone_cancel_reaps_process(monkeypatch):
    import sys

    spawn = subprocess.Popen
    event = threading.Event()
    children = []
    def sleeping_transport(args, **kwargs):
        child = spawn([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child)
        event.set()
        return child
    monkeypatch.setattr(cloud_ingest.subprocess, 'Popen', sleeping_transport)
    with pytest.raises(InterruptedError):
        cloud_ingest._rclone(['lsjson', '--', 'vault:'], event)
    assert children[0].returncode is not None
