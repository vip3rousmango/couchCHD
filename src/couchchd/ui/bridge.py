"""Presentation state for the single QML scene. Disk and process work stays off the Qt thread."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
import hashlib
from pathlib import Path
import threading

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QObject, Property, QDir,
                            QSettings, QStandardPaths, Qt, Signal, Slot)

from ..app import Backend
from .. import cloud_ingest, hardware, profiles, scanner, updater
from ..output_formats import SUFFIX_BY_MODE
from ..telemetry import ItemProgress
from . import launcher_art, steam

PRESETS = {'silent': 'Handheld Silent', 'balanced': 'Balanced', 'docked': 'Docked Max'}
TITLES = ['Dashboard', 'Systems', 'Multi-disc & M3U', 'Cloud ingest', 'History & backups']
PSP_FORMATS = {
    'chd': 'CHD · PPSSPP 1.17 or newer',
    'cso': 'CSO · Also works with older PPSSPP versions',
}


def _output_label(system, ext, psp_format):
    if system == 'psp':
        return psp_format.upper()
    mode = scanner.default_mode(system, ext)
    if mode == 'trim3ds':
        return 'Trimmed 3DS'
    if mode == 'xiso':
        return 'XISO'
    return SUFFIX_BY_MODE[mode].lstrip('.').upper()


def size(count: int) -> str:
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if abs(count) < 1024 or unit == 'GiB':
            return f'{count:,.0f} B' if unit == 'B' else f'{count:,.1f} {unit}'
        count /= 1024


def row(key, label, detail='', value='', arg=''):
    return {'key': key, 'label': str(label), 'detail': str(detail), 'value': str(value), 'arg': str(arg)}


def _settings():
    if Path('/recalbox/share/system').is_dir():
        directory = Path('/recalbox/share/system/couchchd')
    else:
        directory = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))
    return QSettings(str(directory / 'preferences.ini'), QSettings.Format.IniFormat)


def _discover():
    return (profiles.roots(), profiles.default_systems(), profiles.default_preset(), profiles.is_recalbox(), profiles.is_pi())


def _snapshot(backend):
    return backend.scan(), backend.history()


def _directories(path: Path):
    folders = sorted((p for p in path.iterdir() if p.is_dir()), key=lambda p: p.name.casefold())
    return path, folders


def _when(value):
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value).astimezone().strftime('%Y-%m-%d %H:%M')
    return str(value or 'Not recorded')


POLICIES = {
    'quarantine': ('Keep a backup', 'Back up eligible originals so you can Undo. Backups still use space.'),
    'keep': ('Keep in place', 'Leave originals in their current folders.'),
    'immediate': ('Delete immediately', 'Permanently delete eligible verified originals; Undo cannot restore them.'),
}


class QueueModel(QAbstractListModel):
    """One row per source, owned exclusively by the GUI thread."""
    statsChanged = Signal()
    activeChanged = Signal()
    active_stages = frozenset(('preparing', 'compressing', 'verifying', 'publishing'))
    _fields = ('name', 'system', 'stage', 'percent', 'mib_s', 'cpu_percent', 'detail')
    _first = int(Qt.ItemDataRole.UserRole) + 1
    _roles = dict(enumerate((b'discName', b'systemName', b'phase', b'phasePercent',
                             b'inputSpeed', b'deviceCpu', b'itemDetail'), _first))

    def __init__(self, parent):
        super().__init__(parent)
        self._items: list[ItemProgress] = []
        self._by_key: dict[str, int] = {}
        self._active: set[int] = set()
        self._done = 0
        self._first_active = -1

    def roleNames(self):
        return self._roles

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return item.name
        if role not in self._roles:
            return None
        field = self._fields[role - self._first]
        value = getattr(item, field)
        if field == 'system':
            return scanner.SYSTEM_LABELS.get(value, value)
        # QML numeric properties can't distinguish null from measured zero.
        return -1.0 if value is None else value

    @Property(int, notify=statsChanged)
    def total(self): return len(self._items)

    @Property(int, notify=statsChanged)
    def verified(self): return self._done

    @Property(int, notify=statsChanged)
    def activeCount(self): return len(self._active)

    @Property(int, notify=activeChanged)
    def activeIndex(self): return self._first_active

    def reset(self, items=()):
        self.beginResetModel()
        self._items = list(items)
        self._by_key = {item.key: i for i, item in enumerate(self._items)}
        self._active.clear()
        self._first_active = -1
        self._done = 0
        self.endResetModel()
        self.statsChanged.emit()
        self.activeChanged.emit()

    def item(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    @Slot(object)
    def update_item(self, item):
        index = self._by_key.get(item.key)
        if index is None:
            index = len(self._items)
            self.beginInsertRows(QModelIndex(), index, index)
            self._by_key[item.key] = index
            self._items.append(item)
            self.endInsertRows()
            previous = None
        else:
            prior = self._items[index]
            roles = [self._first + i for i, field in enumerate(self._fields)
                     if getattr(prior, field) != getattr(item, field)]
            if not roles:
                return
            self._items[index] = item
            cell = self.index(index, 0)
            self.dataChanged.emit(cell, cell, roles)
            previous = prior.stage
        if previous == item.stage:
            return
        was_active = previous in self.active_stages
        active = item.stage in self.active_stages
        if was_active != active:
            if active:
                self._active.add(index)
            else:
                self._active.discard(index)
            first = min(self._active, default=-1)
            if self._first_active != first:
                self._first_active = first
                self.activeChanged.emit()
        self._done += (item.stage == 'done') - (previous == 'done')
        if previous is None or was_active != active or 'done' in (previous, item.stage):
            self.statsChanged.emit()

    def finish_pending(self, stage, detail):
        for item in self._items:
            if item.stage == 'queued' or item.stage in self.active_stages:
                self.update_item(replace(item, stage=stage, detail=detail, percent=None,
                                         mib_s=None, cpu_percent=None))


class Bridge(QObject):
    changed = Signal()
    statusChanged = Signal()
    operationChanged = Signal()
    spaceChanged = Signal()
    sceneChanged = Signal(bool)  # reset selection on navigation, not toggles
    quitRequested = Signal()
    feedback = Signal(str, bool, bool)
    _finished = Signal(object, object)
    _progress = Signal(str)
    _item = Signal(object)
    _space = Signal(object)  # bytes may exceed Qt's signed 32-bit int
    queueChanged = Signal()

    def __init__(self, root: Path | None):
        super().__init__()
        self.prefs = _settings()
        last = self.prefs.value('library', '', type=str)
        self.root = root if root is not None else Path(last) if last else None
        self.candidates = []
        self.enabled_systems = set(scanner.SYSTEM_LABELS)
        self.recommended = set(scanner.SYSTEM_LABELS)
        self.psp_manually_toggled = False
        saved_format = self.prefs.value('pspFormat', 'chd', type=str)
        self.psp_format = saved_format if saved_format in PSP_FORMATS else 'chd'
        self.pi = self.recalbox = False
        policy = self.prefs.value('cleanupPolicy', 'quarantine', type=str)
        self.policy = policy if policy in POLICIES else 'quarantine'
        self.sound = self.prefs.value('sound', False, type=bool)
        self.rumble = self.prefs.value('rumble', True, type=bool)
        self.preset = 'balanced'
        self.backend = None
        self._free = None
        self.snap = None
        self.history = []
        self.omitted = set()
        self.saved = None
        self._queue = QueueModel(self)
        self._queue_visible = False
        self._queue_cloud = False
        self._queue_result = ''
        self.tab = 0
        self.filter = 'Queue'
        self.ingest = ''
        self.remote_names = None
        self._rows = []
        self._modal = None
        self._dialog_rows = []
        self._target = ''
        self._extract_chd = None
        self._dir = None
        self._directory_trail = []
        self._return_to = ''
        self._confirm = None
        self._status = 'Opening library…'
        self._busy = ''
        self._primary = 'Choose folder'
        self._summary = ''
        self._subtitle = ''
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='library')
        self._cancel = threading.Event()
        self._cancel_requested = False
        self._on_success = None
        self._finished.connect(self._complete, Qt.ConnectionType.QueuedConnection)
        self._progress.connect(self._report)
        self._item.connect(self._queue.update_item, Qt.ConnectionType.QueuedConnection)
        self._space.connect(self._set_space, Qt.ConnectionType.QueuedConnection)

    @Property(QObject, constant=True)
    def queueModel(self): return self._queue

    @Property(bool, notify=queueChanged)
    def queueVisible(self): return self._queue_visible

    @Property(str, notify=queueChanged)
    def queueTitle(self): return 'Cloud ingest queue' if self._queue_cloud else 'Compression queue'

    @Property(str, notify=queueChanged)
    def queueResult(self): return self._queue_result

    @Property(bool, notify=operationChanged)
    def queueCancelling(self): return self._busy == 'Compressing discs' and self._cancel_requested

    @Slot(int)
    def inspectQueueItem(self, index):
        if not self._queue_visible or self._modal:
            return
        item = self._queue.item(index)
        if item is not None:
            body = '\n'.join(filter(None, (item.key, item.detail)))
            self._show('item', item.name, body, [row('back', 'Return to queue')])

    @Property('QVariantList', notify=changed)
    def rows(self): return self._rows

    @Property('QVariantList', notify=changed)
    def modalRows(self): return self._dialog_rows

    @Property('QVariantMap', notify=changed)
    def modal(self): return self._modal or {'kind': '', 'title': '', 'body': ''}

    @Property(int, notify=changed)
    def section(self): return self.tab

    @Property(str, notify=changed)
    def primaryLabel(self): return self._primary

    @Property(str, notify=changed)
    def subtitle(self): return self._subtitle

    @Property(str, notify=changed)
    def summary(self): return self._summary

    @Property(str, notify=changed)
    def location(self): return str(self.root) if self.root else 'No library selected'

    @Property(str, notify=statusChanged)
    def status(self): return self._status

    @Property(bool, notify=operationChanged)
    def busy(self): return bool(self._busy)

    @Property(str, notify=operationChanged)
    def operation(self): return self._busy

    @Property(str, notify=spaceChanged)
    def space(self): return size(self._free) if self._free is not None else '—'

    @Slot(object)
    def _set_space(self, free):
        if self._free != free:
            self._free = free
            self.spaceChanged.emit()

    @Property(str, notify=changed)
    def savings(self): return size(self.saved) if self.saved is not None else '—'

    @Property('QStringList', constant=True)
    def sections(self): return TITLES

    @Slot()
    def initialize(self):
        def ready(data):
            self.candidates, self.recommended, default, self.recalbox, self.pi = data
            self.enabled_systems = set(self.recommended)
            if self.psp_format == 'cso':
                self.enabled_systems.add('psp')
            preset = self.prefs.value('preset', default, type=str)
            self.preset = preset if preset in PRESETS else default
            root = self.root or next(iter(self.candidates), None)
            if root:
                self._library(Path(root))
            else:
                self._report('Choose the folder that contains your system folders.')
                self._draw(True)
        self._work('Detecting libraries', _discover, ready)
        if launcher := launcher_art.managed_launcher():
            self._pool.submit(launcher_art.refresh, launcher)

    def _refresh_after_commit(self, show_result):
        # A disappearing SD card may break the subsequent scan, but does not
        # invalidate a commit. Show both outcomes rather than hiding the first.
        def refresh():
            try:
                return _snapshot(self.backend), ''
            except Exception as exc:
                return None, f'Library refresh failed: {exc}. Reconnect the library and scan again.'
        def finished(data):
            snapshot, warning = data
            if snapshot is not None:
                self._install_scan(snapshot)
            else:
                self.snap, self.history = None, []
                self._set_space(None)
            self._draw(False)
            show_result(warning)
        self._work('Refreshing library', refresh, finished)

    def _work(self, title, fn, done):
        if self._busy:
            return
        self._busy = title
        self._on_success = done
        self.operationChanged.emit()
        self._report(title + '…')
        future = self._pool.submit(fn)
        def finish(f):
            try:
                result = f.result()
            except Exception as exc:
                self._finished.emit(None, str(exc) or type(exc).__name__)
            else:
                self._finished.emit(result, None)
        future.add_done_callback(finish)

    @Slot(object, object)
    def _complete(self, result, error):
        done = self._on_success
        self._on_success = None
        batch_failed = error is not None and self._busy == 'Compressing discs'
        self._busy = ''
        self.operationChanged.emit()
        try:
            if error is not None:
                if batch_failed:
                    self._queue_result = 'Batch stopped'
                    self._queue.finish_pending('failed', str(error))
                    self.queueChanged.emit()
                self._report(str(error))
                self._draw(False)
                self._notice('Operation stopped', str(error), alert=True)
            else:
                done(result)
        except Exception as exc:
            self._notice('Unable to display result', str(exc), alert=True)

    @Slot(str)
    def _report(self, text):
        self._status = text
        self.statusChanged.emit()

    @Slot(str)
    def warn(self, text):
        self._report(text)
        self._notice('Device feedback', text)

    @Slot(str)
    def cue(self, kind):
        self.feedback.emit(kind, self.sound, self.rumble)

    def _saved_key(self):
        return 'results/' + hashlib.sha256(str(self.root).encode()).hexdigest() + '/lastSaved'

    def _library(self, path):
        def open_library():
            path_abs = path.expanduser().resolve(strict=True)
            if not path_abs.is_dir():
                raise ValueError('The selected library is not a directory.')
            backend = Backend(path_abs)
            backend.recover()
            return path_abs, backend, _snapshot(backend)
        def opened(data):
            self.root, self.backend, result = data
            self.prefs.setValue('library', str(self.root))
            self.saved = int(self.prefs.value(self._saved_key())) if self.prefs.contains(self._saved_key()) else None
            self.omitted.clear()
            self._queue_result = ''
            self._queue_visible = False
            self._queue.reset()
            self.queueChanged.emit()
            self._install_scan(result)
            self._report('Scan complete.')
            self._draw(True)
        self._hide()
        self._work('Reading library', open_library, opened)

    def _install_scan(self, result):
        self.snap, self.history = result
        self._set_space(self.snap['free'])
        self.omitted.intersection_update(str(g['path']) for g in self.snap['games'])

    def _scan(self):
        if self.backend is None:
            return self._pick('library')
        def scanned(result):
            self._install_scan(result)
            self._report('Scan complete.')
            self._draw(True)
        self._work('Scanning library', lambda: _snapshot(self.backend), scanned)

    def _completed(self, game):
        mode = 'cso' if game['system'] == 'psp' and self.psp_format == 'cso' else game.get('mode')
        return mode in game.get('verified_modes', ())

    def _eligible(self):
        return {str(g['path']) for g in (self.snap or {}).get('games', ())
                if g['system'] in self.enabled_systems and not self._completed(g)}

    def _selected(self):
        return self._eligible() - self.omitted

    def _draw(self, reset):
        snap = self.snap or {'games': [], 'playlists': [], 'issues': [], 'chds': [], 'verified': []}
        rows = []
        eligible = self._eligible()
        selected = eligible - self.omitted
        primary = 'Start batch' if selected else 'Scan library'
        self._summary = f'{len(selected)} of {len(eligible)} selected'
        if self.tab == 0:
            self._subtitle = 'Review sources and their output formats before conversion.'
            if self.backend is None:
                primary = 'Choose folder'
                rows.append(row('library', 'Choose a ROM library', 'Select the folder that contains psx, dreamcast, or other system folders.'))
            else:
                rows.append(row('filter', self.filter, 'Change library view', 'View'))
                if self.filter == 'Queue':
                    if eligible:
                        all_selected = selected == eligible
                        rows.append(row('bulk', 'Clear selection' if all_selected else 'Select all eligible games',
                                        'Applies to enabled systems. Select individual games below.'))
                    for g in snap['games']:
                        path = str(g['path'])
                        flag = ('Already verified' if self._completed(g) else
                                'System off' if g['system'] not in self.enabled_systems else
                                'Selected' if path in selected else 'Skipped')
                        system = g['system']
                        output = _output_label(system, Path(path).suffix.lower(), self.psp_format)
                        rows.append(row('game', Path(path).name, f"{scanner.SYSTEM_LABELS[system]} → {output}  ·  {size(g['size'])}", flag, path))
                    if not snap['games']:
                        rows.append(row('scan', 'No ready sources', 'Add supported images or cartridge ROMs, then scan again.', 'Rescan'))
                elif self.filter == 'Output files':
                    verified = set(map(str, snap['verified']))
                    paths = sorted(verified.union(map(str, snap['chds'])))
                    for path in paths:
                        key = 'chd' if Path(path).suffix.casefold() == '.chd' else 'info'
                        detail = 'Verified by CouchCHD' if path in verified else 'Verification not recorded by CouchCHD'
                        rows.append(row(key, Path(path).name, detail, 'Inspect', path))
                    if not paths:
                        rows.append(row('scan', 'No output files found', 'Verified conversions will appear here.', 'Rescan'))
                else:
                    for issue in snap['issues']:
                        category = str(issue['category']).casefold()
                        unpack = 'unpack' in category or 'archive' in category
                        if unpack == (self.filter == 'Needs Unpacking'):
                            rows.append(row('info', Path(issue['path']).name, issue['reason'], '', issue['path']))
                    if len(rows) == 1:
                        rows.append(row('scan', 'No matching issues', 'The last scan did not flag files in this view.', 'Rescan'))
                rows.append(row('scan', 'Scan library again', str(self.root)))
        elif self.tab == 1:
            self._subtitle = 'Choose which systems to include in local and cloud batches.'
            for key, name in scanner.SYSTEM_LABELS.items():
                extensions = sorted(scanner.FORMATS[key])
                inputs = ' / '.join(ext[1:].upper() for ext in extensions)
                output = _output_label(key, extensions[0], self.psp_format)
                detail = f'{inputs} → {output}'
                if self.pi and key == 'ps2': detail += '  ·  Not recommended on Pi 4'
                if key == 'psp' and self.psp_format == 'chd' and key not in self.recommended:
                    detail += '  ·  Needs PPSSPP 1.17+; choose CSO in Settings for older versions.'
                if key in ('xbox', '3ds'):
                    detail += '  ·  Original always kept'
                rows.append(row('system', name, detail, 'On' if key in self.enabled_systems else 'Off', key))
        elif self.tab == 2:
            self._subtitle = 'PlayStation, Sega CD, Saturn and Dreamcast CHD sets can share a playlist.'
            rows = [row('playlist', Path(p).name, str(p), 'Inspect', p) for p in snap['playlists']]
            if not rows:
                rows.append(row('scan', 'No playlists in this library', 'Convert a complete numbered CHD set for one of the supported systems.', 'Scan'))
        elif self.tab == 3:
            self._summary = f'{len(self.enabled_systems)} systems enabled'
            self._subtitle = 'Ingest all eligible games in enabled systems. Remote originals stay in place.'
            primary = 'Start ingest' if self.ingest and self.backend else 'Choose source' if self.backend else 'Choose library'
            rows = [row('mount', 'Mounted folder', self.ingest or 'Browse a mounted NAS, SMB, NFS, or external drive.', 'Browse'),
                    row('remotes', 'Configured rclone remotes', 'Lists the remotes already configured on this device.', 'Refresh')]
            if self.remote_names == []:
                rows.append(row('remotes', 'No remotes found', 'Use a mounted folder or configure rclone, then refresh.'))
            for name in self.remote_names or []:
                rows.append(row('remote', name, 'Ingest from this remote', 'Selected' if self.ingest == name else '', name))
            if self.ingest: rows.insert(0, row('runcloud', 'Start cloud ingest', self.ingest, 'Review'))
        else:
            self._subtitle = 'Restore originals or reclaim the space held by verified backups.'
            primary = 'Purge backups'
            rows = [row('undo', 'Undo last batch', 'Restore quarantined originals and reverse the most recent batch.'),
                    row('purge', 'Purge verified backups', 'Test your games first. Removing backups cannot be undone.', 'Review')]
            for record in self.history:
                rows.append(row('history', f"Batch {record['id']}", f"{_when(record['started'])}  ·  {record['operations']} operations", record['status'], record['id']))
        if self._queue_result and self.tab == (3 if self._queue_cloud else 0):
            rows.insert(0, row('lastqueue', 'Review last queue', self._queue_result, 'Open'))
        self._rows, self._primary = rows, primary
        self.changed.emit()
        self.sceneChanged.emit(reset)

    def _show(self, kind, title, body, rows, *, reset=True):
        self._modal = {'kind': kind, 'title': str(title), 'body': str(body)}
        self._dialog_rows = rows
        self.changed.emit()
        self.sceneChanged.emit(reset)

    def _hide(self):
        self._modal = None
        self._dialog_rows = []
        self._confirm = None
        self.changed.emit()
        self.sceneChanged.emit(False)

    def _notice(self, title, message, lines=(), *, alert=False):
        self._show('notice', title, message, [row('back', 'Return')] + [row('note', line) for line in lines])
        if alert: self.cue('alert')

    def _ask(self, title, message, label, callback):
        self._confirm = callback
        self._show('confirm', title, message, [row('back', 'Go back', 'No changes will be made.'), row('accept', label)])

    @Slot(int)
    def switchTab(self, index):
        if self._modal or self._busy or self._queue_visible: return
        self.tab = index % len(TITLES)
        self._draw(True)
        if self.tab == 3 and self.remote_names is None: self._remotes()

    def _remotes(self):
        def loaded(names):
            self.remote_names = names
            self._report(f'{len(names)} configured remotes found.')
            self._draw(False)
        self._work('Looking for remotes', cloud_ingest.remotes, loaded)

    @Slot()
    def settings(self):
        if self._busy or self._queue_visible: return
        if self._modal:
            if self._modal['kind'] == 'settings': self._hide()
            return
        self._settings()

    def _settings(self, reset=True):
        self._show('settings', 'Settings', 'Preferences apply to the next batch.', [
            row('library', 'ROM library', self.location, 'Change'), row('presets', 'Thermal profile', PRESETS[self.preset], 'Choose'),
            row('psp_formats', 'PSP output format', 'For PSP ISO files only.', self.psp_format.upper()),
            row('policy', 'After verification', POLICIES[self.policy][1], POLICIES[self.policy][0]),
            row('sound', 'Interface sounds', 'Quiet navigation ticks and batch feedback.', 'On' if self.sound else 'Off'),
            row('rumble', 'Controller vibration', 'Pulse on completion or when attention is needed.', 'On' if self.rumble else 'Off'),
            row('update', 'Update CouchCHD', 'Check the public release and install it for your next launch.'),
            *([row('art', 'Refresh launcher artwork', 'Update stock artwork and fill empty slots. Keep custom images.')]
              if launcher_art.managed_launcher() else []),
            row('steam', 'Add to Steam', 'Register the launcher, grid artwork, and gamepad layout.'), row('exit', 'Exit CouchCHD')], reset=reset)

    def _pick(self, target, *, returning=False):
        self._target = target
        self._directory_trail.clear()
        if not returning:
            self._return_to = 'settings' if self._modal and self._modal['kind'] == 'settings' else ''
        paths = list(dict.fromkeys([*self.candidates, *([self.root] if self.root else []), Path.home(), *(Path(d.absoluteFilePath()) for d in QDir.drives())]))
        self._show('locations', {'library': 'Choose ROM library', 'mount': 'Choose ingest source', 'extract': 'Choose extraction folder'}[target], 'Open a location, then select the folder you want to use.', [row('browse', p.name or str(p), p, 'Open', p) for p in paths])

    def _browse(self, path, *, remember=True):
        def loaded(data):
            if remember and self._modal and self._modal['kind'] == 'browse':
                self._directory_trail.append(self._dir)
            self._dir, folders = data
            rows = [row('use_dir', 'Use this folder', str(self._dir), 'Select')]
            if self._dir.parent != self._dir: rows.append(row('browse', 'Parent folder', self._dir.parent, 'Up', self._dir.parent))
            rows += [row('browse', p.name, '', 'Open', p) for p in folders]
            self._show('browse', 'Choose folder', self._dir, rows)
            self._report('Choose a directory.')
        self._work('Reading folder', lambda: _directories(Path(path)), loaded)

    def _choose_dir(self):
        path = self._dir
        if self._target == 'library': return self._library(path)
        if self._target == 'mount':
            self.ingest = str(path)
            self._hide()
            self._draw(True)
        elif self._target == 'extract':
            source = self._extract_chd
            self._ask('Extract disc image?', f'{source.name}\nDestination: {path}', 'Extract disc image', lambda: self._extract(source, path))

    def _extract(self, source, path):
        self._hide()
        def done(result):
            self._report('Extraction complete.')
            self._notice('Extraction complete', f'The extracted disc image is in {path}.')
            self.cue('done')
        self._work('Extracting disc', lambda: self.backend.extract(source, path), done)

    def _run_prompt(self, cloud=False):
        if self.backend is None: return self._pick('library')
        if not self.enabled_systems: return self._notice('No systems selected', 'Enable at least one system in Systems.')
        if cloud and not self.ingest: return self._pick('mount')
        sources = None if cloud else self._selected()
        if sources == set(): return self._scan()
        if cloud:
            details = f'All eligible games in enabled systems, from {self.ingest} to {self.root}. Remote originals stay untouched.'
        else:
            scope = 'All eligible games' if sources == self._eligible() else 'Selected games'
            details = f'{scope} in enabled systems: {len(sources)}.\n{self.root}\n{POLICIES[self.policy][1]}'
            if self.policy != 'keep' and any(g['system'] in ('xbox', '3ds') and g['path'] in sources for g in self.snap['games']):
                details += ' Original Xbox and 3DS images always stay in place.'
        if self.recalbox: details += '\nStop EmulationStation first. Its cached gamelist can overwrite playlist metadata.'
        self._ask('Start cloud ingest?' if cloud else 'Start verified compression?', details, 'Start ingest' if cloud else 'Start batch', lambda: self._run(cloud, sources))

    def _run(self, cloud, sources):
        self._hide()
        self._cancel = threading.Event()
        self._cancel_requested = False
        self._queue_cloud = cloud
        self._queue_result = ''
        self._queue.reset(ItemProgress(str(g['path']), Path(g['path']).name, g['system'], 'queued')
                          for g in (self.snap or {}).get('games', ()) if not cloud and str(g['path']) in sources)
        self._queue_visible = True
        self.queueChanged.emit()
        options = dict(systems=set(self.enabled_systems), sources=sources, ingest=self.ingest if cloud else None,
                       preset=self.preset, psp_format=self.psp_format, quarantine=cloud or self.policy != 'keep',
                       immediate=not cloud and self.policy == 'immediate', progress=self._progress.emit,
                       on_item=self._item.emit, on_space=self._space.emit, cancel=self._cancel)
        def finished(result):
            self.saved = int(result['saved'])
            self.prefs.setValue(self._saved_key(), str(self.saved))
            errors = list(map(str, result['errors']))
            converted = list(map(str, result['converted']))
            paused = result.get('paused')
            cancelled = self._cancel_requested
            title = 'Batch cancelled' if cancelled else 'Batch paused' if paused else 'Batch needs attention' if errors else 'Batch complete'
            self._queue_result = title
            self._queue.finish_pending('cancelled' if cancelled else 'paused' if paused else 'skipped',
                                       paused or ('Cancelled before completion.' if cancelled else 'Not processed in this batch.'))
            self.queueChanged.emit()
            text = f'{len(converted)} converted. Compression difference: {size(self.saved)}.'
            if converted and not cloud and self.policy == 'quarantine':
                text += '\nTest your games before permanently removing backups in History.'
            if paused:
                text += f'\n{paused}'
                if not cancelled: text += '\nResolve the condition, then start another batch to continue.'
            if errors: text += f'\n{len(errors)} errors. See the messages below.'
            def refreshed(warning):
                result_text = self._with_free_space(text, result.get('free_before'))
                self._report(result_text)
                lines = errors + list(result.get('messages', ()))
                if warning: lines.append(warning)
                self._notice(title, result_text, lines)
                self.cue('alert' if errors or paused or warning else 'done')
            self._refresh_after_commit(refreshed)
        self._work('Compressing discs', lambda: self.backend.run(**options), finished)

    def _with_free_space(self, message, before):
        if self.snap is None or before is None:
            return message
        after = self.snap['free']
        delta = after - before
        direction = 'more' if delta >= 0 else 'less'
        return f'{message}\nLibrary free now: {size(after)} ({size(abs(delta))} {direction} than before).'

    def _management(self, key):
        if self.backend is None: return self._pick('library')
        undo = key == 'undo'
        text = ('Restore the most recent batch from its backup and reverse its playlist changes.' if undo else
                'Test your converted games first. Removing verified backups frees the space they use, '
                'but you cannot Undo after permanent removal.')
        if undo and self.recalbox: text += ' Stop EmulationStation first to protect gamelist changes.'
        def execute():
            self._hide()
            def change():
                before = hardware.free_bytes(self.root)
                return (self.backend.undo() if undo else self.backend.purge()), before
            def done(result):
                count, before = result
                msg = f'{count} operations reversed.' if undo else f'{count} backups purged.'
                def refreshed(warning):
                    result_text = self._with_free_space(msg, before)
                    self._report(result_text)
                    self._notice('Undo complete' if undo else 'Purge complete', result_text, [warning] if warning else [])
                self._refresh_after_commit(refreshed)
            self._work('Restoring originals' if undo else 'Purging verified backups', change, done)
        self._ask('Undo last batch?' if undo else 'Purge verified backups?', text, 'Restore originals' if undo else 'Permanently purge backups', execute)

    @Slot()
    def primary(self):
        if self._modal or self._busy or self._queue_visible: return
        if self.tab == 4: self._management('purge')
        elif self.tab == 3: self._run_prompt(True)
        elif self._selected(): self._run_prompt()
        else: self._scan()

    @Slot()
    def back(self):
        kind = self._modal['kind'] if self._modal else ''
        if kind == 'item': return self._hide()
        if self._busy == 'Compressing discs' and kind != 'confirm':
            def cancel():
                self._cancel_requested = True
                self._cancel.set()
                self.operationChanged.emit()
                self._hide()
                self._report('Cancelling safely. Waiting for active operations…')
            if not self._cancel.is_set(): self._ask('Cancel this batch?', 'Completed conversions remain in History. Incomplete discs will not be committed.', 'Cancel batch', cancel)
            return
        if kind == 'confirm': return self._hide()
        if self._busy: return
        if kind in ('presets', 'policies', 'psp_formats'): return self._settings()
        if kind == 'browse':
            if self._directory_trail: return self._browse(self._directory_trail.pop(), remember=False)
            return self._pick(self._target, returning=True)
        if kind == 'locations' and self._return_to == 'settings': return self._settings()
        if kind: return self._hide()
        if self._queue_visible:
            self._queue_visible = False
            self.queueChanged.emit()
            self._draw(True)
            return
        if self.tab != 0: return self.switchTab(0)
        self.exitPrompt()

    @Slot()
    def exitPrompt(self):
        if self._busy:
            if self._busy == 'Compressing discs': self.back()
            else: self._report(f'{self._busy}. Wait for this operation before exiting.')
            return
        self._ask('Exit CouchCHD?', 'Your library and saved preferences stay on this device.', 'Exit CouchCHD', self.quitRequested.emit)

    @Slot(int, bool)
    def activate(self, index, inspect=False):
        if self._queue_visible and not self._modal: return
        items = self._dialog_rows if self._modal else self._rows
        if not 0 <= index < len(items): return
        item = items[index]
        key, arg = item['key'], item['arg']
        if self._busy and not (self._modal and ((self._busy == 'Compressing discs' and
                               self._modal['kind'] == 'confirm' and key in ('accept', 'back'))
                               or (self._modal['kind'] == 'item' and key == 'back'))): return
        if key == 'accept':
            action = self._confirm
            if action is not None:
                self._confirm = None
                action()
            return
        if key == 'back': return self.back()
        if key == 'note': return
        if key == 'lastqueue':
            self._queue_visible = True
            self.queueChanged.emit()
            return
        if key == 'bulk' and not inspect:
            eligible = self._eligible()
            if eligible.isdisjoint(self.omitted):
                self.omitted.update(eligible)
            else:
                self.omitted.difference_update(eligible)
            return self._draw(False)
        if key == 'game':
            game = next(g for g in self.snap['games'] if str(g['path']) == arg)
            if inspect:
                return self._notice(Path(arg).name, f"{game['system']}  ·  {size(game['size'])}\n{arg}", [str(t) for t in game['tracks']])
            if self._completed(game):
                return self._notice('Already verified', 'This output already exists. Choose Output files in the library view to inspect it.')
            if game['system'] not in self.enabled_systems:
                return self._notice('System disabled', 'Enable this system on the Systems tab before selecting this game.')
            if arg in self.omitted: self.omitted.remove(arg)
            else: self.omitted.add(arg)
            return self._draw(False)
        if key == 'chd':
            self._extract_chd = Path(arg)
            return self._show('chd', Path(arg).name, f"{item['detail']}\n{arg}", [row('extract', 'Extract CD to BIN/CUE', 'For CD or GD-ROM CHDs only.'), row('back', 'Return')])
        if key == 'extract': return self._pick('extract')
        if key == 'playlist':
            def playlist_lines():
                with Path(arg).open(encoding='utf-8-sig', errors='replace') as stream:
                    return [line.rstrip() for line in stream if line.strip()]
            return self._work('Reading playlist', playlist_lines, lambda lines: self._notice(Path(arg).name, arg, lines))
        if key in ('info', 'history') or (inspect and not self._modal):
            return self._notice(item['label'], '\n'.join(x for x in (item['detail'], item['value'], arg) if x))
        if key == 'library' or key == 'mount': return self._pick(key)
        if key == 'browse': return self._browse(arg)
        if key == 'use_dir': return self._choose_dir()
        if key == 'scan': return self._scan()
        if key == 'filter':
            choices = ['Queue', 'Output files', 'Needs Unpacking', 'Other scan issues']
            return self._show('filter', 'Library views', 'Review eligible sources, existing images, or scanner findings.', [row('view', v, '', 'Current' if self.filter == v else '', v) for v in choices])
        if key == 'view':
            self.filter = arg
            self._hide()
            return self._draw(True)
        if key == 'system':
            self.enabled_systems.symmetric_difference_update([arg])
            if arg == 'psp':
                self.psp_manually_toggled = True
            return self._draw(False)
        if key == 'psp_formats':
            return self._show('psp_formats', 'PSP output format', 'Choose how PSP ISO files will be saved. Existing outputs are not changed.', [
                row('psp_format', label, '', 'Current' if self.psp_format == value else '', value)
                for value, label in PSP_FORMATS.items()])
        if key == 'psp_format':
            if self.psp_format != arg and 'psp' not in self.recommended and not self.psp_manually_toggled:
                if arg == 'cso':
                    self.enabled_systems.add('psp')
                else:
                    self.enabled_systems.discard('psp')
            self.psp_format = arg
            self.prefs.setValue('pspFormat', arg)
            self._draw(False)
            return self._settings()
        if key == 'presets': return self._show('presets', 'Thermal profile', 'Deck and Pi keep a device CPU limit, even when docked. Silent requests fewer workers.', [row('preset', label, '', 'Current' if self.preset == p else '', p) for p, label in PRESETS.items()])
        if key == 'preset':
            self.preset = arg
            self.prefs.setValue('preset', arg)
            return self._settings()
        if key == 'policy':
            return self._show('policies', 'After verification', 'This applies only to eligible local inputs. Original Xbox, 3DS and cloud sources always stay in place.', [
                row('set_policy', label, detail, 'Current' if self.policy == p else '', p)
                for p, (label, detail) in POLICIES.items()])
        if key == 'set_policy':
            if arg != 'immediate': return self._policy(arg)
            return self._ask('Delete verified originals?', 'Immediate cleanup bypasses quarantine and cannot be undone. Each future local batch will use this policy until you change it.', 'Enable immediate deletion', lambda: self._policy(arg))
        if key in ('sound', 'rumble'):
            setattr(self, key, not getattr(self, key))
            self.prefs.setValue(key, getattr(self, key))
            self.cue('tick' if key == 'sound' else 'done')
            return self._settings(False)
        if key == 'update':
            self._hide()
            def updated(result):
                title, message = result
                self._report(title)
                self._notice(title, message)
            return self._work('Updating CouchCHD', lambda: updater.update(self._progress.emit), updated)
        if key == 'art' and (launcher := launcher_art.managed_launcher()):
            self._hide()
            return self._work('Checking launcher artwork', lambda: launcher_art.refresh(launcher),
                              lambda msg: self._notice('Launcher artwork', msg))
        if key == 'steam':
            self._hide()
            return self._work('Registering with Steam', steam.register, lambda msg: self._notice('Steam shortcut ready', msg))
        if key == 'exit': return self.exitPrompt()
        if key == 'remotes': return self._remotes()
        if key == 'remote':
            self.ingest = arg
            return self._draw(False)
        if key == 'runcloud': return self._run_prompt(True)
        if key in ('undo', 'purge'): return self._management(key)

    def _policy(self, value):
        self.policy = value
        self.prefs.setValue('cleanupPolicy', value)
        self._settings()

    def close(self):
        self._cancel.set()
        self._pool.shutdown(wait=True, cancel_futures=True)
        self.prefs.sync()
