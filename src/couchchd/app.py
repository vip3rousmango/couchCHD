"""Shared controller for the QML shell and unattended CLI."""

import os
import tempfile
import threading
from collections import Counter
from collections.abc import Callable
from contextlib import closing, contextmanager
from pathlib import Path

from . import cloud_ingest, converter, hardware, multidisc, profiles, scanner
from .batch import Batch, Job
from .ledger import Ledger
from .locking import library_lock
from .telemetry import ItemProgress


def _disc_output(root, game, psp_format):
    """Publish beside the frontend's system gamelist, even for nested source sets."""
    directory = scanner.system_folder(root, game.source)
    if directory is None:
        raise ValueError(f'No system folder for {game.source}')
    return directory / scanner.output_path(game.source, scanner.output_mode(game, psp_format)).name


def _already_published(output, known):
    return output in known or (output.suffix == '.chd' and output.parent / '.chd_discs' / output.name in known)


def _ingest_folder(root, system):
    if scanner.folder_system(root.name) == system:
        return root
    candidates = [p for p in root.iterdir()
                  if scanner.folder_system(p.name) == system and (p.is_dir() or p.is_symlink())]
    if len(candidates) > 1:
        raise ValueError(f'Multiple {system} folders; select one as the destination library.')
    return candidates[0] if candidates else root / system


def _artifacts(root):
    chds, playlists = [], []
    for directory, dirs, files in os.walk(root):
        base = Path(directory)
        dirs[:] = [d for d in dirs if (scanner.visible_directory(d) or d == '.chd_discs')
                   and not (base / d).is_symlink()]
        for name in files:
            path = base / name
            group = {'.chd': chds, '.m3u': playlists}.get(path.suffix.lower())
            if group is not None and path.is_file() and not path.is_symlink():
                group.append(str(path))
    return sorted(chds), sorted(playlists)


def _scan_game(root, game, known):
    job = Job(game, game.source, True)
    row = dict(path=str(game.source), system=game.system, size=game.size, mode=game.mode,
               output_bound=job.output_bound(), sheet_bytes=game.size if game.sheet else 0,
               tracks=[str(p) for p in game.files if p != game.source],
               verified_modes=[scanner.output_mode(game, fmt)
                               for fmt in (('chd', 'cso') if game.system == 'psp' else ('chd',))
                               if known and _already_published(_disc_output(root, game, fmt), known)])
    if game.archive:
        row.update(archive=True, archive_bytes=game.stamps[0][2], expanded_bytes=game.size,
                   staging_bytes=job.archive_staging_bytes)
    return row


def _scan_rows(root, games, issues, known):
    chds, playlists = _artifacts(root)
    return {
        'games': [_scan_game(root, game, known) for game in games],
        'issues': [{'path': str(i.path), 'category': i.category, 'reason': i.reason} for i in issues],
        'chds': chds, 'playlists': playlists, 'verified': sorted(map(str, known)),
        'free': hardware.free_bytes(root),
    }


