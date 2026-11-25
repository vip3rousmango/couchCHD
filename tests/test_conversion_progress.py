"""Exercise live progress and verification through a real child process and disc."""
import errno
import os
import subprocess
import sys
import threading
from itertools import groupby

import pytest

from couchchd import converter, hardware, profiles, scanner
from couchchd.app import Backend
from couchchd.ledger import Ledger


@pytest.fixture
def disc_and_tool(tmp_path, monkeypatch):
    source = tmp_path / 'My disc.iso'
    source.write_bytes(bytes(16 * 2048) + b'\x01CD001\x01' + bytes(2048 - 7))
    game = scanner.inspect(source, 'psp', fresh=False)
    tool = tmp_path / 'native.py'
    tool.write_text(r'''
import os, sys, time
from pathlib import Path

cmd, *args = sys.argv[1:]
root = Path(__file__).parent
fault = os.environ.get('PROGRESS_TEST_FAULT', '')

def live(prefix, percent, marker=None):
    # Two writes, no line feed. Do not finish the command until the user-facing
    # callback has seen the actual percentage (an exit-time log scrape fails).
    os.write(2, b'\r' + prefix + b', ' + percent[:2])
    os.write(2, percent[2:] + b'% complete... (ratio=3.3%)  \r')
    deadline = time.monotonic() + 8
    while not (root / (marker or cmd)).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('no live update for ' + cmd)
        time.sleep(.01)

if cmd == 'help':
    print('chdman - MAME Compressed Hunks of Data (CHD) manager 0.267')
    sys.exit(1)
if cmd == 'createdvd':
    Path(args[args.index('-o') + 1]).write_bytes(b'candidate')
    if not fault or fault == 'measure':
        live(b'Compressing', b'12.5')
        if fault == 'measure':
            live(b'Compressing', b'62.5', 'createdvd-next')
        for n in range(600):
            os.write(2, b'Compressing, %d.0%% complete... (ratio=3.3%%)  \r' % (13 + n % 70))
    if fault == 'full':
        os.write(2, b'No space left on device\n' + b'x' * 200000)
        sys.exit(1)
if cmd == 'verify':
    if not fault or fault == 'measure':
        live(b'Verifying', b'65.4')
    if fault == 'bad':
        os.write(2, b'Error: mismatched hash\n')
    if fault != 'missing':
        os.write(1, b'Raw SHA1 verification successful!\n')
    os.write(2, b'x' * 200000 + b'\r')  # larger than the retained output; no LF
    os.write(1, b'Overall SHA1 verification successful!\n')
if cmd == 'info':
    print("Metadata:     Tag='DVD '  Index=0  Length=1 bytes")
    print('Logical size: 34,816 bytes\nHunk Size: 2,048 bytes\nUnit Size: 2,048 bytes\nTotal Units: 17')
''', encoding='utf-8')
    # Run the stand-in through Python, not a POSIX-only executable shebang.
    popen = subprocess.Popen
    def python_child(argv, **kwargs):
        return popen([sys.executable, *argv], **kwargs)
    monkeypatch.setattr(converter.subprocess, 'Popen', python_child)
    return game, tool


def test_carriage_return_updates_visible_before_create_and_verify_finish(disc_and_tool, monkeypatch):
    monkeypatch.setattr(converter, 'StageSampler', lambda *args: pytest.fail('CPU sampled for text-only CLI'))
    game, tool = disc_and_tool
    messages = []
    def show(message):
        messages.append(message)
        if message == 'Compressing: My disc.iso (12.5%)':
            (tool.parent / 'createdvd').touch()
        if message == 'Verifying: My disc.iso (65.4%)':
            (tool.parent / 'verify').touch()
    output = game.source.with_suffix('.chd')
    converter.convert(game, output, tool, cancel=threading.Event(), threads=1, progress=show)
    assert output.read_bytes() == b'candidate'
    assert messages.index('Compressing: My disc.iso') < messages.index('Compressing: My disc.iso (12.5%)')
    assert messages.index('Verifying: My disc.iso') < messages.index('Verifying: My disc.iso (65.4%)')
    assert len(messages) < 20  # a burst of native CR updates does not flood the UI
    assert all('3.3%' not in message and '100%' not in message for message in messages)


