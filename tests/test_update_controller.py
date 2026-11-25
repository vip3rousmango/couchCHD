import os
from pathlib import Path
import subprocess
import sys
from threading import Event, current_thread, main_thread
from unittest.mock import patch


def test_one_controller_tap_updates_only_while_idle(tmp_path):
    import pytest
    pytest.importorskip('PySide6')
    pytest.importorskip('pygame')
    pytest.importorskip('vdf')
    from couchchd.ui import bridge

    # A separate QGuiApplication must not share the suite's QCoreApplication.
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software',
               XDG_CONFIG_HOME=str(tmp_path), PYTHONPATH=str(Path(bridge.__file__).parents[2]))
    subprocess.run([sys.executable, __file__], env=env, timeout=30, check=True)


def _controller_session():
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
    win, = engine.rootObjects()

    def until(condition):
        for _ in range(150):
            QTest.qWait(20)
            if condition():
                return
        raise AssertionError(f'Unexpected screen: {state.modal}; status: {state.status}')

    # Feed the actual controller action signal into QML: no attached device,
    # library, real release server or device install is needed for this path.
    batch, finish_update = Event(), Event()
    def update(progress):
        assert current_thread() is not main_thread()
        progress('Downloading v0.1.0-preview.5…')
        assert finish_update.wait(8)
        return 'Update installed', 'Close and reopen CouchCHD when ready.'

    try:
        with patch.object(bridge.updater, 'update', side_effect=update) as apply:
            state._work('Compressing discs', lambda: batch.wait(8), lambda _: None)
            pad.action.emit('settings')
            assert state.busy and not state.modal['kind'] and not state._cancel.is_set()
            apply.assert_not_called()
            batch.set()
            until(lambda: not state.busy)
            pad.action.emit('settings')
            until(lambda: state.modal['kind'] == 'settings')
            index = next(i for i, row in enumerate(state.modalRows) if row['label'] == 'Update CouchCHD')
            for _ in range(index):
                pad.action.emit('down')
            pad.action.emit('confirm')
            until(lambda: state.busy and state.status == 'Downloading v0.1.0-preview.5…')
            pad.action.emit('settings')
            pad.action.emit('confirm')
            finish_update.set()
            until(lambda: state.modal['title'] == 'Update installed')
            apply.assert_called_once()
            assert not state.busy and win.isVisible() and 'reopen' in state.modal['body']
    finally:
        batch.set()
        finish_update.set()
        state.close()
        del engine


if __name__ == '__main__':
    _controller_session()