class Backend:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError(f'Not a library directory: {root}')
        if not scanner.library_root_allowed(self.root):
            raise ValueError('Choose the ROM library, not a backup, staging, firmware or excluded system folder.')

    @contextmanager
    def _session(self):
        with library_lock(self.root), Ledger(self.root) as db:
            converter.recover(self.root)
            yield db

    def recover(self):
        with self._session():
            return self.root

    def scan(self):
        with self._session() as db:
            games, issues = scanner.scan(self.root)
            return _scan_rows(self.root, games, issues, set(db.verified()))

    def health(self):
        # Diagnostics on a read-only master must not create an SQLite file/lock
        # or replay pending rename operations as a side effect of inspection.
        from .library_tools import health
        games, issues = scanner.scan(self.root)
        return health(self.root, _scan_rows(self.root, games, issues, set()))

    def plan(self, systems, psp_format, target_bytes):
        from .library_tools import plan
        scope = sorted(scanner.SYSTEM_LABELS if systems is None else systems)
        result = plan(self.root, self.scan(), scope, psp_format, target_bytes)
        return dict(result, root=str(self.root), systems=scope, psp_format=psp_format)

    def run_plan(self, systems, psp_format, target_bytes, sources, *, preset='balanced',
                 progress=None, cancel=None, on_item=None, on_space=None):
        if not sources:
            raise ValueError('No games fit this plan. Review the space estimate again.')
        return self.run(systems=systems, sources=tuple(sources), psp_format=psp_format, preset=preset,
                        plan_target=target_bytes, progress=progress, cancel=cancel,
                        on_item=on_item, on_space=on_space)

    def export_entries(self, profile=None, *, players=None):
        from .handheld import entries
        return entries(self.root, profile, players=players)

    def export_handheld(self, selected, dest, profile=None, *, players=None, expected_destination=None):
        from .handheld import export
        return export(self.root, selected, Path(dest), profile, players=players, expected_destination=expected_destination)

    def review(self):
        from .review import review
        with self._session() as db:
            return review(db)

    def set_play_tested(self, id, tested):
        from .review import set_play_tested
        with self._session() as db:
            return set_play_tested(db, id, tested)

    def reclaim_preview(self):
        from .review import reclaim_preview
        with self._session() as db:
            return reclaim_preview(db)

    def reclaim_reviewed(self, token):
        from .review import reclaim_reviewed
        with self._session() as db:
            return reclaim_reviewed(db, token)

    def run(self, *, systems=None, sources=None, ingest=None, preset='balanced', psp_format='chd', quarantine=True,
            immediate=False, progress=None, cancel=None, on_item: Callable[[ItemProgress], None] | None = None,
            on_space: Callable[[int], None] | None = None, plan_target=None, scratch_dir=None):
        report = progress or (lambda _: None)
        cancel = threading.Event() if cancel is None else cancel
        if psp_format not in ('chd', 'cso'):
            raise ValueError('PSP output must be chd or cso.')
        if plan_target is not None and (ingest or immediate or not quarantine):
            raise ValueError('Space plans require local, reversible quarantine jobs.')
        if immediate and (not quarantine or ingest):
            raise ValueError('Immediate cleanup is only available for local, verified quarantine jobs.')
        if systems is None:
            systems = set(scanner.SYSTEM_LABELS) if plan_target is not None else profiles.default_systems(psp_format)
        if ingest and (root_system := scanner.folder_system(self.root.name)):
            systems = set(systems).intersection({root_system})
            if not systems:
                raise ValueError(f'This destination accepts only {root_system} inputs.')
        scratch_base = Path(scratch_dir).expanduser().resolve(strict=True) if scratch_dir else None
        if scratch_base is not None and (not scratch_base.is_dir() or scratch_base.is_relative_to(self.root)):
            raise ValueError('Choose an existing temporary workspace outside the working ROM library.')
        if ingest and Path(ingest).is_dir():
            origin = Path(ingest).resolve()
            if origin.is_relative_to(self.root) or self.root.is_relative_to(origin):
                raise ValueError('Ingest source and destination must be separate libraries.')
            if scratch_base is not None and scratch_base.is_relative_to(origin):
                raise ValueError('Choose a temporary workspace outside the read-only ingest source.')
        with self._session() as db, hardware.Watchdog() as guard, tempfile.TemporaryDirectory(
                prefix='.couchchd-work-', dir=scratch_base) as tmp:
            volume = self.root.stat()
            free_before = hardware.free_bytes(self.root)
            if on_space is not None:
                on_space(free_before)
            jobs = []
            errors = []
            if not ingest:
                games, issues = scanner.scan(self.root)
                # Do not silently choose between two inputs that would produce one filename.
                destinations = {g.source: _disc_output(self.root, g, psp_format) for g in games}
                collisions = Counter(str(p).casefold() for p in destinations.values())
                known = set(db.verified())
                if plan_target is not None:
                    from .library_tools import plan
                    current = plan(self.root, _scan_rows(self.root, games, issues, known),
                                   systems, psp_format, plan_target)
                    if list(sources or ()) != current['sources']:
                        raise ValueError('Available space or titles changed. Preview the plan again.')
                    indexed = {str(g.source): g for g in games}
                    games = [indexed[path] for path in current['sources']]
                missing = None if sources is None else set(map(str, sources))
                for game in games:
                    if missing is not None:
                        key = str(game.source)
                        if key not in missing:
                            continue
                        missing.remove(key)
                    if game.system not in systems:
                        continue
                    output = destinations[game.source]
                    job = Job(game, output, True)
                    if collisions[str(output).casefold()] > 1:
                        message = f'Ambiguous output for {game.source}; rename one input.'
                        errors.append(message)
                        job.report(on_item, 'failed', detail=message)
                    elif not _already_published(output, known):
                        try:
                            selected = scanner.select_format(game, psp_format)
                        except (OSError, ValueError) as exc:
                            message = f'{game.source.name}: {exc}'
                            errors.append(message)
                            report(message)
                            job.report(on_item, 'failed', detail=message)
                        else:
                            jobs.append(Job(selected, output, True))
                    else:
                        message = f'Retained input: {output.name} is already verified.'
                        report(message)
                        job.report(on_item, 'skipped', detail=message)
                for issue in issues:
                    message = f'{issue.category}: {issue.path.name} — {issue.reason}'
                    report(message)
                    key = str(issue.path)
                    if missing is not None:
                        if key not in missing:
                            continue
                        missing.remove(key)
                    system = scanner.library_system(self.root, issue.path)
                    if not system or system in systems:
                        if issue.category == 'invalid':
                            errors.append(message)
                        if on_item is not None and (issue.category == 'invalid' or sources is not None):
                            stage = 'failed' if issue.category == 'invalid' else 'skipped'
                            on_item(ItemProgress(key, issue.path.name, system, stage, detail=message))
                for key in sorted(missing or ()):
                    path = Path(key)
                    system = scanner.library_system(self.root, path)
                    if system in systems:
                        message = f'Selected source no longer available: {path}'
                        errors.append(message)
                        if on_item is not None:
                            on_item(ItemProgress(str(path), path.name, system, 'failed', detail=message))
                if not jobs:
                    return dict(converted=[], errors=errors, saved=0, paused='', messages=[],
                                free_before=free_before, free_after=hardware.free_bytes(self.root))
            try:
                scratch = Path(tmp)
                plan = profiles.compression_plan(preset)
                batch = db.begin()
                worker = Batch(self.root, scratch, db, batch, None, guard, plan,
                               quarantine, immediate, progress, cancel, on_item, on_space)
            except Exception as exc:
                for job in jobs:
                    job.report(on_item, 'failed', detail=f'Could not start batch: {exc}')
                raise
            messages = []
            try:
                if ingest:
                    def explain(message):
                        messages.append(message)
                        report(message)
                    with closing(cloud_ingest.games(ingest, scratch, systems, cancel, on_item, explain)) as stream:
                        while not worker.paused:
                            reason = guard.pause_reason()
                            if cancel.is_set():
                                worker.paused = 'Ingest cancelled. Remote originals were not changed.'
                                break
                            if reason.lower().startswith(('suspend', 'resume')):
                                cancel.wait(0.25)
                                continue
                            if reason or hardware.free_bytes(self.root) < 2 * 1024**3:
                                worker.paused = reason or 'Free at least 2 GiB on the destination before ingesting.'
                                report(worker.paused)
                                break
                            entry = next(stream, None)
                            if entry is None:
                                break
                            game, relative, key = entry
                            scanner.safe_relative_name(relative.as_posix())
                            if (not all(map(scanner.visible_directory, relative.parts[:-1]))
                                    or relative.name != scanner.output_path(game.source, game.mode).name
                                    or relative.parts[0] != game.system):
                                raise ValueError(f'Unsafe ingest destination: {relative}')
                            try:
                                game = scanner.select_format(game, psp_format)
                            except (OSError, ValueError) as exc:
                                stage = worker._error(exc, game.source.name)
                                if on_item is not None:
                                    on_item(ItemProgress(key, game.source.name, game.system, stage, detail=str(exc)))
                                continue
                            # The scratch descriptor's parent is ephemeral; use the
                            # system resolved from the remote, not its scratch name.
                            system_dir = _ingest_folder(self.root, game.system)
                            output = system_dir / scanner.output_path(game.source, game.mode).name
                            worker.process([Job(game, output, False, key)])
                else:
                    worker.process(jobs)
                if worker.outputs and not worker.paused:
                    organized_messages = multidisc.organize(self.root, batch, db, worker.outputs)
                    messages.extend(organized_messages)
                    for message in organized_messages:
                        report(message)
            except (OSError, ValueError, RuntimeError) as exc:
                worker._error(exc, 'Batch')
            finally:
                db.finish(batch)
            # Organizing can relocate a newly published disc; report its live name.
            outputs = []
            for path in worker.outputs:
                if path.suffix == '.chd' and not path.exists():
                    organized = path.parent / '.chd_discs' / path.name
                    if organized.is_file():
                        path = organized
                outputs.append(str(path))
            try:
                free_after = hardware.free_bytes(self.root) if os.path.samestat(volume, self.root.stat()) else None
            except OSError:
                free_after = None  # A completed publication still belongs in the result.
            return dict(converted=outputs, errors=errors + worker.errors, saved=worker.saved,
                        paused=worker.paused, messages=messages, free_before=free_before, free_after=free_after)

    def history(self):
        with self._session() as db:
            return db.history()

    def undo(self):
        with self._session() as db:
            return db.undo()

    def purge(self):
        with self._session() as db:
            return db.purge()

    def extract(self, chd: Path, dest: Path):
        with self._session():
            path = Path(chd).resolve(strict=True)
            if not path.is_relative_to(self.root) or path.suffix.lower() != '.chd':
                raise ValueError('Select a CHD in this library.')
            return converter.extract(path, Path(dest).expanduser().resolve(), converter.tool_path())
