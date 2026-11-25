"""Controller screens for planning, checking, reviewing and copying a library."""
import json
import os
from pathlib import Path

from .. import handheld, handheld_profiles, hardware, scanner
from ..app import Backend
from .presentation import row, size


GIB = 1024**3


class LibraryTools:
    def __init__(self, bridge):
        self.ui = bridge
        self.goal = 10 * GIB
        self.systems = set()
        self.plan = None
        self.origin = None
        self.catalog = []
        self.selected = set()
        self.filter = ''
        self.reviews = []
        self.reclaim = None
        self.profiles = {p['id']: p for p in handheld_profiles.profiles()}
        saved = bridge.prefs.value('handheld/profile', '', type=str)
        self.profile_missing = bool(saved and saved not in self.profiles)
        self.profile = self.profiles.get(saved, self.profiles[handheld_profiles.DEFAULT_PROFILE])
        self._load_profile_settings()
        self.repeat_note = ''

    def _pref(self, name):
        return f"handheld/{self.profile['id']}/{name}"

    def _saved(self, name):
        try:
            return json.loads(self.ui.prefs.value(self._pref(name), '', type=str))
        except (TypeError, ValueError):
            return None

    def _load_profile_settings(self):
        options = self.profile['players']
        saved = self._saved('players')
        if self.ui.prefs.contains(self._pref('players')):
            self.players = {p['id'] for p in options if isinstance(saved, list) and p['id'] in saved}
        else:
            self.players = {p['id'] for p in options if p['default']}
        # Old or edited settings cannot enable competing versions together.
        for group in {p['group'] for p in options if p.get('group')}:
            matches = {p['id'] for p in options if p.get('group') == group} & self.players
            if len(matches) > 1:
                self.players.difference_update(matches)
        last = self._saved('lastPack')
        self.last_pack = None
        if not isinstance(last, dict) or not isinstance(last.get('system'), str):
            return
        if not all(isinstance(last.get(k), str) and Path(last[k]).is_absolute()
                   and '..' not in Path(last[k]).parts for k in ('source', 'folder')):
            return
        if isinstance(last.get('games'), list) and all(isinstance(s, str) for s in last['games']):
            self.last_pack = last

    @property
    def _platform_folder(self):
        return self.profile['destination'] == 'platform'

    def _profiles_menu(self):
        body = ('Saved profile unavailable. Choose your handheld and its current firmware.' if self.profile_missing
                else 'Choose the system actually running on your handheld.')
        self.ui._show('pack_profiles', 'Handheld and firmware', body, [
            row('tool_profile_set', p['label'], ', '.join(p['models']) or p['firmware'],
                'Current' if p is self.profile and not self.profile_missing else '', p['id']) for p in self.profiles.values()])

    @staticmethod
    def _formats(systems):
        return ' · '.join(f"{scanner.SYSTEM_LABELS.get(s, s)}: {', '.join(f.lstrip('.').upper() for f in formats)}"
                          for s, formats in systems.items())

    def _players_menu(self, reset=True):
        self.ui._show('pack_players', 'Players and formats',
                      'Select only installed players. For an unknown version, leave advanced formats off.', [
                          row('back', 'Use these players'),
                          *(row('tool_player_toggle', p['label'], self._formats(p['systems']),
                                'On' if p['id'] in self.players else 'Off', p['id']) for p in self.profile['players'])],
                      reset=reset)

    def _profile_info(self):
        self.ui._show('pack_info', self.profile['label'], self.profile['hint'], [
            row('back', 'Return to export'),
            row('note', 'Evidence', self.profile['evidence']),
            row('note', 'What the copy checks', 'SHA-256 compares file contents. Test boot, saves and disc swaps on your handheld.'),
            *(row('note', 'Source', url) for url in self.profile['sources'])])

    def activate(self, key, arg):
        ui = self.ui
        if key == 'tool_plan':
            if ui.backend is None:
                return ui._pick('library')
            self.systems = set(ui.enabled_systems)
            return self._plan_setup()
        if key == 'tool_systems':
            return self._plan_systems()
        if key == 'tool_system':
            self.systems.symmetric_difference_update([arg])
            return self._plan_systems(False)
        if key == 'tool_all_systems':
            self.systems = set() if len(self.systems) == len(scanner.SYSTEM_LABELS) else set(scanner.SYSTEM_LABELS)
            return self._plan_systems(False)
        if key == 'tool_goal':
            return ui._show('space_goal', 'Compression goal', 'Choose the difference you want after conversion and eventual cleanup.',
                            [row('tool_set_goal', size(n * GIB) if n else 'All that fits', '',
                                 'Current' if self.goal == n * GIB else '', n) for n in (1, 5, 10, 20, 50, 100, 0)])
        if key == 'tool_set_goal':
            self.goal = int(arg) * GIB
            return self._plan_setup()
        if key == 'tool_preview':
            if not self.systems:
                return self._plan_systems()
            return ui._work('Estimating space', lambda: ui.backend.plan(self.systems, ui.psp_format, self.goal), self._show_plan)
        if key == 'tool_run_plan':
            sources = tuple(self.plan['sources'])
            systems = frozenset(self.systems)
            return ui._ask('Start this space plan?',
                           f'{len(sources)} sources in {ui.root}. Verified originals eligible for cleanup go to backup, not the trash.'
                           + (' Stop EmulationStation first to protect playlist metadata.' if ui.recalbox else ''),
                           'Start verified batch', lambda: ui._run(False, sources, plan=(systems, self.goal)),
                           return_to=lambda: self._show_plan(self.plan))
        if key in ('tool_health', 'tool_export'):
            return self._source_menu(key.removeprefix('tool_'))
        if key in ('tool_health_here', 'tool_export_here'):
            return self.open_source('health' if key == 'tool_health_here' else 'export', ui.backend.root)
        if key in ('tool_health_browse', 'tool_export_browse'):
            return ui._pick('health_source' if key == 'tool_health_browse' else 'export_source')
        if key == 'tool_profiles':
            return self._profiles_menu()
        if key == 'tool_profile_set':
            self.profile = self.profiles[arg]
            self.profile_missing = False
            ui.prefs.setValue('handheld/profile', arg)
            self._load_profile_settings()
            return self._source_menu('export')
        if key == 'tool_players':
            return self._players_menu()
        if key == 'tool_player_toggle':
            option = next(p for p in self.profile['players'] if p['id'] == arg)
            if arg in self.players:
                self.players.remove(arg)
            else:
                if group := option.get('group'):
                    self.players.difference_update(p['id'] for p in self.profile['players'] if p.get('group') == group)
                self.players.add(arg)
            ui.prefs.setValue(self._pref('players'), json.dumps(sorted(self.players)))
            return self._players_menu(False)
        if key == 'tool_profile_info':
            return self._profile_info()
        if key == 'tool_pack_repeat':
            return self.open_source('export', self.last_pack['source'], repeat=True)
        if key == 'tool_last_folder':
            return self.export_to(Path(self.previous_folder), previous=True)
        if key == 'tool_filter':
            return self._pack_filters()
        if key == 'tool_filter_by':
            if self._platform_folder and self.filter != arg:
                self.selected.clear()
                self.repeat_note = ''
            self.filter = arg
            return self.show_pack()
        if key == 'tool_pack_toggle':
            self.selected.symmetric_difference_update([arg])
            return self.show_pack(False)
        if key == 'tool_pack_bulk':
            shown = {g['path'] for g in self._visible_games()}
            if shown <= self.selected:
                self.selected.difference_update(shown)
            else:
                self.selected.update(shown)
            return self.show_pack(False)
        if key == 'tool_pack_dest':
            return ui._pick('export_dest')
        if key == 'tool_review':
            return self._load_reviews(arg)
        if key == 'tool_disc':
            return self._show_disc(next(r for r in self.reviews if r['id'] == arg))
        if key == 'tool_mark':
            disc = next(r for r in self.reviews if r['id'] == arg)
            def change():
                ui.backend.set_play_tested(arg, not disc['tested'])
                return ui.backend.review()
            return ui._work('Saving play-test mark', change, self._show_reviews)
        if key == 'tool_reclaim':
            if ui.backend is None:
                return ui._pick('library')
            return ui._work('Checking tested backups', ui.backend.reclaim_preview, self._show_reclaim)
        if key == 'tool_reclaim_confirm':
            preview = self.reclaim
            return ui._ask('Permanently remove these backups?', self._reclaim_description(preview)
                           + '\nThis cannot be undone. Verified game files stay in the library.',
                           'Permanently remove backups', lambda: self._purge(preview['token']),
                           return_to=lambda: self._show_reclaim(preview))
        raise ValueError(f'Unknown library action: {key}')

    def back(self, kind):
        if kind in ('plan_systems', 'space_goal', 'plan_preview'):
            self._plan_setup()
        elif kind == 'pack_profiles' and self.profile_missing:
            self.ui._hide()
        elif kind in ('pack_profiles', 'pack_players', 'pack_info', 'pack_system'):
            self._source_menu('export')
        elif kind in ('pack_filter', 'pack_changed'):
            self.show_pack()
        elif kind == 'pack_games':
            self._source_menu('export')
        elif kind == 'health_report':
            self._source_menu('health')
        elif kind == 'play_disc':
            self._show_reviews(self.reviews)
        else:
            return False
        return True

    def _plan_setup(self):
        goal = size(self.goal) if self.goal else 'All that fits'
        names = ', '.join(scanner.SYSTEM_LABELS[key] for key in scanner.SYSTEM_LABELS if key in self.systems)
        self.ui._show('space_plan', 'Make room on this card',
                      f'{self.ui.root}\nEstimate a batch for this working library. Nothing changes until you start it.', [
                          row('tool_goal', 'Compression goal', 'Estimated savings, not space freed today.', goal),
                          row('tool_systems', 'Systems to include', names or 'Select at least one system.', len(self.systems)),
                          row('tool_preview', 'Preview batch', 'See the games, staging space and exclusions.' if self.systems else 'Choose systems first.')])

    def _plan_systems(self, reset=True):
        all_selected = len(self.systems) == len(scanner.SYSTEM_LABELS)
        self.ui._show('plan_systems', 'Systems to include', 'Choose systems for this plan only.', [
            row('back', 'Use these systems'), row('tool_all_systems', 'Clear all' if all_selected else 'Select all systems'),
            *(row('tool_system', name, '', 'Included' if key in self.systems else 'Off', key)
              for key, name in scanner.SYSTEM_LABELS.items())], reset=reset)

    def _show_plan(self, plan):
        self.plan = plan
        estimate = size(plan['estimated_savings_bytes'])
        body = (f"{len(plan['sources'])} sources · {estimate} estimated compression savings.\n"
                f"Free now: {size(plan['free_bytes'])}. Starting space needed for staging: {size(plan['required_free_bytes'])}.\n"
                f"Quarantine frees 0 B. Expect about {size(plan['estimated_output_bytes'])} of extra outputs while backups remain. "
                'Play-test the games before reclaiming their backups.')
        if self.goal and not plan['target_met']:
            body += f'\nThe feasible batch falls short of the {size(self.goal)} goal.'
        rows = [row('back', 'Adjust plan')]
        if plan['sources']:
            rows.append(row('tool_run_plan', 'Start this batch', 'Run only the listed sources, in this order.', 'Review'))
        for i, item in enumerate(plan['items'], 1):
            rows.append(row('note', f"{i}. {Path(item['path']).name}",
                            f"{item['path']}\n{size(item['source_bytes'])} → about {size(item['estimated_output_bytes'])} · "
                            f"{size(item['required_free_bytes'])} staging headroom"))
        for item in plan['excluded']:
            rows.append(row('note', f"Excluded: {Path(item['path']).name}", f"{item['reason']}\n{item['path']}"))
        if plan['note']:
            rows.append(row('note', 'About these estimates', plan['note']))
        self.ui._show('plan_preview', 'Space plan', body, rows)

    def _source_menu(self, action):
        if action == 'export':
            if self.profile_missing:
                return self._profiles_menu()
            version = next((p['label'] for p in self.profile['players'] if p.get('group') and p['id'] in self.players),
                           self.profile['firmware'])
            rows = [row('tool_profiles', 'Handheld and firmware', self.profile['label']),
                    row('tool_players', 'Players and formats', version, len(self.players))]
            if self.players:
                if self.last_pack:
                    rows.insert(0, row('tool_pack_repeat', 'Sync last selection', self.last_pack['source'], 'Review'))
                if self.ui.backend is not None:
                    rows.append(row('tool_export_here', 'Use current library', self.ui.location))
                rows.append(row('tool_export_browse', 'Browse a source folder', 'Mounted library or system folder.'))
            rows.append(row('tool_profile_info', 'About this profile', 'Folder layout, evidence and playback checks.'))
            body = ('Choose games, then confirm the connected card. This copies files; it does not install an app.'
                    if self.players else 'No player selected. Choose the emulator installed on your handheld to see eligible games.')
            return self.ui._show('export_source', 'Sync to handheld', body, rows)
        rows = []
        if self.ui.backend is not None:
            rows.append(row(f'tool_{action}_here', 'Use current library', self.ui.location))
        rows.append(row(f'tool_{action}_browse', 'Browse a source folder', 'Choose a mounted library or one system folder.'))
        self.ui._show('health_source', 'Library health check', 'Check this source without changing its files.', rows)

    def open_source(self, action, path, *, repeat=False):
        profile, players = self.profile['id'], tuple(sorted(self.players))
        def load():
            origin = Backend(path)
            return origin, origin.health() if action == 'health' else origin.export_entries(profile, players=players)
        def loaded(result):
            origin, data = result
            if action == 'health':
                body = f"{origin.root}\n{data['count']} findings. No files changed."
                rows = [row('back', 'Choose another source')]
                rows += [row('note', Path(i['path']).name, f"{i['reason']}\nNext: {i['action']}\n{i['path']}") for i in data['issues']]
                return self.ui._show('health_report', 'Library health', body, rows)
            self.origin, self.catalog = origin, data
            self.catalog_systems = sorted({g['system'] for g in data})
            previous = set(self.last_pack['games']) if repeat else set()
            self.selected = {g['path'] for g in data if g['path'] in previous}
            missing = len(previous) - len(self.selected)
            self.repeat_note = f'{missing} previously selected no longer eligible or present.' if missing else ''
            self.filter = ''
            if self._platform_folder and data:
                if len(self.catalog_systems) == 1:
                    self.filter = self.catalog_systems[0]
                elif repeat and self.last_pack.get('system') in self.catalog_systems:
                    self.filter = self.last_pack['system']
                    self.selected.intersection_update(g['path'] for g in data if g['system'] == self.filter)
                else:
                    self.selected.clear()
                    return self._pack_filters(first=True)
            self.show_pack()
        self.ui._work('Checking library health' if action == 'health' else 'Finding matching game files', load, loaded)

    def _pack_filters(self, first=False):
        systems = self.catalog_systems if self._platform_folder else ['', *self.catalog_systems]
        self.ui._show('pack_system' if first else 'pack_filter',
                      'Choose one system' if self._platform_folder else 'Show games',
                      'Each system uses its own game folder. Changing system starts a new selection.'
                      if self._platform_folder else 'Changing this view keeps your selection.', [
                          row('tool_filter_by', scanner.SYSTEM_LABELS.get(s, 'All systems'), '',
                              'Current' if self.filter == s else '', s) for s in systems])

    @property
    def folder_hint(self):
        if self._platform_folder:
            name = scanner.SYSTEM_LABELS.get(self.filter, self.filter)
            return f'Choose the existing {name} game folder used by your player, not the card root.'
        if self.profile['destination'] == 'rom-root':
            return self.profile['hint']
        return 'Choose the card’s ROMs folder or a separate export folder.'

    @property
    def previous_folder(self):
        if (self.last_pack and self.last_pack.get('identity') and
                (not self._platform_folder or self.last_pack.get('system') == self.filter)):
            return self.last_pack['folder']
        return None

    def _visible_games(self):
        return [g for g in self.catalog if not self.filter or g['system'] == self.filter]

    def _pack_summary(self):
        total = sum(g['size'] for g in self.catalog if g['path'] in self.selected)
        return f'{len(self.selected)} selected · up to {size(total)} to copy'

    def show_pack(self, reset=True):
        games = self._visible_games()
        rows = []
        body = f"{self.profile['label']}\n{self._pack_summary()}"
        if self.repeat_note:
            body += '\n' + self.repeat_note
        if self.catalog:
            if self.selected:
                rows.append(row('tool_pack_dest', 'Choose destination folder', self.folder_hint, 'Browse'))
            else:
                # Keep game and bulk rows in place when the first title is selected with a controller.
                rows.append(row('note', 'Select games below', 'Then choose where to copy them.'))
            if len(self.catalog_systems) > 1:
                rows.append(row('tool_filter', 'Change system' if self._platform_folder else 'Show games', '',
                                scanner.SYSTEM_LABELS.get(self.filter, 'All systems')))
            all_shown = all(g['path'] in self.selected for g in games)
            rows.append(row('tool_pack_bulk', 'Clear shown games' if all_shown else 'Select all shown games'))
            rows += [row('tool_pack_toggle', g['title'], f"{g['path']} · {g['files']} files · {size(g['size'])}",
                         'Selected' if g['path'] in self.selected else 'Not selected', g['path']) for g in games]
        else:
            body = f"No eligible games in {self.origin.root}."
            if self.repeat_note:
                body += '\n' + self.repeat_note
            rows += [row('back', 'Choose another source or profile'),
                     row('note', 'What to look for', 'Choose a system folder or its ROM library. Export uses complete images or supported playlists; it does not convert sources.')]
            rows += [row('note', p['label'], self._formats(p['systems']))
                     for p in self.profile['players'] if p['id'] in self.players]
        self.ui._show('pack_games', 'Choose games', body, rows, reset=reset)

    def export_to(self, dest, *, previous=False):
        origin = self.origin
        selected = tuple(g['path'] for g in self.catalog if g['path'] in self.selected)
        profile, players = self.profile['id'], tuple(sorted(self.players))

        def identify():
            try:
                with handheld.open_directory(handheld.safe_absolute(dest)) as fd:
                    st = os.fstat(fd)
                    return st.st_dev, st.st_ino
            except OSError:
                if not previous:
                    raise
                return None

        def review(identity):
            if previous and (not identity or list(identity) != self.last_pack['identity']):
                self.last_pack.pop('identity', None)
                return self.ui._show('pack_changed', 'Choose the card again',
                                     f'The previous folder is missing or has changed:\n{dest}\nNothing was copied.', [
                                         row('tool_pack_dest', 'Browse connected folders', self.folder_hint),
                                         row('back', 'Return to games')])
            self.ui._ask('Copy to this folder?', f'To: {dest}\n{self.profile["label"]} · {self._pack_summary()}\n'
                         f'From: {origin.root}\nCheck the connected card. Identical files are skipped; other files are never replaced.',
                         'Copy and verify', lambda: execute(identity), return_to=self.show_pack)

        def execute(identity):
            def finished(result):
                self.last_pack = dict(source=str(origin.root), folder=str(dest), games=list(selected),
                                      system=self.filter, identity=list(identity))
                self.ui.prefs.setValue(self._pref('lastPack'), json.dumps(self.last_pack))
                text = (f"{dest}\nFiles copied and verified: {len(result['copied'])} ({size(result['bytes'])}). "
                        f"Identical files skipped: {len(result['skipped'])}. Originals kept. Test playback on your handheld.")
                lines = [f'Copied: {p}' for p in result['copied']] + [f"Already identical: {p}" for p in result['skipped']]
                self.ui._report('Handheld export complete.')
                self.ui._notice('Handheld export complete', text, lines)
                self.ui.cue('done')
            self.ui._hide()
            self.ui._work('Copying and verifying files',
                          lambda: origin.export_handheld(selected, dest, profile, players=players,
                                                         expected_destination=identity), finished)
        self.ui._work('Checking destination folder', identify, review)

    def _load_reviews(self, path):
        if self.ui.backend is None:
            return self.ui._pick('library')
        def loaded(reviews):
            if path:
                self.reviews = reviews
                disc = next((r for r in reviews if r['path'] == path), None)
                if disc is None:
                    return self.ui._notice('Cannot mark this file', 'It is no longer a reviewable, verified output. Scan the library again.')
                return self._show_disc(disc)
            self._show_reviews(reviews)
        self.ui._work('Loading verified games', self.ui.backend.review, loaded)

    def _show_reviews(self, reviews):
        self.reviews = reviews
        tested = sum(r['tested'] for r in reviews)
        rows = [row('tool_reclaim', 'Review tested backups', 'See exactly what can be permanently removed.')]
        if reviews:
            rows += [row('tool_disc', Path(r['path']).name, r['path'], 'Play-tested' if r['tested'] else 'Not tested', r['id']) for r in reviews]
        else:
            rows.append(row('back', 'Return to library', 'Convert and verify a game before marking it play-tested.'))
        self.ui._show('play_reviews', 'Play-test your games', f'{len(reviews)} verified files · {tested} marked play-tested. '
                      'File verification does not test playability. Select a disc after trying it in your emulator.', rows)

    def _show_disc(self, disc):
        tested = disc['tested']
        self.ui._show('play_disc', Path(disc['path']).name,
                      f"{disc['path']}\nFile verified · {'Play-tested' if tested else 'Not play-tested'}. "
                      'Check boot, saves, music and disc swapping where applicable. The mark applies only to this disc.',
                      [row('back', 'Return to verified games'), row('tool_mark', 'Clear play-test mark' if tested else 'I play-tested this disc', '', '', disc['id'])])

    @staticmethod
    def _reclaim_description(preview):
        batches = ', '.join(str(b) for b in preview['batches'])
        return (f"Backup files: {preview['count']} · {size(preview['bytes'])} of originals. "
                + (f'Batches losing Undo: {batches}. Undo cannot go back past a reclaimed batch.'
                   if batches else 'No batch will lose Undo.'))

    def _show_reclaim(self, preview):
        self.reclaim = preview
        body = self._reclaim_description(preview)
        rows = [row('back', 'Keep backups')]
        if preview['count']:
            rows.append(row('tool_reclaim_confirm', 'Remove listed backups', 'One more confirmation is required.', 'Review'))
            rows.extend(row('note', Path(p).name, p) for p in preview['files'])
        else:
            body += ' Mark verified games after play-testing. Only eligible backups belonging to tested outputs can be reclaimed.'
        self.ui._show('reclaim_preview', 'Backups ready to reclaim', body, rows)

    def _purge(self, token):
        ui = self.ui
        ui._hide()
        def change():
            before = hardware.free_bytes(ui.root)
            return before, ui.backend.reclaim_reviewed(token)
        def done(result):
            before, count = result
            def refreshed(warning):
                text = ui._with_free_space(f'{count} tested backup files removed.', before)
                ui._report(text)
                ui._notice('Reclaim complete', text, [warning] if warning else [])
            ui._refresh_after_commit(refreshed)
        ui._work('Removing tested backups', change, done)
