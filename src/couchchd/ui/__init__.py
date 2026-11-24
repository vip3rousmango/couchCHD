"""CouchCHD's optional controller-first Qt interface."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def launch(root: Path | None = None) -> int:
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from .bridge import Bridge
    from .gamepad import InputRouter

    app = QGuiApplication(sys.argv[:1])
    app.setOrganizationName('CouchCHD')
    app.setApplicationName('CouchCHD')
    QQuickStyle.setStyle('Basic')
    directory = Path(__file__).resolve().parent
    app.setWindowIcon(QIcon(str(directory / 'assets' / 'mark.svg')))
    engine = QQmlApplicationEngine()
    bridge = Bridge(root)
    pad = InputRouter(app)
    pad.warning.connect(bridge.warn)
    bridge.feedback.connect(pad.feedback)
    engine.rootContext().setContextProperty('library', bridge)
    engine.rootContext().setContextProperty('router', pad)
    try:
        engine.load(QUrl.fromLocalFile(str(directory / 'qml' / 'Main.qml')))
        if not engine.rootObjects():
            print('CouchCHD: the bundled QML interface could not be loaded.', file=sys.stderr)
            return 1
        if (os.environ.get('XDG_CURRENT_DESKTOP', '').lower() == 'gamescope' or
                os.environ.get('SteamGamepadUI') == '1' or Path('/recalbox/share').is_dir()):
            engine.rootObjects()[0].showFullScreen()
        QTimer.singleShot(0, pad.start)
        QTimer.singleShot(0, bridge.initialize)
        return app.exec()
    finally:
        pad.close()
        bridge.close()
        # Destroy QML bindings while their context objects still exist.
        del engine
