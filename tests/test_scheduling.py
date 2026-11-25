import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from couchchd import converter, hardware, profiles, scanner
from couchchd.batch import Batch, Job
from couchchd.ledger import Ledger


@pytest.fixture
def linux_host(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.delenv('SteamDeck', raising=False)
    monkeypatch.setattr(profiles.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(profiles.platform, 'freedesktop_os_release', lambda: {'ID': 'bazzite'})
    monkeypatch.setattr(profiles, '_DMI', tmp_path)
    monkeypatch.setattr(profiles, '_MODEL', tmp_path / 'model')
    monkeypatch.setattr(hardware, '_MEMINFO', tmp_path / 'meminfo')
    monkeypatch.setattr(hardware, 'cpu_count', lambda: 8)
    (tmp_path / 'meminfo').write_text('MemTotal: 16000000 kB\n')
    return tmp_path


def plans():
    return [profiles.compression_plan(p) for p in ('silent', 'balanced', 'docked')]


@pytest.mark.parametrize('signal', ['product_name', 'board_name', 'SteamDeck', 'SteamOS'])
def test_deck_cpu_budget_is_aggregate_and_never_unlocked_by_docked(linux_host, monkeypatch, signal):
    if signal in ('product_name', 'board_name'):
        (linux_host / signal).write_text('Jupiter\n' if signal == 'product_name' else 'Galileo\n')
    elif signal == 'SteamDeck':
        monkeypatch.setenv(signal, '1')
    else:
        monkeypatch.setattr(profiles.platform, 'freedesktop_os_release', lambda: {'ID': 'steamos'})
    assert plans() == [profiles.CompressionPlan(n, True) for n in (4, 5, 5)]
    monkeypatch.setattr(hardware, 'cpu_count', lambda: 1)
    assert profiles.compression_plan('balanced') == profiles.CompressionPlan(1, True)


@pytest.mark.parametrize(('memtotal_kb', 'budget'), [(900_000, 1), (1_850_000, 1), (3_750_000, 2),
                                                     (7_900_000, 2), ('bad', 1), (None, 1)])
def test_pi_ram_tiers_and_unknown_ram(linux_host, monkeypatch, memtotal_kb, budget):
    (linux_host / 'model').write_bytes(b'Raspberry Pi 4 Model B Rev 1.5\x00')
    meminfo = linux_host / 'meminfo'
    if memtotal_kb is None:
        meminfo.unlink()
    else:
        meminfo.write_text(f'MemTotal: {memtotal_kb} kB\n')
    monkeypatch.setattr(hardware, 'cpu_count', lambda: 4)
    assert plans() == [profiles.CompressionPlan(budget, True)] * 3


def test_desktop_profiles_and_unidentified_low_resource_fallback(linux_host, monkeypatch):
    assert plans() == [profiles.CompressionPlan(n, False) for n in (4, 6, 8)]
    (linux_host / 'meminfo').write_text('MemTotal: 1900000 kB\n')
    assert profiles.compression_plan('docked') == profiles.CompressionPlan(1, True)
    (linux_host / 'meminfo').unlink()
    monkeypatch.setattr(profiles.platform, 'machine', lambda: 'aarch64')
    monkeypatch.setattr(hardware, 'cpu_count', lambda: 4)
    assert profiles.compression_plan('docked') == profiles.CompressionPlan(1, True)


def test_portable_queue_publishes_one_disc_before_preparing_the_next(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    allowed = {2, 4, 6, 8, 10, 12, 14, 16}
    worker_masks = threading.local()
    def affinity(_):
        return getattr(worker_masks, 'cpus', allowed)
    def pin(tid, cpus):
        assert tid == 0 and threading.current_thread() is not threading.main_thread()
        worker_masks.cpus = set(cpus)
    monkeypatch.setattr(os, 'sched_getaffinity', affinity, raising=False)
    monkeypatch.setattr(os, 'sched_setaffinity', pin, raising=False)
    topology = root / 'devices/system/cpu/cpu2/topology'
    topology.mkdir(parents=True)
    (topology / 'thread_siblings_list').write_text('2,16\n')
    monkeypatch.setattr(hardware, '_SYS', root)
    monkeypatch.setattr(hardware, '_power', lambda: (False, None))
    monkeypatch.setattr(hardware, 'free_bytes', lambda path: 16 * 1024**3)
    messages, items = [], []
    jobs = []
    for n in range(5):
        source = root / f'{n}.iso'
        source.write_bytes(bytes(16 * 2048) + b'\x01CD001\x01' + bytes(2048 - 7))
        jobs.append(Job(scanner.inspect(source, 'psp', fresh=False), source.with_suffix('.chd'), True))
    def native_create(game, target, tool, *, threads, **kwargs):
        assert len(affinity(0)) == threads == 5
        assert affinity(0) <= allowed - {2, 16}  # one complete core free for the frontend
        assert kwargs['device_cpus'] == len(allowed)  # before the narrower worker affinity
        target.write_bytes(b'verified output')
    monkeypatch.setattr(converter, 'convert', native_create)
    with Ledger(root) as db:
        batch = db.begin()
        worker = Batch(root, root, db, batch, None, hardware.Watchdog(), profiles.CompressionPlan(5, True),
                       False, False, messages.append, threading.Event(), items.append)
        worker.process(jobs)
        db.finish(batch)
        assert not worker.errors
        assert worker.outputs == [job.output for job in jobs]
    events = [m.split(':', 1)[0] for m in messages if m.startswith(('Preparing:', 'Verified:'))]
    assert events == ['Preparing', 'Verified'] * len(jobs)
    assert [e.key for e in items[:len(jobs)]] == [str(j.game.source) for j in jobs]
    assert all(e.stage == 'queued' for e in items[:len(jobs)])
    for j in jobs:
        assert [e.stage for e in items if e.key == str(j.game.source)] == ['queued', 'preparing', 'publishing', 'done']
    assert affinity(0) == allowed


def test_affinity_denial_falls_back_to_one_and_stops_if_hot(tmp_path, monkeypatch):
    zone = tmp_path / 'class/thermal/thermal_zone0'
    zone.mkdir(parents=True)
    (zone / 'type').write_text('cpu-thermal')
    (zone / 'temp').write_text('79999')
    monkeypatch.setattr(hardware, '_SYS', tmp_path)
    monkeypatch.setattr(os, 'sched_getaffinity', lambda _: {1, 3, 5, 7}, raising=False)
    def deny(*args):
        raise PermissionError()
    monkeypatch.setattr(os, 'sched_setaffinity', deny, raising=False)
    cancel, messages = threading.Event(), []
    with hardware.portable_budget(2, messages.append, cancel) as threads:
        assert threads == 1 and any('without a hard CPU ceiling' in m for m in messages)
        hardware.throttle_child(1234)
        (zone / 'temp').write_text('80000')
        with pytest.raises(InterruptedError, match='Thermal limit'):
            hardware.throttle_child(1234)
    assert cancel.is_set()


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux per-TID affinity and procfs')
def test_running_native_helpers_are_restricted_before_and_after_heating(tmp_path, monkeypatch):
    allowed = os.sched_getaffinity(0)
    if len(allowed) < 3:
        pytest.skip('Need at least three allowed CPUs to observe both reductions')
    chip = tmp_path / 'class/hwmon/hwmon1'
    chip.mkdir(parents=True)
    (chip / 'name').write_text('amdgpu')
    (chip / 'temp1_input').write_text('79000')
    monkeypatch.setattr(hardware, '_SYS', tmp_path)
    probe = tmp_path / 'native.py'
    probe.write_text('''import json, os, threading, time
from pathlib import Path
release = threading.Event()
helpers = [threading.Thread(target=release.wait, daemon=True) for _ in range(3)]
for helper in helpers:
    helper.start()
tids = [os.getpid()] + [t.native_id for t in helpers]
nice = [os.getpriority(os.PRIO_PROCESS, tid) for tid in tids]
def masks():
    return [sorted(os.sched_getaffinity(tid)) for tid in tids]
initial = masks()
Path('class/hwmon/hwmon1/temp1_input').write_text('80000')
end = time.monotonic() + 10
while any(len(m) != 1 for m in masks()) and time.monotonic() < end:
    time.sleep(.02)
Path('observed').write_text(json.dumps([initial, masks(), nice]))
release.set()
''')
    cancel, messages = threading.Event(), []
    ui_nice = os.getpriority(os.PRIO_PROCESS, 0)
    def run_disc():
        with hardware.portable_budget(2, messages.append, cancel) as threads:
            assert threads == 2
            converter._run(Path(sys.executable), probe, cwd=tmp_path, cancel=cancel)
            assert len(os.sched_getaffinity(0)) == 1  # subsequent verify/info inherit the hot limit
        assert os.sched_getaffinity(0) == allowed  # reusable executor thread restored
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(run_disc).result(timeout=20)
    before, after, priorities = json.loads((tmp_path / 'observed').read_text())
    assert min(priorities) >= 10
    assert os.getpriority(os.PRIO_PROCESS, 0) == ui_nice
    assert all(len(mask) == 2 and set(mask) <= allowed for mask in before)
    assert all(mask == after[0] and len(mask) == 1 for mask in after)
    assert os.sched_getaffinity(0) == allowed
    assert messages.count('Thermal limit: reducing this disc to one CPU (80°C or above).') == 1
