"""Capture: PYTHONPATH=src python tests/test_queue_ui.py --capture queue.png --size 1280x800

Native input timing (temporary generated ISOs, requires real chdman and 3 GiB free):
    PYTHONPATH=src python tests/test_queue_ui.py --native-latency --input keyboard
    PYTHONPATH=src python tests/test_queue_ui.py --native-latency --input dpad
Set QT_QPA_PLATFORM to the device's platform for on-device measurements (default: offscreen).
"""
import argparse
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import pytest

pytest.importorskip('PySide6')
pytest.importorskip('pygame')
pytest.importorskip('vdf')

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QSettings, QTimer, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QSignalSpy

from couchchd.app import Backend
from couchchd.telemetry import ItemProgress
from couchchd.ui import bridge
from couchchd.ui.gamepad import InputRouter


def test_queue_samples_change_only_the_right_roles_and_row():
    app = QCoreApplication.instance() or QCoreApplication([])
    queue = bridge.QueueModel(app)
    first = ItemProgress('/psx/Disc.cue', 'Disc.cue', 'psx', 'queued')
    second = replace(first, key='/saturn/Disc.cue', system='saturn')
    queue.reset([first, second])
    changes, resets = QSignalSpy(queue.dataChanged), QSignalSpy(queue.modelReset)
    queue.update_item(replace(second, stage='compressing', percent=0., mib_s=0., cpu_percent=0.))
    activity, stats = QSignalSpy(queue.activeChanged), QSignalSpy(queue.statsChanged)
    queue.update_item(replace(second, stage='compressing', percent=37.5, mib_s=9., cpu_percent=42.))
    assert queue.total == 2 and queue.activeIndex == 1 and queue.activeCount == 1
    assert changes.count() == 2 and resets.count() == activity.count() == stats.count() == 0
    top, bottom, roles = changes.at(1)
    assert top.row() == bottom.row() == 1
    assert {queue.roleNames()[r] for r in roles} == {b'phasePercent', b'inputSpeed', b'deviceCpu'}
    assert queue.item(0) == first
    queue.update_item(replace(second, stage='done'))
    queue.finish_pending('paused', 'Battery low')
    assert queue.item(0).stage == 'paused' and queue.item(0).detail == 'Battery low'
    assert queue.item(1).cpu_percent is None and queue.verified == 1 and queue.activeIndex == -1


@pytest.mark.parametrize(('size', 'source'), [('1280x800', 'local'), ('960x540', 'cloud')])
def test_queue_scene_navigation_and_results(tmp_path, size, source):
    paths = (str(Path(bridge.__file__).parents[2]), os.environ.get('PYTHONPATH'))
    subprocess.run([sys.executable, __file__, '--size', size, '--source', source,
                    '--capture', str(tmp_path / 'queue.png')], check=True, timeout=35,
                   env=dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software',
                            PYTHONPATH=os.pathsep.join(filter(None, paths))))


def until(check, message, seconds=8):
    if check(): return
    loop = QEventLoop()
    poll = QTimer(interval=10)
    poll.timeout.connect(lambda: loop.quit() if check() else None)
    deadline = QTimer(singleShot=True, interval=int(seconds * 1000))
    deadline.timeout.connect(loop.quit)
    poll.start(); deadline.start(); loop.exec(); poll.stop(); deadline.stop()
    assert check(), message


def visible_texts(item):
    if not item.isVisible(): return []
    labels = [item.property('text')] if item.metaObject().indexOfProperty('text') >= 0 else []
    for child in item.childItems(): labels.extend(visible_texts(child))
    return labels


class SampleBatch:
    """A fixed timeline, solely for reproducible render and navigation tests."""
    def __init__(self, cloud):
        titles = ['Aster Circuit', 'Blue Meridian', 'Copper Road', 'Harbor Lights', 'Moon Relay', 'Orbit Runner', 'Signal Bay']
        self.items = []
        for i in range(122):
            system, suffix = [('psx', 'cue'), ('ps2', 'iso'), ('saturn', 'cue')][i % 3]
            name = (titles[i] if i < len(titles) else f'Archive {i+1:03d}') + '.' + suffix
            key = ('vault:discs/' if cloud else '/Library/roms/') + system + '/' + name
            self.items.append(ItemProgress(key, name, system, 'queued'))
        self.release = threading.Event()
        self.advance = threading.Event()

    def scan(self):
        return dict(games=[dict(path=i.key, system=i.system, size=512 * 1024**2, tracks=[]) for i in self.items],
                    chds=[], issues=[], playlists=[], verified=[], free=64 * 1024**3)

    def history(self): return []

    def run(self, *, on_item, cancel, **kwargs):
        for item in self.items: on_item(item)
        for item in self.items[:2]: on_item(replace(item, stage='done'))
        on_item(replace(self.items[2], stage='compressing', percent=41.8, mib_s=18.6, cpu_percent=53.4))
        while not self.advance.wait(.1) and not self.release.is_set() and not cancel.is_set(): pass
        on_item(replace(self.items[2], stage='done'))
        on_item(replace(self.items[3], stage='verifying', percent=67.2))
        while not self.release.wait(.1) and not cancel.is_set(): pass
        on_item(replace(self.items[3], stage='cancelled', detail='Input retained.'))
        return dict(saved=174 * 1024**2, errors=[], converted=[i.key for i in self.items[:3]], paused='Cancelled.')


