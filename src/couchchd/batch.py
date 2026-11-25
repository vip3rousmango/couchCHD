"""Workers only prepare files. The lock holder alone publishes and journals them."""

import errno
import hashlib
import os
import shutil
import sqlite3
import time
import uuid
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from . import cloud_ingest, converter, hardware, scanner
from .output_formats import can_replace_source
from .telemetry import ItemProgress

GIB = 1024**3


def child_dir(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError(f'Path outside library: {relative}')
    path = root
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise ValueError(f'Refusing linked directory: {path}')
        path.mkdir(exist_ok=True)
    return path


def fingerprint(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def publish_prepared(source: Path, target: Path):
    # target is a fresh, private name supplied by the ledger, never a library entry.
    try:
        source.rename(target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        with source.open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        with source.open('rb') as src, target.open('rb') as dst:
            if hashlib.file_digest(src, 'sha256').digest() != hashlib.file_digest(dst, 'sha256').digest():
                raise OSError(f'Checksum mismatch after copying {source.name}')


@dataclass(frozen=True)
class Job:
    game: scanner.Game
    output: Path
    local: bool
    origin: str | None = None  # ingest scratch paths are not persistent identities

    @property
    def may_move_original(self):
        # XISO and trimmed 3DS preserve game content, not a byte-exact disc dump.
        return self.local and can_replace_source(self.output)

    def output_bound(self):
        size = self.game.size
        if self.game.mode == 'createcd':
            size = max(size, sum((frames + 3) // 4 * 4 for frames in self.game.frames) * 2448)
        # Do not assume media compresses: containers also need headers and indexes.
        return size + (size + 31) // 32 + 64 * 1024

    def report(self, callback, stage, percent=None, mib_s=None, cpu_percent=None, detail=''):
        if callback is not None:
            callback(ItemProgress(self.origin or str(self.game.source), self.game.source.name, self.game.system,
                                  stage, percent, mib_s, cpu_percent, detail))


class Batch:
    def __init__(self, root, scratch, db, batch, tool, guard, plan, quarantine, immediate, progress, cancel,
                 on_item=None, on_space=None):
        self.root, self.scratch, self.db, self.batch = root, scratch, db, batch
        self.tool, self.guard, self.plan = tool, guard, plan
        self.quarantine, self.cancel, self.on_item = quarantine, cancel, on_item
        self.on_space = on_space
        self.next_space_sample = 0.0
        self.native_progress = progress
        self.progress = progress or (lambda _: None)
        # The coordinator sees the device's full available set; portable_budget
        # may narrow the worker's CPU affinity before convert() starts.
        self.device_cpus = hardware.cpu_count() if on_item is not None else 0
        self.immediate = immediate
        self.outputs = []
        self.errors = []
        self.saved = 0
        self.paused = ''
        self.interrupt_stage = 'cancelled'
        self.sources = set()
        self.root_identity = fingerprint(root)[:2]

    def _location(self, job):
        if job.output.name != scanner.output_path(job.game.source, job.game.mode).name:
            raise ValueError(f'Destination does not match the selected format: {job.output}')
        if fingerprint(self.root)[:2] != self.root_identity:
            raise OSError('Library mount changed; reselect the original library.')
        directory = child_dir(self.root, job.output.parent.relative_to(self.root))
        kind = hardware.fs_type(directory)
        if kind == 'fat32' and job.output_bound() >= 4 * GIB:
            raise ValueError('FAT32 cannot safely stage this image; use exFAT or another filesystem.')
        if job.output.exists() or job.output.is_symlink():
            raise FileExistsError(f'Output already exists: {job.output}')
        if job.output.suffix.lower() == '.chd' and (directory / '.chd_discs' / job.output.name).exists():
            raise FileExistsError(f'An organized disc already exists: {job.output.name}')
        staging = job.local and hardware.fs_type(job.game.source) in cloud_ingest.NETWORK
        work = self.scratch if staging or kind in cloud_ingest.NETWORK else child_dir(directory, Path('.couchchd_tmp'))
        return work, staging

    def _prepare(self, job, work, stage, target, threads):
        context = cloud_ingest.stage(job.game, work, cancel=self.cancel) if stage else nullcontext(job.game)
        with context as game:
            if game.mode != job.game.mode:
                raise ValueError(f'Staged format changed for {job.game.source.name}; input retained.')
            on_stage = partial(job.report, self.on_item) if self.on_item is not None else None
            converter.convert(game, target, self.tool, cancel=self.cancel, threads=threads,
                              progress=self.native_progress, on_stage=on_stage, device_cpus=self.device_cpus,
                              fast_cd=self.plan.portable)
        job.game.assert_unchanged()

    def _run_job(self, job, work, stage, target, threads):
        # Never change the UI/coordinator's CPU affinity. Restrict only the
        # executor thread that will spawn chdman, before native threads exist.
        budget = (hardware.portable_budget(threads, self.progress, self.cancel) if self.plan.portable
                  else nullcontext(threads))
        with budget as cpus:
            self._prepare(job, work, stage, target, cpus)

    def _publish(self, job, prepared):
        if self.cancel.is_set():
            raise InterruptedError('Batch cancelled; input retained.')
        job.game.assert_unchanged()
        size = prepared.stat().st_size
        job.report(self.on_item, 'publishing')
        self.progress(f'Publishing: {job.output.name}')
        if self.cancel.is_set():
            raise InterruptedError('Batch cancelled before publication; input retained.')
        self.db.publish(self.batch, job.output, lambda stage: publish_prepared(prepared, stage))
        self.outputs.append(job.output)
        # Count distinct input files: shared tracks do not represent duplicate savings.
        new_bytes = sum(p.stat().st_size for p in job.game.files if p not in self.sources)
        self.sources.update(job.game.files)
        self.saved += new_bytes - size
        self.progress(f'Verified: {job.output.name} (compression delta {new_bytes - size} bytes)')
        if self.quarantine and job.may_move_original:
            game = job.game
            game.assert_unchanged()
            backups = []
            for path in dict.fromkeys((game.source, *game.files)):
                sig = fingerprint(path)
                game.assert_unchanged(path)
                if path.suffix.lower() not in {'.cue', '.gdi'} and not scanner.unreferenced(path, self.root):
                    self.progress(f'Retained referenced track: {path.name}')
                    continue
                backups.append(self.db.quarantine(self.batch, path, expected=sig, outputs=[job.output]))
            if self.immediate:
                self.db.purge_job(self.batch, backups)
        elif self.quarantine and job.local:
            self.progress(f'Original retained: {job.game.source.name} (required for this format).')

    def _error(self, exc, name):
        stage = 'failed'
        if ((isinstance(exc, OSError) and exc.errno == errno.ENOSPC)
                or getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL):
            self.paused = 'Storage full. Free space, then resume the batch.'
            self.interrupt_stage = stage = 'paused'
            self.cancel.set()  # running converters observe this and terminate their children
        elif isinstance(exc, InterruptedError) and self.cancel.is_set():
            stage = self.interrupt_stage
            if not self.paused:
                self.paused = 'Cancelled. Remaining inputs were not moved.'
        message = f'{name}: {exc}'
        self.errors.append(message)
        self.progress(message)
        return stage

    def process(self, jobs):
        if not jobs:
            return
        # Ingest announces its source before it starts the potentially long download.
        if self.on_item is not None:
            for job in jobs:
                if job.local:
                    job.report(self.on_item, 'queued')
        pending = deque(jobs)
        try:
            self._dispatch(pending)
        except BaseException as exc:
            for job in pending:
                job.report(self.on_item, 'skipped', detail=f'Batch stopped before conversion: {exc}')
            raise
        if self.cancel.is_set() and not self.paused:
            self.paused = 'Cancelled. Verified jobs were retained; remaining inputs were not moved.'
        for job in pending:
            job.report(self.on_item, self.interrupt_stage if self.cancel.is_set() else 'paused',
                       detail=self.paused)

    def _dispatch(self, pending):
        total = sum(j.output_bound() for j in pending)
        if hardware.free_bytes(self.root) < total:
            ordered = sorted(pending, key=lambda j: j.game.size)
            pending.clear()
            pending.extend(ordered)
        reserved = Counter()
        active = {}
        previous_reason = ''
        # One portable disc owns the available compression CPUs; an entire
        # library on an SD card must not become competing random writers.
        width = 1 if self.plan.portable else min(len(pending), self.plan.cpus, 4)
        with ThreadPoolExecutor(max_workers=width) as pool:
            try:
                while pending or active:
                    reason = self.guard.pause_reason()
                    if reason and reason != previous_reason:
                        self.progress(reason)
                    previous_reason = reason
                    if reason.lower().startswith('battery'):
                        self.paused = reason
                    if reason.lower().startswith(('suspend', 'resume')) and not self.cancel.is_set():
                        self.cancel.wait(0.25)
                        continue  # do not publish finished jobs during the remount window
                    if self.on_space is not None and time.monotonic() >= self.next_space_sample:
                        self.on_space(hardware.free_bytes(self.root))
                        self.next_space_sample = time.monotonic() + 5
                    cpus = self.plan.cpus if self.plan.portable else min(self.plan.cpus, self.guard.limit())
                    cap = min(width, cpus)
                    while (pending and len(active) < cap and not self.cancel.is_set()
                           and not self.paused and not reason):
                        job = pending[0]
                        try:
                            work, stage = self._location(job)
                            # On one filesystem publication renames the prepared file.
                            # Across mounts it briefly needs the full file on both.
                            estimate = job.output_bound() + GIB
                            scratch = job.game.size if stage else 0
                            if job.game.sheet:
                                # chdman can require private copies of track extents.
                                scratch += job.game.size
                            needs = {}
                            volumes = [(job.output.parent, estimate), (work, estimate + scratch)]
                            if self.quarantine and job.may_move_original and job.game.source.stat().st_dev != self.root_identity[0]:
                                # The output remains live during cross-device quarantine.
                                backup = job.game.size + GIB
                                if job.output.parent.stat().st_dev == self.root_identity[0]:
                                    backup += job.output_bound()
                                volumes.append((self.root, backup))
                            for location, amount in volumes:
                                device = location.stat().st_dev
                                if device not in needs or needs[device][1] < amount:
                                    needs[device] = location, amount
                            if any(hardware.free_bytes(loc) - reserved[dev] < max(2 * GIB, amount)
                                   for dev, (loc, amount) in needs.items()):
                                if not active:
                                    self.paused = f'Insufficient headroom for {job.output.name}. Free space and resume.'
                                    self.progress(self.paused)
                                break
                            name = scanner.output_path(Path(uuid.uuid4().hex), job.game.mode).name
                            target = work / (name + '.part')
                            self.progress(f'Preparing: {job.game.source.name}')
                            if job.local:
                                job.report(self.on_item, 'preparing')
                            dest_identity = fingerprint(job.output.parent)[:2]
                            future = pool.submit(self._run_job, job, work, stage, target, cpus // cap)
                            active[future] = job, target, needs, dest_identity
                            for dev, (_, amount) in needs.items():
                                reserved[dev] += amount
                        except (OSError, RuntimeError, ValueError) as exc:
                            stage = self._error(exc, job.game.source.name)
                            job.report(self.on_item, stage, detail=str(exc))
                        pending.popleft()
                    if not active:
                        if self.paused or self.cancel.is_set() or not pending:
                            break
                        self.cancel.wait(0.25)  # suspend/remount guard
                        continue
                    done, _ = wait(active, timeout=0.25, return_when=FIRST_COMPLETED)
                    for future in done:
                        job, target, needs, dest_identity = active.pop(future)
                        published_before = len(self.outputs)
                        stage, detail = 'failed', 'Batch interrupted.'
                        try:
                            future.result()
                            if (fingerprint(self.root)[:2] != self.root_identity
                                    or fingerprint(job.output.parent)[:2] != dest_identity):
                                raise OSError('Destination mount changed; prepared output was not published.')
                            self._publish(job, target)
                        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                            stage = self._error(exc, job.game.source.name)
                            detail = str(exc)
                        else:
                            stage, detail = 'done', ''
                        finally:
                            try:
                                target.unlink(missing_ok=True)
                            except OSError as exc:
                                stage = self._error(exc, job.game.source.name)
                                detail = f'{detail}; {exc}' if detail else str(exc)
                            for dev, (_, amount) in needs.items():
                                reserved[dev] -= amount
                            if len(self.outputs) > published_before:
                                # Quarantine, purge, or scratch cleanup can fail *after*
                                # the ledger has committed a verified output.
                                stage = 'done'
                                if detail:
                                    detail = f'{job.output.name} verified and published. Post-publication error: {detail}'
                            job.report(self.on_item, stage, 100.0 if stage == 'done' else None, detail=detail)
            finally:
                if active:
                    self.cancel.set()
                    wait(active)
                    for job, target, _, _ in active.values():
                        try:
                            target.unlink(missing_ok=True)
                        except OSError as exc:
                            self._error(exc, job.game.source.name)
                        job.report(self.on_item, self.interrupt_stage, detail='Batch stopped before publication.')