@pytest.mark.parametrize('fault', ['bad', 'missing', 'full'])
def test_noisy_native_output_cannot_hide_failed_verification_or_full_disk(disc_and_tool, monkeypatch, fault):
    game, tool = disc_and_tool
    output = game.source.with_suffix('.chd')
    original = game.source.read_bytes()
    cancel = threading.Event()
    monkeypatch.setenv('PROGRESS_TEST_FAULT', fault)
    with pytest.raises(OSError if fault == 'full' else ValueError) as failure:
        converter.convert(game, output, tool, cancel=cancel, threads=1)
    if fault == 'full':
        assert failure.value.errno == errno.ENOSPC and cancel.is_set()
    assert not output.exists() and not list(tool.parent.rglob('*.part'))
    assert game.source.read_bytes() == original


@pytest.mark.parametrize('abort', ['cancel', 'callback_error'])
def test_interrupted_live_verification_never_publishes(disc_and_tool, abort):
    game, tool = disc_and_tool
    output = game.source.with_suffix('.chd')
    original = game.source.read_bytes()
    cancel = threading.Event()
    def show(message):
        if message == 'Compressing: My disc.iso (12.5%)':
            (tool.parent / 'createdvd').touch()
        if message == 'Verifying: My disc.iso (65.4%)':
            if abort == 'cancel':
                cancel.set()
            else:
                raise RuntimeError('progress callback failed')
    with pytest.raises(InterruptedError if abort == 'cancel' else RuntimeError,
                       match='cancelled' if abort == 'cancel' else 'callback failed'):
        converter.convert(game, output, tool, cancel=cancel, threads=1, progress=show)
    assert game.source.read_bytes() == original
    assert not output.exists() and not list(tool.parent.rglob('*.part'))


@pytest.fixture
def library(disc_and_tool, monkeypatch):
    game, tool = disc_and_tool
    root = tool.parent.resolve() / 'library'
    system = root / 'psp'
    system.mkdir(parents=True)
    source = game.source.rename(system / game.source.name)
    os.utime(source, (1600000000, 1600000000))
    monkeypatch.setattr(converter, 'tool_path', lambda: tool)
    monkeypatch.setattr(profiles, 'compression_plan', lambda _: profiles.CompressionPlan(1, False))
    monkeypatch.setattr(hardware, 'free_bytes', lambda _: 16 * 1024**3)
    monkeypatch.setattr(hardware.Watchdog, 'pause_reason', lambda _: '')
    monkeypatch.setenv('PROGRESS_TEST_FAULT', 'quiet')
    return Backend(root), source, tool


def test_backend_measures_live_stage_deltas_and_skips_already_verified(library, monkeypatch):
    app, source, tool = library
    monkeypatch.setenv('PROGRESS_TEST_FAULT', 'measure')
    events = []
    def observe(item):
        events.append(item)
        # Let each process advance only after the corresponding live Qt-facing
        # event; log scraping after exit cannot satisfy this protocol.
        marker = {('compressing', 12.5): 'createdvd', ('compressing', 62.5): 'createdvd-next',
                  ('verifying', 65.4): 'verify'}.get((item.stage, item.percent))
        if marker:
            (tool.parent / marker).touch()
    result = app.run(systems={'psp'}, quarantine=False, on_item=observe)
    assert not result['errors'] and result['converted'] == [str(source.with_suffix('.chd'))]
    assert {(e.key, e.name, e.system) for e in events} == {(str(source), source.name, 'psp')}
    assert [k for k, _ in groupby(e.stage for e in events)] == [
        'queued', 'preparing', 'compressing', 'verifying', 'publishing', 'done']
    assert any(e.stage == 'compressing' and e.percent == 62.5 and e.mib_s > 0
               for e in events if e.mib_s is not None)
    assert next(e for e in events if e.stage == 'verifying').percent is None
    assert events[-1].percent == 100 and events[-1].mib_s is None and events[-1].cpu_percent is None
    events.clear()
    assert not app.run(systems={'psp'}, on_item=observe)['converted']
    assert len(events) == 1 and events[0].stage == 'skipped' and events[0].key == str(source)