def scenario(state, pad, win, view, args):
    cloud = args.source == 'cloud'
    sample = SampleBatch(cloud)
    state.backend = sample
    state.root = Path('/Library/roms')
    state.tab = 3 if cloud else 0
    state.ingest = 'vault:discs' if cloud else ''
    state.snap = sample.scan()
    state._draw(True)
    state.primary(); state.activate(1, False)
    index = lambda: view.property('currentIndex')
    try:
        until(lambda: state.queueModel.activeIndex == 2 and index() == 2, 'No live active row')
        until(lambda: any('18.6 MiB/s' in s for s in visible_texts(win.contentItem())), 'Rate was not rendered')
        labels = visible_texts(win.contentItem())
        assert '41.8%' in labels and any('53.4%' in s for s in labels) and 'Compressing' in labels
        assert state.queueModel.total == 122 and state.queueModel.verified == 2
        if args.capture:
            image = win.grabWindow()
            assert not image.isNull() and image.save(str(args.capture)), 'No rendered capture'
        for _ in range(6): pad.action.emit('right')
        selected, scrolled = index(), view.property('contentY')
        assert selected > 10 and scrolled > 0
        sample.advance.set()
        until(lambda: state.queueModel.activeIndex == 3, 'Next native phase was not displayed')
        assert index() == selected and view.property('contentY') == scrolled
        for action in ('primary', 'next', 'settings'): pad.action.emit(action)
        assert not state.modal['kind'] and state.busy and state.section == (3 if cloud else 0)
        pad.action.emit('inspect'); assert index() == 3
        pad.action.emit('confirm'); assert state.modal['kind'] == 'item'
        pad.action.emit('back'); assert state.busy and not state.modal['kind']
        pad.action.emit('back'); assert state.modal['kind'] == 'confirm' and not state._cancel.is_set()
        pad.action.emit('down'); pad.action.emit('confirm')
        until(lambda: not state.busy and state.modal['title'] == 'Batch cancelled', 'No cancel result')
        pad.action.emit('back'); assert state.queueVisible and state.queueModel.item(3).stage == 'cancelled'
        pad.action.emit('back'); assert not state.queueVisible and state.rows[0]['key'] == 'lastqueue'
        pad.action.emit('confirm'); assert state.queueVisible and state.queueModel.verified == 3
    finally:
        sample.release.set()


def generated_library(folder):
    import random
    root = folder / 'roms'
    system = root / 'psp'
    system.mkdir(parents=True)
    rng = random.Random(16)
    for i in range(122):
        source = system / f'Source {i+1:03d}.iso'
        with source.open('wb') as stream:
            if i == 0:
                for _ in range(160): stream.write(rng.randbytes(1024 * 1024))
            else: stream.truncate(17 * 2048)
            stream.seek(16 * 2048); stream.write(b'\x01CD001\x01')
        os.utime(source, (1600000000, 1600000000))
    return root


def virtual_dpad(pad):
    import ctypes
    import pygame as pg
    from pygame._sdl2 import controller
    pad.start()
    if controller.get_count(): raise RuntimeError('Disconnect physical controllers, or use --input keyboard.')
    sdl = ctypes.CDLL(str(Path(pg.__file__).parent / 'SDL2.dll') if sys.platform == 'win32' else pg.base.__file__)
    ident = sdl.SDL_JoystickAttachVirtual(1, 6, 21, 0)
    assert ident == 0, 'Could not attach virtual SDL controller'
    sdl.SDL_JoystickOpen.restype = ctypes.c_void_p
    stick = ctypes.c_void_p(sdl.SDL_JoystickOpen(ident))
    assert stick.value
    until(lambda: pad.device != 'Keyboard', 'SDL did not pick up the D-pad')

    def press(down, value):
        button = pg.CONTROLLER_BUTTON_DPAD_DOWN if down else pg.CONTROLLER_BUTTON_DPAD_UP
        assert sdl.SDL_JoystickSetVirtualButton(stick, button, value) == 0

    def close():
        sdl.SDL_JoystickClose(stick); sdl.SDL_JoystickDetachVirtual(ident)
    return press, close


