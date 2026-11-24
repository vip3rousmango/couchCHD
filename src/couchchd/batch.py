"""Workers only prepare files. The lock holder alone publishes and journals them."""

import errno
import hashlib
import os
import shutil
import sqlite3
import uuid
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from . import cloud_ingest, converter, hardware, scanner

GIB = 1024**3
NETWORK = {'nfs', 'cifs', 'sshfs'}


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


class Batch:
    def __init__(self, root, scratch, db, batch, tool, guard, cores, quarantine, immediate, progress, cancel):
        self.root, self.scratch, self.db, self.batch = root, scratch, db, batch
        self.tool, self.guard, self.cores = tool, guard, max(1, cores)
        self.quarantine, self.progress, self.cancel = quarantine, progress, cancel
        self.immediate = immediate
        self.outputs = []
        self.errors = []
        self.saved = 0
        self.paused = ''
        self.sources = set()
        self.root_identity = fingerprint(root)[:2]

    def _location(self, job):
        if fingerprint(self.root)[:2] != self.root_identity:
            raise OSError('Library mount changed; reselect the original library.')
        directory = child_dir(self.root, job.output.parent.relative_to(self.root))
        kind = hardware.fs_type(directory)
        if kind == 'fat32' and job.game.size >= 4 * GIB:
            raise ValueError('FAT32 cannot safely stage this image; use exFAT or another filesystem.')
        if job.output.exists() or job.output.is_symlink():
            raise FileExistsError(f'Output already exists: {job.output}')
        if (directory / '.chd_discs' / job.output.name).exists():
            raise FileExistsError(f'An organized disc already exists: {job.output.name}')
        staging = job.local and hardware.fs_type(job.game.source) in NETWORK
        work = self.scratch if staging or kind in NETWORK else child_dir(directory, Path('.couchchd_tmp'))
        return work, staging

    def _prepare(self, job, work, stage, target, threads):
        context = cloud_ingest.stage(job.game, work, cancel=self.cancel) if stage else nullcontext(job.game)
        with context as game:
            converter.convert(game, target, self.tool, cancel=self.cancel, threads=threads)
        job.game.assert_unchanged()

    def _publish(self, job, prepared):
        if self.cancel.is_set():
            raise InterruptedError('Batch cancelled; input retained.')
        job.game.assert_unchanged()
        size = prepared.stat().st_size
        self.db.publish(self.batch, job.output, lambda stage: publish_prepared(prepared, stage))
        self.outputs.append(job.output)
        # Count distinct input files: shared tracks do not represent duplicate savings.
        new_bytes = sum(p.stat().st_size for p in job.game.files if p not in self.sources)
        self.sources.update(job.game.files)
        self.saved += new_bytes - size
        self.progress(f'Verified: {job.output.name} (compression delta {new_bytes - size} bytes)')
        if self.quarantine and job.local:
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

    def _error(self, exc, name):
        if ((isinstance(exc, OSError) and exc.errno == errno.ENOSPC)
                or getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL):
            self.paused = 'Storage full. Free space, then resume the batch.'
            self.cancel.set()  # running converters observe this and terminate their children
        elif isinstance(exc, InterruptedError) and self.cancel.is_set() and not self.paused:
            self.paused = 'Cancelled. Remaining inputs were not moved.'
        message = f'{name}: {exc}'
        self.errors.append(message)
        self.progress(message)

    def process(self, jobs):
        if not jobs:
            return
        total = sum(j.game.size for j in jobs)
        if hardware.free_bytes(self.root) < total * 0.6:
            jobs = sorted(jobs, key=lambda j: j.game.size)
        pending = deque(jobs)
        reserved = Counter()
        active = {}
        previous_reason = ''
        # Split CPU only when independent games exist. Never dedicate dozens of
        # parallel random writers to a single SD card even on a many-core host.
        width = min(len(jobs), self.cores, 4)
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
                    cap = min(width, self.guard.limit())
                    while (pending and len(active) < cap and not self.cancel.is_set()
                           and not self.paused and not reason):
                        job = pending[0]
                        try:
                            work, stage = self._location(job)
                            # Group reservations by device; NAS output and local scratch
                            # may need simultaneous headroom, not just the final CHD.
                            estimate = (job.game.size * 6 + 9) // 10 + GIB
                            needs = {}
                            volumes = [(job.output.parent, estimate), (work, estimate + (job.game.size if stage else 0))]
                            if self.quarantine and job.local and job.game.source.stat().st_dev != self.root_identity[0]:
                                volumes.append((self.root, job.game.size + GIB))
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
                            target = work / (uuid.uuid4().hex + '.chd.part')
                            self.progress(f'Compressing: {job.game.source.name}')
                            dest_identity = fingerprint(job.output.parent)[:2]
                            future = pool.submit(self._prepare, job, work, stage, target, max(1, min(self.guard.limit(), self.cores) // cap))
                            active[future] = job, target, needs, dest_identity
                            for dev, (_, amount) in needs.items():
                                reserved[dev] += amount
                        except (OSError, RuntimeError, ValueError) as exc:
                            self._error(exc, job.game.source.name)
                        pending.popleft()
                    if not active:
                        if self.paused or self.cancel.is_set() or not pending:
                            break
                        self.cancel.wait(0.25)  # suspend/remount guard
                        continue
                    done, _ = wait(active, timeout=0.25, return_when=FIRST_COMPLETED)
                    for future in done:
                        job, target, needs, dest_identity = active.pop(future)
                        try:
                            future.result()
                            if (fingerprint(self.root)[:2] != self.root_identity
                                    or fingerprint(job.output.parent)[:2] != dest_identity):
                                raise OSError('Destination mount changed; prepared output was not published.')
                            self._publish(job, target)
                        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                            self._error(exc, job.game.source.name)
                        finally:
                            target.unlink(missing_ok=True)
                            for dev, (_, amount) in needs.items():
                                reserved[dev] -= amount
            finally:
                if active:
                    self.cancel.set()
                    wait(active)
                    for _, target, _, _ in active.values():
                        target.unlink(missing_ok=True)
        if self.cancel.is_set() and not self.paused:
            self.paused = 'Cancelled. Verified jobs were retained; remaining inputs were not moved.'