@pytest.mark.parametrize(('stop', 'expected'), [('battery', 'paused'), ('storage', 'paused'), ('cancel', 'cancelled')])
def test_unstarted_sources_finish_without_fabricated_work(library, monkeypatch, stop, expected):
    app, source, _ = library
    other = source.with_name('Another.iso')
    other.write_bytes(source.read_bytes())
    os.utime(other, (1600000000, 1600000000))
    cancel = threading.Event()
    if stop == 'battery':
        monkeypatch.setattr(hardware.Watchdog, 'pause_reason', lambda _: 'Battery at 5%')
    elif stop == 'storage':
        monkeypatch.setattr(hardware, 'free_bytes', lambda _: 0)
    else:
        cancel.set()
    events = []
    result = app.run(systems={'psp'}, on_item=events.append, cancel=cancel)
    assert result['paused'] and not result['converted']
    for disc in (source, other):
        row = [e for e in events if e.key == str(disc)]
        assert [e.stage for e in row] == ['queued', expected]
        assert all((e.percent, e.cpu_percent, e.mib_s) == (None, None, None) for e in row)
        assert disc.exists()


@pytest.mark.parametrize(('fault', 'stage'), [('bad', 'failed'), ('full', 'paused'), ('publish-cancel', 'cancelled')])
def test_failed_or_cancelled_jobs_never_announce_done(library, monkeypatch, fault, stage):
    app, source, _ = library
    monkeypatch.setenv('PROGRESS_TEST_FAULT', fault)
    cancel, events = threading.Event(), []
    def observe(item):
        events.append(item)
        if fault == 'publish-cancel' and item.stage == 'publishing':
            cancel.set()
    result = app.run(systems={'psp'}, on_item=observe, cancel=cancel)
    assert not result['converted'] and result['errors']
    assert events[-1].stage == stage and events[-1].detail and events[-1].percent is None
    assert not any(e.stage == 'done' for e in events)
    assert source.exists() and not source.with_suffix('.chd').exists()


def test_cleanup_failure_keeps_verified_status(library, monkeypatch):
    app, source, _ = library
    def no_quarantine(*args, **kwargs):
        raise OSError('backup unavailable')
    monkeypatch.setattr(Ledger, 'quarantine', no_quarantine)
    events = []
    result = app.run(systems={'psp'}, on_item=events.append)
    assert result['errors'] and result['converted'] == [str(source.with_suffix('.chd'))]
    assert events[-1].stage == 'done' and 'backup unavailable' in events[-1].detail
    assert app.scan()['verified'] == result['converted'] and source.exists()


def test_ingest_keeps_mounted_source_identity_through_publication(library):
    app, source, tool = library
    destination = tool.parent / 'destination'
    destination.mkdir()
    events = []
    result = Backend(destination).run(systems={'psp'}, ingest=str(app.root), on_item=events.append)
    assert not result['errors'] and source.exists()
    assert {e.key for e in events} == {str(source)}
    assert [k for k, _ in groupby(e.stage for e in events)] == [
        'queued', 'preparing', 'compressing', 'verifying', 'publishing', 'done']


def test_ambiguous_and_vanished_selected_sources_each_get_their_own_row(library):
    app, source, _ = library
    other = source.parent / 'different folder' / source.name
    other.parent.mkdir()
    other.write_bytes(source.read_bytes())
    os.utime(other, (1600000000, 1600000000))
    missing = source.with_name('vanished.iso')
    selected = {str(path) for path in (source, other, missing)}
    events = []
    result = app.run(systems={'psp'}, sources=selected, on_item=events.append)
    assert not result['converted'] and len(result['errors']) == len(selected)
    assert {e.key for e in events if e.stage == 'failed'} == selected


def test_coordinator_abort_drains_active_and_retires_pending_rows(library, monkeypatch):
    app, source, _ = library
    other = source.with_name('Another.iso')
    other.write_bytes(source.read_bytes())
    os.utime(other, (1600000000, 1600000000))
    monkeypatch.setenv('PROGRESS_TEST_FAULT', '')  # child awaits an acknowledgement it will never get
    calls = 0
    def failing_guard(_):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError('mount monitor failed')
        return ''
    monkeypatch.setattr(hardware.Watchdog, 'pause_reason', failing_guard)
    events = []
    result = app.run(systems={'psp'}, on_item=events.append)
    assert result['errors'] and not result['converted']
    latest = {e.key: e for e in events}
    assert set(latest) == {str(source), str(other)}
    assert {e.stage for e in latest.values()} == {'cancelled', 'skipped'}
    assert source.exists() and other.exists() and not list(app.root.rglob('*.part'))
