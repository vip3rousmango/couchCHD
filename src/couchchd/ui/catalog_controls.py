"""Controller-first views for explicitly registered ROM libraries and pull-only copies."""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from time import monotonic

from .. import cloud_ingest, scanner
from ..catalog import Catalog
from ..catalog_transport import destination_paths
from .presentation import _when, row, size


# Common paths and names need only a single selection. For unusual ones the
# character picker works on a console with no physical or on-screen keyboard.
_ROM_FOLDERS = ('roms', 'Emulation/roms', '/recalbox/share/roms')
_CHAR_GROUPS = ('abcdef', 'ghijkl', 'mnopqr', 'stuvwxyz', 'ABCDEF', 'GHIJKL',
                'MNOPQR', 'STUVWXYZ', '0123456789', ' -_./', "()[]&'+,", '!#$%@^{}=;~`')


def _status(source):
    if source.status == 'new':
        return 'Not refreshed'
    if source.status == 'offline':
        return f'Offline · last seen {_when(source.last_seen)}' if source.last_seen else 'Offline · never seen'
    return f'Online at last refresh · {_when(source.last_seen)}'


def _action(key, label, detail='', value='', arg=''):
    return row('catalog_' + key, label, detail, value, arg)


def _count(n, singular, plural=None):
    return f'{n} {singular if n == 1 else plural or singular + "s"}'


def _scan_detail(source, ready, originals):
    if source.last_seen is None:
        return ('Reconnect this source and refresh it.' if source.status == 'offline' else
                'Refresh this library to scan its folders.')
    copies = f'{ready} ready to copy' if source.status == 'online' else _count(ready, 'cached copy', 'cached copies')
    parts = [f'{copies} · {_count(originals, "original")}']
    if source.directories is None:
        parts.append('Older saved list. Refresh to find originals and scan details.')
    else:
        parts.append(f'{_count(source.directories, "folder")} scanned.')
        if source.unplaced:
            parts.append(f'{_count(source.unplaced, "file")} without a clear system. '
                         'Use one platform folder such as psx, psp or dreamcast per game path.')
        if source.unsupported:
            parts.append(f'{_count(source.unsupported, "file")} in an unsupported format for its platform. '
                         'For archived discs, use a single-disc ZIP or unpack a working copy.')
        if source.invalid_playlists:
            parts.append(f'{_count(source.invalid_playlists, "unusable playlist")}. Check the disc filenames and paths.')
        if not (ready or originals or source.unplaced or source.unsupported or source.invalid_playlists):
            parts.append('No eligible games in the platform folders. BIN or RAW tracks need their CUE or GDI sheet.'
                         if source.platform_folders else
                         'No supported platform folder found. Choose a platform folder or its parent.')
    return '\n'.join(parts)


def _full_path(source, relative):
    location = source.location
    base = location if Path(location).is_absolute() else location.partition(':')[2]
    key = Path(relative)
    # Direct platform roots use a virtual leading system component in the CLI key.
    if Path(base).name.casefold() in scanner.SYSTEMS:
        key = key.relative_to(Path(base).name)
    return location + ('' if location.endswith(('/', ':')) else '/') + key.as_posix()


