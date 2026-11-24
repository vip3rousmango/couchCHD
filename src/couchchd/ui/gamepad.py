"""One SDL controller stream; Qt supplies the window, never SDL."""
from __future__ import annotations

from array import array
import math
import os
import time

from PySide6.QtCore import QObject, Property, QEvent, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication

os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
os.environ.setdefault('SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS', '1')
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
# The SDL2 default maps Nintendo buttons by label, not by physical position.
os.environ['SDL_GAMECONTROLLER_USE_BUTTON_LABELS'] = '0'
import pygame as pg
from pygame._sdl2 import controller


KEYS = {
    Qt.Key.Key_Up: 'up', Qt.Key.Key_Down: 'down', Qt.Key.Key_Left: 'left', Qt.Key.Key_Right: 'right',
    Qt.Key.Key_Return: 'confirm', Qt.Key.Key_Enter: 'confirm', Qt.Key.Key_Space: 'confirm',
    Qt.Key.Key_Escape: 'back', Qt.Key.Key_Backspace: 'back', Qt.Key.Key_X: 'inspect',
    Qt.Key.Key_Y: 'primary', Qt.Key.Key_PageUp: 'previous', Qt.Key.Key_PageDown: 'next',
    Qt.Key.Key_F1: 'settings', Qt.Key.Key_Tab: 'down', Qt.Key.Key_Backtab: 'up',
}
BUTTONS = {
    pg.CONTROLLER_BUTTON_A: 'confirm', pg.CONTROLLER_BUTTON_B: 'back',
    pg.CONTROLLER_BUTTON_X: 'inspect', pg.CONTROLLER_BUTTON_Y: 'primary',
    pg.CONTROLLER_BUTTON_START: 'settings', pg.CONTROLLER_BUTTON_LEFTSHOULDER: 'previous',
    pg.CONTROLLER_BUTTON_RIGHTSHOULDER: 'next', pg.CONTROLLER_BUTTON_DPAD_UP: 'up',
    pg.CONTROLLER_BUTTON_DPAD_DOWN: 'down', pg.CONTROLLER_BUTTON_DPAD_LEFT: 'left',
    pg.CONTROLLER_BUTTON_DPAD_RIGHT: 'right',
}
GLYPHS = {
    'Keyboard': ['Enter', 'Esc', 'X', 'Y', 'F1', 'PgUp/PgDn'],
    'Gamepad': ['A', 'B', 'X', 'Y', 'Start', 'LB/RB'],
    'PlayStation': ['×', '○', '□', '△', 'Options', 'L1/R1'],
    'Nintendo': ['B', 'A', 'Y', 'X', '+', 'L/R'],
}


def family(name: str, guid: str = '') -> str:
    name = name.casefold()
    # SDL encodes the little-endian USB vendor ID at bytes 4–5. This also
    # identifies wireless pads that report only the name "Wireless Controller".
    vendor = guid[8:12].lower() if len(guid) == 32 else ''
    if vendor == '4c05' or any(word in name for word in ('dualshock', 'dualsense', 'playstation', 'ps3', 'ps4', 'ps5', 'sony')):
        return 'PlayStation'
    if vendor == '7e05' or any(word in name for word in ('nintendo', 'switch', 'joy-con', 'joycon')):
        return 'Nintendo'
    return 'Gamepad'


def stick_direction(x: int, y: int) -> str:
    if max(abs(x), abs(y)) < 16500:
        return ''
    if abs(x) > abs(y):
        return 'left' if x < 0 else 'right'
    return 'up' if y < 0 else 'down'


