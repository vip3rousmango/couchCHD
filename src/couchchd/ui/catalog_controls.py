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
        # Only read our local index here unless the user selected Refresh. An
        # unavailable NAS must never hold up opening the saved library list.
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
        rows = [_action('systems', 'Find compressed games', 'See which saved libraries have each game.',
                        str(len(self.files))),
                _action('add', 'Add a library', 'Register a home folder or a device you own.')]
        if self.sources:
            rows.append(_action('refresh_all', 'Refresh all libraries', 'Check only the sources listed below.'))
        rows.extend(_action('source', s.name, s.location, _status(s), s.id) for s in self.sources.values())
        self.ui._show('catalog_home', 'Home & device libraries',
                      'Browse the saved list offline. Refresh to check a source, then choose what to copy.'
                      if self.sources else 'Add your first source. Keep originals on your NAS, NTFS disk, or other device.', rows)

    def _source(self):
        source = self.sources.get(self.selected)
        if source is None:
            return self._home()
        body = f'{source.location}\n{_status(source)}'
        if source.status == 'offline' and source.error:
            body += f'\n{source.error}'
        self.ui._show('catalog_source', source.name, body, [
            _action('refresh_source', 'Refresh this library', 'Read its current game list without changing any files.'),
            _action('systems', 'Browse saved games', 'Includes the last successful listing while offline.', arg=source.id),
            _action('forget', 'Forget this library', 'Remove this source from the catalog on this device only.')])

    def _add(self):
        self.ui._show('catalog_add', 'Add a library', 'Choose a source for already compressed games. Originals stay in place.', [
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
                 _action('folder', 'Use the remote root', 'Only if the system folders are directly inside this remote.')]
        self.ui._show('catalog_folders', 'Choose the ROM folder',
                      f'{self.remote}\nChoose the folder containing psx, dreamcast, or psp. A leading / is an absolute remote path.', rows)

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
        self.ui._show('catalog_register', 'Save this library?', f'{name}\n{self.location}\nSaving registers the source here. Refresh it when you want to list its games.', [
            _action('save', 'Save library'), row('back', 'Change name')])

    def _saved(self):
        self.selected = next((s.id for s in self.sources.values() if s.name == self.label), '')
        self.ui._report('Library saved. Refresh it to see its games.')
        self._source()

    def _by_scope(self, entries):
        return not self.scope or any(e.source_id == self.scope for e in entries)

    def _systems(self):
        counts = Counter(path.partition('/')[0] for path, entries in self.files.items() if self._by_scope(entries))
        source = self.sources.get(self.scope)
        title = source.name if source else 'All libraries'
        rows = [_action('system', scanner.SYSTEM_LABELS.get(system, system), '', str(count), system)
                for system, count in sorted(counts.items())]
        if rows:
            body = 'Choose a system to find a game and its locations.'
        elif source:
            body = 'No compressed games listed. Refresh this source and check that it points at the folder containing your systems.'
        else:
            body = 'No games listed. Add a library and refresh it to find compressed games.'
        self.ui._show('catalog_systems', title, body, rows or [row('back', 'Back to libraries' if not source else 'Back to source')])

    def _games(self):
        prefix = self.system + '/'
        rows = [_action('locations', Path(relative).name, relative, f'{len(entries)} location' + ('s' if len(entries) != 1 else ''), relative)
                for relative, entries in self.files.items() if relative.startswith(prefix) and self._by_scope(entries)]
        self.ui._show('catalog_games', scanner.SYSTEM_LABELS.get(self.system, self.system),
                      'Choose a game to compare its saved locations.', rows or [row('back', 'No games left in this listing')])

    def _locations(self):
        rows = []
        for entry in self.files.get(self.relative, ()):
            source = self.sources[entry.source_id]
            rows.append(_action('preview', source.name, f'{source.location}\n{_status(source)}', size(entry.size), source.id))
        self.ui._show('catalog_locations', 'Game locations', self.relative, rows or [row('back', 'Not found at last refresh')])

    def _preview(self):
        entry = next((e for e in self.files.get(self.relative, ()) if e.source_id == self.selected), None)
        if entry is None:
            return self._locations()
        source = self.sources[self.selected]
        body = f'From: {source.name} · {source.location}\nGame: {self.relative}\nLast listed size: {size(entry.size)}'
        rows = [row('back', 'Go back', 'Nothing will be copied.')]
        if source.status != 'online':
            body += f'\n{_status(source)}. Reconnect the source and refresh it before pulling.'
            rows.append(_action('recheck', 'Refresh this location'))
        elif not self.ui.backend:
            body += '\nChoose a writable ROM library in Settings before pulling.'
        else:
            try:
                target, = destination_paths(self.ui.root, (self.relative,))
            except ValueError as error:
                body += f'\nDestination unavailable: {error}\nChoose a compatible ROM library in Settings.'
            else:
                body += (f'\nTo: {target}\nCopies and verifies this game, including playlist discs, without recompression. '
                         'Identical files can be reused; different files are never replaced.')
                rows.append(_action('pull', 'Pull this game'))
        self.ui._show('catalog_preview', 'Review pull', body, rows)

    def _pull(self):
        source_id, relative, destination = self.selected, self.relative, self.ui.root
        self.ui._show('catalog_work', 'Pulling game', f'{relative}\nTo: {destination}', [])
        last_report = 0.0

        def progress(done, total):
            nonlocal last_report
            now = monotonic()
            if now - last_report >= 0.25 or done == total:
                self.ui._progress.emit(f'Pulling {Path(relative).name}: {size(done)} / {size(total)}')
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
                self._snapshot('Saving library', self._saved, lambda c: c.add(label, location))
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
