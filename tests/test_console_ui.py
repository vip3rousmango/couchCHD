import os
from pathlib import Path
import subprocess
import sys
import textwrap

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


def test_single_qml_window_handles_physical_actions(tmp_path):
    # QCoreApplication and QGuiApplication cannot share a process. Isolate Qt
    # platform selection and all user preferences from the workstation.
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software',
               XDG_CONFIG_HOME=str(tmp_path), PYTHONPATH=str(Path(bridge.__file__).parents[2]))
    script = '''
        import sys
        import ctypes
        import pygame as pg
        from pathlib import Path
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtTest import QTest
        from couchchd.ui import bridge
        from couchchd.ui.gamepad import InputRouter

        app = QGuiApplication([])
        engine = QQmlApplicationEngine()
        state, pad = bridge.Bridge(None), InputRouter(app)
        engine.rootContext().setContextProperty('library', state)
        engine.rootContext().setContextProperty('router', pad)
        engine.load(QUrl.fromLocalFile(str(Path(bridge.__file__).parent / 'qml/Main.qml')))
        assert len(engine.rootObjects()) == 1
        win = engine.rootObjects()[0]
        win.requestActivate()
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
