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


def _disc_output(root, game):
    """Publish beside the frontend's system gamelist, even for nested source sets."""
    for folder in game.source.parents:
        if scanner.SYSTEMS.get(folder.name.casefold()) == game.system:
            return folder / game.source.with_suffix('.chd').name
        if folder == root:
            break
    raise ValueError(f'No system folder for {game.source}')


def _source_system(root, path):
    if not path.is_relative_to(root):
        return ''
    parts = (root.name, *path.relative_to(root).parts[:-1])
    return next((scanner.SYSTEMS[p.lower()] for p in reversed(parts) if p.lower() in scanner.SYSTEMS), '')


def _artifacts(root):
    chds, playlists = [], []
    for directory, dirs, files in os.walk(root):
        base = Path(directory)
        dirs[:] = [d for d in dirs if (not d.startswith('.') or d == '.chd_discs')
                   and d != '_couchchd_backup' and not (base / d).is_symlink()]
        for name in files:
            path = base / name
            group = {'.chd': chds, '.m3u': playlists}.get(path.suffix.lower())
            if group is not None and path.is_file() and not path.is_symlink():
                group.append(str(path))
    return sorted(chds), sorted(playlists)


class Backend:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError(f'Not a library directory: {root}')
        if '_couchchd_backup' in self.root.parts or '.couchchd_tmp' in self.root.parts:
            raise ValueError('Choose the ROM library, not its backup or staging directory.')

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
            chds, playlists = _artifacts(self.root)
            return {
                'games': [{'path': str(g.source), 'system': g.system, 'size': g.size,
                           'tracks': [str(p) for p in g.files if p != g.source]} for g in games],
                'issues': [{'path': str(i.path), 'category': i.category, 'reason': i.reason} for i in issues],
                'chds': chds, 'playlists': playlists, 'verified': [str(p) for p in db.verified()],
                'free': hardware.free_bytes(self.root),
            }

    def run(self, *, systems=None, sources=None, ingest=None, preset='balanced', quarantine=True,
            immediate=False, progress=None, cancel=None, on_item: Callable[[ItemProgress], None] | None = None):
        report = progress or (lambda _: None)
        cancel = threading.Event() if cancel is None else cancel
        if immediate and (not quarantine or ingest):
            raise ValueError('Immediate cleanup is only available for local, verified quarantine jobs.')
        if systems is None:
            systems = profiles.default_systems()
        if ingest and Path(ingest).is_dir():
            origin = Path(ingest).resolve()
            if origin.is_relative_to(self.root) or self.root.is_relative_to(origin):
                raise ValueError('Ingest source and destination must be separate libraries.')
        with self._session() as db, hardware.Watchdog() as guard, tempfile.TemporaryDirectory(prefix='couchchd_ingest-') as tmp:
            jobs = []
            errors = []
            if not ingest:
                games, issues = scanner.scan(self.root)
                # Do not silently choose between two inputs that would produce one filename.
                destinations = {g.source: _disc_output(self.root, g) for g in games}
                collisions = Counter(str(p).casefold() for p in destinations.values())
                known = set(db.verified())
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
                    elif output not in known and output.parent / '.chd_discs' / output.name not in known:
                        jobs.append(job)
                    else:
                        message = f'Retained input: a verified CHD already exists for {game.source.name}'
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
                    system = _source_system(self.root, issue.path)
                    if not system or system in systems:
                        if issue.category == 'invalid':
                            errors.append(message)
                        if on_item is not None and (issue.category == 'invalid' or sources is not None):
                            stage = 'failed' if issue.category == 'invalid' else 'skipped'
                            on_item(ItemProgress(key, issue.path.name, system, stage, detail=message))
                for key in sorted(missing or ()):
                    path = Path(key)
                    system = _source_system(self.root, path)
                    if system in systems:
                        message = f'Selected source no longer available: {path}'
                        errors.append(message)
                        if on_item is not None:
                            on_item(ItemProgress(str(path), path.name, system, 'failed', detail=message))
                if not jobs:
                    return dict(converted=[], errors=errors, saved=0, paused='', messages=[])
            try:
                tool = converter.tool_path()
                scratch = Path(tmp)
                plan = profiles.compression_plan(preset)
                batch = db.begin()
                worker = Batch(self.root, scratch, db, batch, tool, guard, plan,
                               quarantine, immediate, progress, cancel, on_item)
            except Exception as exc:
                for job in jobs:
                    job.report(on_item, 'failed', detail=f'Could not start batch: {exc}')
                raise
            messages = []
            try:
                if ingest:
                    with closing(cloud_ingest.games(ingest, scratch, systems, cancel, on_item)) as stream:
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
                            if relative.is_absolute() or '..' in relative.parts or relative.suffix.lower() != '.chd' or any(
                                p.startswith('.') or p == '_couchchd_backup' for p in relative.parts
                            ):
                                raise ValueError(f'Unsafe ingest destination: {relative}')
                            # The scratch descriptor's parent is ephemeral; use the
                            # system resolved from the remote, not its scratch name.
                            system_dir = self.root if scanner.SYSTEMS.get(self.root.name.casefold()) == game.system else self.root / game.system
                            worker.process([Job(game, system_dir / relative.name, False, key)])
                else:
                    worker.process(jobs)
                if worker.outputs and not worker.paused:
                    messages = multidisc.organize(self.root, batch, db, worker.outputs)
                    for message in messages:
                        report(message)
            except (OSError, ValueError, RuntimeError) as exc:
                worker._error(exc, 'Batch')
            finally:
                db.finish(batch)
            # Organizing can relocate a newly published disc; report its live name.
            outputs = [p if p.exists() else p.parent / '.chd_discs' / p.name for p in worker.outputs]
            return dict(converted=[str(p) for p in outputs], errors=errors + worker.errors,
                        saved=worker.saved, paused=worker.paused, messages=messages)

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