def native_latency(state, pad, win, view, folder, use_dpad):
    """Time off-thread input arrival to a changed, rendered queue selection under load."""
    from couchchd.converter import tool_path
    tool = tool_path()  # fail early if the native executable isn't installed
    state.root = generated_library(folder)
    state.backend = Backend(state.root)
    state.snap = state.backend.scan()
    state.policy = 'keep'
    state._draw(True)
    assert state._selected() and len(state._selected()) == 122
    down = Qt.Key.Key_Down
    def keyboard(forward, value):
        event = QEvent.Type.KeyPress if value else QEvent.Type.KeyRelease
        QCoreApplication.postEvent(win, QKeyEvent(event, down if forward else Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier))
    press, close = virtual_dpad(pad) if use_dpad else (keyboard, lambda: None)
    done = threading.Event()
    presented = threading.Event()
    received = threading.Event()
    times = []
    failures = []
    origin = 0
    def changed():
        if origin and not received.is_set(): received.set()
    def frame():
        if origin and received.is_set() and not presented.is_set():
            times.append((time.monotonic_ns() - origin) / 1e6)
            presented.set()
    view.currentIndexChanged.connect(changed)
    win.frameSwapped.connect(frame, Qt.ConnectionType.DirectConnection)
    def inputs():
        nonlocal origin
        try:
            for n in range(8):
                received.clear(); presented.clear(); origin = time.monotonic_ns()
                press(n % 2 == 0, 1)
                try:
                    assert presented.wait(3), 'No selected-row frame within 3 seconds'
                finally:
                    press(n % 2 == 0, 0)
                    origin = 0
                time.sleep(.07)
        except BaseException as exc:
            failures.append(exc)
        finally: done.set()
    thread = threading.Thread(target=inputs)
    try:
        state.primary(); state.activate(1, False)
        # A sampled percent, unlike the initial preparing/phase event, proves the
        # child has already written actual chdman progress to its output pipe.
        def sampling():
            item = state.queueModel.item(0)
            return item and item.stage == 'compressing' and item.percent is not None
        until(sampling, 'No live sample from the first native compressor: ' + state.status, 35)
        # Lock selection to a safely interior row, permitting alternating directions.
        pad.action.emit('right')
        thread.start(); until(done.is_set, 'Input probe timed out', 30)
        thread.join()
        if failures: raise failures[0]
        assert sampling(), 'Compressor finished before input probe; measurement invalid'
        assert len(times) == 8, times
        print(f'{tool} / {"virtual SDL D-pad" if use_dpad else "Qt arrow keys"}: '
              f'input to swapped selection frame (ms): {[round(ms, 1) for ms in times]}; max {max(times):.1f}', flush=True)
        assert max(times) < 250, f'Input blackout while compressing: {max(times):.0f} ms'
    finally:
        state._cancel.set()
        if thread.is_alive(): thread.join(4)
        win.frameSwapped.disconnect(frame); view.currentIndexChanged.disconnect(changed)
        close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path)
    parser.add_argument('--size', default='1280x800')
    parser.add_argument('--source', choices=('local', 'cloud'), default='local')
    parser.add_argument('--native-latency', action='store_true')
    parser.add_argument('--input', choices=('keyboard', 'dpad'), default='keyboard')
    args = parser.parse_args()
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    os.environ.setdefault('QT_QUICK_BACKEND', 'software')
    app = QGuiApplication([])
    with tempfile.TemporaryDirectory(prefix='couchchd-ui-') as tmp:
        folder = Path(tmp)
        bridge._settings = lambda: QSettings(str(folder / 'preferences.ini'), QSettings.Format.IniFormat)
        state, pad = bridge.Bridge(None), InputRouter(app)
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty('library', state)
        engine.rootContext().setContextProperty('router', pad)
        engine.load(QUrl.fromLocalFile(str(Path(bridge.__file__).parent / 'qml/Main.qml')))
        try:
            win, = engine.rootObjects()
            width, height = map(int, args.size.split('x'))
            win.resize(width, height); win.requestActivate()
            view = win.findChild(QQuickItem, 'queueList')
            assert view is not None
            if args.native_latency: native_latency(state, pad, win, view, folder, args.input == 'dpad')
            else: scenario(state, pad, win, view, args)
        finally:
            state.close(); pad.close()
            from shiboken6 import delete
            delete(engine)


if __name__ == '__main__': main()