class CatalogControls:
    def __init__(self, bridge):
        self.ui = bridge
        self.sources = {}
        self.files = {}
        self.scope = ''  # source ID, or all registered sources
        self.system = self.relative = self.selected = ''
        self.remote = self.location = self.label = ''
        self.field = self.text = ''

    def _snapshot(self, title, after, change=None):
        # Ordinary navigation reads only the local index; source scans run only on save or refresh.
        def read():
            catalog = Catalog()
            if change is not None:
                change(catalog)
            sources = {s.id: s for s in catalog.sources()}
            files = defaultdict(list)
            for entry in catalog.entries():
                if entry.source_id in sources:
                    files[entry.relative].append(entry)
            return sources, files

        def loaded(snapshot):
            self.sources, self.files = snapshot
            after()

        self.ui._work(title, read, loaded)

    def _home(self):
        rows = [_action('systems', 'Browse games & originals', 'Find a compressed copy or review a source for conversion.',
                        str(len(self.files))),
                _action('add', 'Add a library', 'Register a home folder or a device you own.')]
        if self.sources:
            rows.append(_action('refresh_all', 'Refresh all libraries', 'Check only the sources listed below.'))
        rows.extend(_action('source', s.name, s.location, _status(s), s.id) for s in self.sources.values())
        self.ui._show('catalog_home', 'Home & device libraries',
                      'Browse the last scan offline. Refresh a library when its contents change.'
                      if self.sources else 'Add your first source. Keep originals on your NAS, NTFS disk, or other device.', rows)

    def _source(self):
        source = self.sources.get(self.selected)
        if source is None:
            return self._home()
        counts = Counter(e.kind for entries in self.files.values() for e in entries if e.source_id == source.id)
        body = f'{source.location}\n{_status(source)}\n{_scan_detail(source, counts["output"], counts["original"])}'
        if source.status == 'offline' and source.error:
            body += f'\n{source.error}'
        self.ui._show('catalog_source', source.name, body, [
            _action('systems', 'Browse games & originals', 'View the last successful scan by platform.', arg=source.id),
            _action('refresh_source', 'Refresh this library', 'Scan its folders again. Source files stay untouched.'),
            _action('forget', 'Forget this library', 'Remove this source from the catalog on this device only.')])

    def _add(self):
        self.ui._show('catalog_add', 'Add a library', 'Choose a platform folder or a parent containing your collection. CouchCHD scans inside it without changing files.', [
            _action('mount', 'Mounted ROM folder', 'Browse a disk, a read-only NTFS volume, or a mounted NAS / SSHFS share.'),
            _action('remotes', 'Configured rclone remote', 'Choose a remote already set up on this device, including SFTP.')])

    def _remotes(self, names):
        self.ui._show('catalog_remotes', 'Choose a remote',
                      'Choose the connection, then its ROM folder. No passwords are needed here.' if names else
                      'No configured remotes found. Configure rclone outside CouchCHD or use a mounted folder.',
                      [_action('remote', name, arg=name) for name in names] or [_action('add', 'Choose another source')])

    def _folders(self):
        rows = [_action('folder', folder, arg=folder) for folder in _ROM_FOLDERS]
        rows += [_action('enter_folder', 'Enter another ROM path'),
                 _action('folder', 'Use the remote root', 'Scan platform folders anywhere under this remote.')]
        self.ui._show('catalog_folders', 'Choose the ROM folder',
                      f'{self.remote}\nChoose a platform folder or a parent to scan. A leading / is an absolute remote path.', rows)

    def chosen_directory(self, path):
        self.remote = ''
        self.location = str(path)
        self._names()

    def _names(self, message=''):
        suggestion = self.remote[:-1] if self.remote else Path(self.location).name
        choices = dict.fromkeys([suggestion, 'Home', 'Bazzite', 'Steam Deck', 'Recalbox', 'NAS'])
        taken = {s.name.casefold() for s in self.sources.values()}
        rows = [_action('name', name, arg=name) for name in choices if name and name.casefold() not in taken]
        rows.append(_action('enter_name', 'Enter another name'))
        self.ui._show('catalog_names', 'Name this library', message or f'{self.location}\nChoose a name you’ll recognize on this device.', rows)

    def _editor(self, field=None, error=''):
        if field:
            self.field, self.text = field, ''
        name = self.field == 'name'
        rows = [_action('text_done', 'Use this name' if name else 'Use this ROM path')]
        if self.text:
            rows.append(_action('erase', 'Remove last character'))
        rows.extend(_action('chars', 'Space and separators' if group.startswith(' ') else ' '.join(group), arg=group)
                    for group in _CHAR_GROUPS)
        self.ui._show('catalog_editor', 'Enter a library name' if name else 'Enter a ROM path',
                      '\n'.join(filter(None, (error, self.text or 'Choose a character group below.'))), rows)

    def _register(self, name):
        self.label = name
        self.ui._show('catalog_register', 'Save and scan this library?', f'{name}\n{self.location}\nFind compressed games and originals in nested platform folders. Nothing is copied.', [
            _action('save', 'Save & scan library'), row('back', 'Change name')])

    def _saved(self):
        self.selected = next((s.id for s in self.sources.values() if s.name == self.label), '')
        source = self.sources.get(self.selected)
        self.ui._report('Library saved and scanned.' if source and source.status == 'online' else
                        'Library saved. Reconnect it and refresh to scan.')
        self._source()

    def _scoped(self):
        for path, entries in self.files.items():
            group = [e for e in entries if e.source_id == self.scope] if self.scope else entries
            if group:
                yield path, group

    def _systems(self):
        counts = Counter(entries[0].system for _, entries in self._scoped())
        source = self.sources.get(self.scope)
        title = source.name if source else 'All libraries'
        rows = [_action('system', scanner.SYSTEM_LABELS.get(system, system), '', str(count), system)
                for system, count in sorted(counts.items())]
        if rows:
            body = 'Choose a platform. Compressed copies and originals are listed separately by file.'
        elif source:
            body = _scan_detail(source, 0, 0)
        else:
            body = ('Open a saved library for its scan details, or refresh it when reconnected.' if self.sources else
                    'Add a library to scan for games and originals.')
        self.ui._show('catalog_systems', title, body, rows or [row('back', 'Back to libraries' if not source else 'Back to source')])

    def _games(self):
        rows = []
        for relative, entries in self._scoped():
            if entries[0].system != self.system:
                continue
            if entries[0].kind == 'original':
                label = 'Needs conversion'
            else:
                label = 'Ready to copy' if any(e.status == 'online' for e in entries) else 'Cached copy'
            rows.append(_action('locations', Path(relative).name, relative, label, relative))
        self.ui._show('catalog_games', scanner.SYSTEM_LABELS[self.system],
                      'Choose a file to see its location and next step.', rows or [row('back', 'No games left in this listing')])

    def _locations(self):
        rows = []
        for entry in self.files.get(self.relative, ()):
            if self.scope and entry.source_id != self.scope:
                continue
            source = self.sources[entry.source_id]
            rows.append(_action('preview', source.name, f'{_full_path(source, self.relative)}\n{_status(source)}', size(entry.size), source.id))
        self.ui._show('catalog_locations', 'Game locations', self.relative, rows or [row('back', 'Not found at last refresh')])

    def _preview(self):
        entry = next((e for e in self.files.get(self.relative, ()) if e.source_id == self.selected), None)
        if entry is None:
            return self._locations()
        source = self.sources[self.selected]
        original = entry.kind == 'original'
        body = f'From: {source.name}\n{_full_path(source, self.relative)}\nListed size: {size(entry.size)}'
        if original:
            body += '\nOriginal detected by name, not a compressed copy. Ingest validates inputs before conversion.'
        rows = [row('back', 'Go back')]
        if source.status != 'online':
            body += f'\n{_status(source)}. Reconnect this source and refresh it.'
            rows.append(_action('recheck', 'Refresh this location'))
        elif original:
            if not Path(source.location).is_absolute() and Path(self.relative).suffix.lower() == '.zip':
                body += '\nDisc ZIP ingest needs a mounted source. Choose its mounted folder under Libraries & devices.'
                rows.append(_action('mount_ingest', 'Choose mounted ingest folder'))
            else:
                body += ('\nSelect this library under Libraries & devices. Review enabled systems and a separate destination '
                         'before starting; ingest processes the library, not just this file.')
                rows.append(_action('ingest', 'Use library as ingest source', 'Selects the source only. No conversion starts.'))
        elif not self.ui.backend:
            body += '\nChoose a writable ROM library in Settings before pulling.'
        else:
            destination, relative = self.ui.root, self.relative

            def resolve():
                try:
                    target, = destination_paths(destination, (relative,))
                    return target, ''
                except (OSError, ValueError) as exc:
                    return None, str(exc)

            def show(result):
                target, error = result
                if error:
                    detail = f'Destination unavailable: {error}\nChoose a compatible ROM library in Settings.'
                else:
                    detail = f'To: {target}\nCopies and checks every file. Different files are never replaced.'
                    rows.append(_action('pull', 'Pull this game'))
                self.ui._show('catalog_preview', 'Review pull', body + '\n' + detail, rows)

            return self.ui._work('Checking destination', resolve, show)
        self.ui._show('catalog_preview', 'Needs conversion' if original else 'Review pull', body, rows)

    def _pull(self):
        source_id, relative, destination = self.selected, self.relative, self.ui.root
        self.ui._show('catalog_work', 'Pulling game', f'{relative}\nTo: {destination}', [])
        last_report = started = None

        def progress(done, total):
            nonlocal last_report, started
            now = monotonic()
            if started is None:
                started = now
            if last_report is None or now - last_report >= 0.25 or done == total:
                elapsed = now - started
                rate = f' · {size(done / elapsed)}/s read' if done and elapsed >= 0.25 else ''
                self.ui._progress.emit(f'Pulling {Path(relative).name}: {size(done)} / {size(total)}{rate}')
                last_report = now

        def finished(paths):
            def refreshed(warning):
                body = f'{len(paths)} verified file' + ('s' if len(paths) != 1 else '') + f' available in {destination}.'
                self.ui._report(body)
                if warning:
                    body += '\n' + warning
                self.ui._show('catalog_result', 'Pull complete', body,
                              [row('back', 'Return to game locations')] + [row('note', str(p)) for p in paths])
                self.ui.cue('alert' if warning else 'done')
            self.ui._refresh_after_commit(refreshed)

        self.ui._work('Pulling game', lambda: Catalog().pull(source_id, relative, destination, progress=progress), finished)

    def activate(self, key, arg):
        match key:
            case 'catalog_open':
                self._snapshot('Reading saved libraries', self._home)
            case 'catalog_source':
                self.selected = arg
                self._source()
            case 'catalog_refresh_all' | 'catalog_refresh_source' | 'catalog_recheck':
                target = None if key == 'catalog_refresh_all' else self.selected
                after = {'catalog_refresh_all': self._home, 'catalog_refresh_source': self._source,
                         'catalog_recheck': self._locations}[key]
                def finished():
                    self.ui._report('Refresh complete. Offline sources retain their last successful listing.')
                    after()
                self._snapshot('Refreshing libraries', finished, lambda c: c.refresh(target))
            case 'catalog_forget':
                source = self.sources[self.selected]
                self.ui._show('catalog_forget', f'Forget {source.name}?',
                              'Remove this saved location from this device. No game files will be deleted.',
                              [row('back', 'Keep library'), _action('remove', 'Forget library')])
            case 'catalog_remove':
                target = self.selected
                self._snapshot('Forgetting library', self._home, lambda c: c.remove(target))
            case 'catalog_add':
                self._add()
            case 'catalog_mount':
                self.ui._pick('catalog-mounted')
            case 'catalog_remotes':
                self.ui._work('Reading configured remotes', cloud_ingest.remotes, self._remotes)
            case 'catalog_remote':
                self.remote = arg
                self._folders()
            case 'catalog_folder':
                self.location = self.remote + arg
                self._names()
            case 'catalog_name':
                self._register(arg)
            case 'catalog_enter_folder' | 'catalog_enter_name':
                self._editor('name' if key == 'catalog_enter_name' else 'folder')
            case 'catalog_chars':
                self.ui._show('catalog_chars', 'Choose a character', self.text or 'Choose the first character.',
                              [_action('char', 'Space' if ch == ' ' else ch, arg=ch) for ch in arg])
            case 'catalog_char' | 'catalog_erase':
                self.text = self.text + arg if key == 'catalog_char' else self.text[:-1]
                self._editor()
            case 'catalog_text_done':
                value = self.text.strip() if self.field == 'name' else self.text
                if not value:
                    self._editor(error='Enter at least one character.')
                elif self.field == 'name':
                    if len(value) > 128 or any(value.casefold() == s.name.casefold() for s in self.sources.values()):
                        self._editor(error='Choose a unique name of 128 characters or fewer.')
                    else:
                        self._register(value)
                else:
                    self.location = self.remote + value
                    self._names()
            case 'catalog_save':
                label, location = self.label, self.location
                self._snapshot('Saving and scanning library', self._saved, lambda c: c.refresh(c.add(label, location).id))
            case 'catalog_systems':
                self.scope = arg
                self._systems()
            case 'catalog_system':
                self.system = arg
                self._games()
            case 'catalog_locations':
                self.relative = arg
                self._locations()
            case 'catalog_preview':
                self.selected = arg
                self._preview()
            case 'catalog_ingest':
                self.ui.ingest = self.sources[self.selected].location
                self.ui._hide()
                self.ui.switchTab(3)
                self.ui._report('Ingest source selected. Review the systems and working library before starting.')
            case 'catalog_mount_ingest':
                self.ui._hide()
                self.ui.switchTab(3)
                self.ui._pick('mount')
            case 'catalog_pull':
                self._pull()

    def back(self, kind):
        if kind == 'locations' and self.ui._target == 'catalog-mounted':
            self._add()
            return True
        match kind:
            case 'catalog_home': self.ui._hide()
            case 'catalog_source' | 'catalog_add': self._home()
            case 'catalog_remotes': self._add()
            case 'catalog_folders': self.activate('catalog_remotes', '')
            case 'catalog_names':
                if self.remote:
                    self._folders()
                else:
                    self.ui._pick('catalog-mounted')
            case 'catalog_editor': self._names() if self.field == 'name' else self._folders()
            case 'catalog_chars': self._editor()
            case 'catalog_register': self._names()
            case 'catalog_forget': self._source()
            case 'catalog_systems':
                if self.scope:
                    self.selected = self.scope
                    self._source()
                else:
                    self._home()
            case 'catalog_games': self._systems()
            case 'catalog_locations': self._games()
            case 'catalog_preview' | 'catalog_result': self._locations()
            case _: return False
        return True