class InputRouter(QObject):
    action = Signal(str)
    changed = Signal()
    warning = Signal(str)

    def __init__(self, app: QGuiApplication):
        super().__init__(app)
        self._app = app
        self._pad = None
        self._family = 'Keyboard'
        self._name = 'Keyboard'
        self._held: set[str] = set()
        self._repeat = 0.0
        self._sounds = {}
        self._audio_failed = False
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._poll)
        self._probe_at = 0.0
        app.installEventFilter(self)

    def start(self):
        try:
            # SDL only needs its event pump, not a second compositor surface.
            pg.display.init()
            controller.init()
            self._timer.start()
        except pg.error as exc:
            self.warning.emit(f'Controller input unavailable: {exc}. Keyboard and pointer still work.')

    @Property('QStringList', notify=changed)
    def glyphs(self):
        return GLYPHS[self._family]

    @Property(str, notify=changed)
    def device(self):
        return self._name

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and event.key() in KEYS:
            if event.modifiers() & (Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
                return False
            # Steam Input keyboard events cannot be reliably distinguished from real ones.
            # With SDL active, consume mapped keys here and dispatch only SDL actions.
            if self._pad is None and event.type() == QEvent.Type.KeyPress:
                action = KEYS[event.key()]
                if not event.isAutoRepeat() or action in ('up', 'down', 'left', 'right'):
                    self.action.emit(action)
            return True
        return False

    def _detach(self):
        if self._pad is not None:
            self._pad.quit()
        self._pad = None
        self._family = self._name = 'Keyboard'
        self._held.clear()
        self.changed.emit()

    def _poll(self):
        now = time.monotonic()
        try:
            pg.event.get()  # drain joystick and controller events, including hotplug
            if self._pad is not None and not self._pad.attached():
                self._detach()
            if self._pad is None and now >= self._probe_at:
                self._probe_at = now + 0.75
                for i in range(controller.get_count()):
                    if controller.is_controller(i):
                        self._pad = controller.Controller(i)
                        self._name = self._pad.name or 'Gamepad'
                        self._family = family(self._name, self._pad.as_joystick().get_guid())
                        self.changed.emit()
                        break
            if self._pad is None:
                return
            down = {action for button, action in BUTTONS.items() if self._pad.get_button(button)}
            stick = stick_direction(self._pad.get_axis(pg.CONTROLLER_AXIS_LEFTX), self._pad.get_axis(pg.CONTROLLER_AXIS_LEFTY))
            if stick:
                down.add(stick)
        except pg.error as exc:
            self._detach()
            self._timer.stop()
            self.warning.emit(f'Controller input stopped: {exc}. Reconnect and restart CouchCHD, or use the keyboard.')
            return
        new = down - self._held
        self._held = down
        if not self._app.focusWindow():
            return
        if new:
            # A chord must not accept a destructive confirmation opened by the same chord.
            for action in ('back', 'settings', 'previous', 'next', 'up', 'down', 'left', 'right', 'inspect', 'primary', 'confirm'):
                if action in new:
                    self.action.emit(action)
                    break
            self._repeat = now + 0.39
        elif now >= self._repeat and len(down) == 1:
            action = next(iter(down))
            if action in ('up', 'down', 'left', 'right'):
                self.action.emit(action)
                self._repeat = now + 0.115

    @Slot(str, bool, bool)
    def feedback(self, kind, sound, haptics):
        if haptics and self._pad is not None and kind in ('done', 'alert'):
            try:
                self._pad.rumble(0.16, 0.29 if kind == 'alert' else 0.13, 190)
            except pg.error:
                pass  # Some virtual controllers report rumble but cannot play it.
        if not sound or self._audio_failed:
            return
        try:
            if not self._sounds:
                rate = 22050
                pg.mixer.init(frequency=rate, size=-16, channels=1, buffer=256, allowedchanges=0)
                for key, notes in {'tick': [(520, .022)], 'done': [(523, .09), (784, .16)], 'alert': [(440, .10), (349, .14)]}.items():
                    pcm = array('h')
                    for hz, duration in notes:
                        samples = int(rate * duration)
                        pcm.extend(int(1900 * math.sin(math.pi * i / samples) ** 2 * math.sin(2 * math.pi * hz * i / rate)) for i in range(samples))
                    self._sounds[key] = pg.mixer.Sound(buffer=pcm.tobytes())
            self._sounds[kind].play()
        except pg.error as exc:
            self._audio_failed = True
            self.warning.emit(f'UI audio is unavailable: {exc}')

    def close(self):
        self._timer.stop()
        self._app.removeEventFilter(self)
        self._detach()
        controller.quit()
        pg.mixer.quit()
        pg.display.quit()
