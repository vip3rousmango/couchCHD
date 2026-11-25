import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time

import pytest

pytest.importorskip('PySide6')
pytest.importorskip('pygame')
pytest.importorskip('vdf')

from PySide6.QtCore import QCoreApplication, QSettings
from couchchd.ui import bridge
from couchchd.ui.gamepad import family, stick_direction


@pytest.mark.parametrize(('name', 'expected'), [('DualSense Wireless Controller', 'PlayStation'), ('PS4 Controller', 'PlayStation'), ('Nintendo Switch Pro', 'Nintendo'), ('Xbox One', 'Gamepad')])
def test_face_labels_follow_physical_layout(name, expected):
    assert family(name) == expected


def test_stick_dead_zone_and_dominant_axis():
    assert stick_direction(10000, 2000) == ''
    assert stick_direction(-29500, 9000) == 'left'
    assert stick_direction(-20000, 29000) == 'down'


def test_immediate_cleanup_requires_deliberate_second_choice(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    prefs = QSettings(str(tmp_path / 'preferences.ini'), QSettings.Format.IniFormat)
    monkeypatch.setattr(bridge, '_settings', lambda: prefs)
    state = bridge.Bridge(None)
    try:
        state.settings()
        index = next(i for i, item in enumerate(state.modalRows) if item['key'] == 'policy')
        state.activate(index, False)
        delete = next(i for i, item in enumerate(state.modalRows) if item['arg'] == 'immediate')
        state.activate(delete, False)
        assert state.policy == 'quarantine' and state.modal['kind'] == 'confirm'
        state.activate(0, False)
        assert state.policy == 'quarantine' and not prefs.contains('cleanupPolicy')
        state.settings()
        state.activate(index, False)
        state.activate(delete, False)
        state.activate(1, False)
        assert state.policy == 'immediate'
        assert prefs.value('cleanupPolicy') == 'immediate'
    finally:
        state.close()
    assert app is QCoreApplication.instance()


def test_controller_selection_to_verified_cso_and_measured_free_space(tmp_path, monkeypatch):
    from test_cso import _iso

    app = QCoreApplication.instance() or QCoreApplication([])
    prefs = QSettings(str(tmp_path / 'preferences.ini'), QSettings.Format.IniFormat)
    monkeypatch.setattr(bridge, '_settings', lambda: prefs)
    root = tmp_path / 'roms'
    psp = root / 'psp'
    psp.mkdir(parents=True)
    selected = psp / 'First.iso'
    other = psp / 'Other.iso'
    for path in (selected, other):
        path.write_bytes(_iso())
        os.utime(path, (1600000000, 1600000000))
    nes = root / 'nes' / 'Cart.nes'
    nes.parent.mkdir()
    nes.write_bytes(b'NES\x1a' + bytes([1, 1]) + bytes(10 + 16384 + 8192))
    os.utime(nes, (1600000000, 1600000000))
    output = selected.with_suffix('.cso')
    # Compression is positive but free space *falls* while the backup remains.
    monkeypatch.setattr(bridge.hardware, 'free_bytes', lambda _: (4 if output.exists() else 5) * 1024**3)
    state = bridge.Bridge(root)

    def settle():
        deadline = time.monotonic() + 12
        while state.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert not state.busy, state.status

    def choose(key, arg=None):
        rows = state.modalRows if state.modal['kind'] else state.rows
        index = next(i for i, item in enumerate(rows) if item['key'] == key and (arg is None or item['arg'] == str(arg)))
        state.activate(index, False)

    try:
        state._library(root)
        settle()
        state.settings()
        choose('psp_formats')
        assert next(row['arg'] for row in state.modalRows if row['value'] == 'Current') == 'chd'
        choose('psp_format', 'cso')
        state.back()
        state.switchTab(1)
        choose('system', 'nes')
        state.switchTab(0)
        assert state.summary == '2 of 2 selected'
        choose('bulk')
        assert state.summary == '0 of 2 selected'
        choose('bulk')
        assert state.summary == '2 of 2 selected'
        choose('game', other)
        assert state.summary == '1 of 2 selected'
        state.primary()
        assert 'back' in state.modal['body'].lower() and 'Undo' in state.modal['body']
        choose('accept')
        settle()
        assert state.modal['title'] == 'Batch complete', state.modal
        assert 'Compression difference:' in state.modal['body'] and '1.0 GiB less' in state.modal['body']
        assert '4.0 GiB' in state.space and state.queueModel.verified == 1
        assert output.read_bytes().startswith(b'CISO') and not selected.exists()
        assert other.is_file() and nes.is_file()
        state.back(); state.back()
        choose('filter')
        choose('view', 'Output files')
        choose('info', output)
        assert 'Verified by CouchCHD' in state.modal['body'] and 'RetroAchievements' not in state.modal['body']
        assert not any(row['key'] == 'extract' for row in state.modalRows)
    finally:
        state.close()
    reopened = bridge.Bridge(root)
    try:
        assert reopened.psp_format == 'cso'
    finally:
        reopened.close()


def test_single_qml_window_handles_physical_actions(tmp_path):
    # QCoreApplication and QGuiApplication cannot share a process. Isolate Qt
    # platform selection and all user preferences from the workstation.
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software',
               XDG_CONFIG_HOME=str(tmp_path), PYTHONPATH=str(Path(bridge.__file__).parents[2]))
    script = '''
        import sys
        import ctypes
        import os
        import pygame as pg
        from threading import Event
        from pathlib import Path
        from PySide6.QtCore import QSettings, QUrl
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtQuick import QQuickItem
        from PySide6.QtTest import QTest
        from couchchd.ui import bridge
        from couchchd.ui.gamepad import InputRouter

        app = QGuiApplication([])
        engine = QQmlApplicationEngine()
        prefs = QSettings(str(Path(os.environ['XDG_CONFIG_HOME']) / 'prefs.ini'), QSettings.Format.IniFormat)
        bridge._settings = lambda: prefs
        state, pad = bridge.Bridge(None), InputRouter(app)
        engine.rootContext().setContextProperty('library', state)
        engine.rootContext().setContextProperty('router', pad)
        engine.load(QUrl.fromLocalFile(str(Path(bridge.__file__).parent / 'qml/Main.qml')))
        assert len(engine.rootObjects()) == 1
        win = engine.rootObjects()[0]
        win.requestActivate()
        # The status line must show the worker's queued update *during* a batch,
        # not just after busy clears. No physical/virtual controller needed here.
        release = Event()
        message = "Verifying: My disc.iso (65.4%)"
        def compress():
            state._progress.emit(message)
            release.wait(5)
        state._work("Compressing discs", compress, lambda _: None)
        try:
            for _ in range(100):
                QTest.qWait(20)
                if any(item.isVisible() and item.property('text') == message
                       for item in win.findChildren(QQuickItem)):
                    break
            else:
                raise AssertionError('live status not visible in the QML window')
            assert state.busy
        finally:
            release.set()
        for _ in range(100):
            QTest.qWait(20)
            if not state.busy:
                break
        pad.start()
        if sys.platform == 'win32':
            sdl = ctypes.CDLL(str(Path(pg.__file__).parent / 'SDL2.dll'))
        else:
            # dlsym on this module resolves its own SDL, not another system copy.
            sdl = ctypes.CDLL(pg.base.__file__)
        ident = sdl.SDL_JoystickAttachVirtual(1, 6, 21, 0)
        assert ident >= 0
        if ident != 0:  # Don't commandeer a developer's physical controller.
            sys.exit(77)
        sdl.SDL_JoystickOpen.restype = ctypes.c_void_p
        stick = ctypes.c_void_p(sdl.SDL_JoystickOpen(ident))
        assert stick.value
        for _ in range(100):
            QTest.qWait(20)
            if pad.device != 'Keyboard':
                break
        assert pad.device != 'Keyboard'
        assert sdl.SDL_JoystickSetVirtualButton(stick, pg.CONTROLLER_BUTTON_RIGHTSHOULDER, 1) == 0
        # A complete tap between UI polls must still navigate exactly once.
        pg.event.pump()
        assert sdl.SDL_JoystickSetVirtualButton(stick, pg.CONTROLLER_BUTTON_RIGHTSHOULDER, 0) == 0
        pg.event.pump()
        for _ in range(100):
            QTest.qWait(20)
            if state.section == 1:
                break
        assert state.section == 1
        pad.action.emit('confirm')
        assert 'psx' not in state.enabled_systems
        pad.action.emit('settings')
        assert state.modal['kind'] == 'settings'
        pad.action.emit('confirm')
        assert state.modal['kind'] == 'locations'
        assert len(QGuiApplication.allWindows()) == 1
        pad.action.emit('back')
        assert state.modal['kind'] == 'settings'
        index = next(i for i, r in enumerate(state.modalRows) if r['key'] == 'psp_formats')
        for _ in range(index): pad.action.emit('down')
        pad.action.emit('confirm')
        assert state.modal['kind'] == 'psp_formats'
        pad.action.emit('down'); pad.action.emit('confirm')
        assert state.modal['kind'] == 'settings' and prefs.value('pspFormat') == 'cso'
        sdl.SDL_JoystickClose(stick)
        sdl.SDL_JoystickDetachVirtual(ident)
        pad.close()
        state.close()
        del engine
    '''
    result = subprocess.run([sys.executable, '-c', textwrap.dedent(script)], env=env, timeout=30)
    if result.returncode == 77:
        pytest.skip('Physical controller already in use')
    result.check_returncode()
