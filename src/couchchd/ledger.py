"""Write-ahead filesystem journal. Call under the library's exclusive instance lock.

A file is never identified by its name alone. Every potentially destructive step
records the source identity and content before the filesystem change. Interrupted
batches remain available to Undo; recovery does not guess which files to remove.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Iterable

from .output_formats import can_replace_source, is_output


class SafetyError(ValueError):
    """Filesystem state doesn't match the journal; leave both copies alone."""


def _signature(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _mark(path: Path, *, sync: bool = False, expected=None) -> str:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise SafetyError(f"Not a regular file: {path}")
    sig = _signature(before)
    if expected is not None and sig != tuple(expected):
        raise SafetyError(f"Source changed since verification: {path}")
    with path.open('rb') as stream:
        if _signature(os.fstat(stream.fileno())) != sig:
            raise SafetyError(f"File changed while opening: {path}")
        if sync and os.name != 'nt':
            os.fsync(stream.fileno())
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if _signature(path.lstat()) != sig:
        raise SafetyError(f"File changed while hashing: {path}")
    # ctime is essential to change detection above, but renaming changes it.
    return json.dumps([*sig[:4], digest])


def _quick(path: Path, mark: str) -> bool:
    st = path.lstat()
    return stat.S_ISREG(st.st_mode) and list(_signature(st)[:4]) == json.loads(mark)[:4]


def _require(path: Path, mark: str) -> None:
    if not _quick(path, mark) or _mark(path) != mark:
        raise SafetyError(f"File no longer matches journal: {path}")


def _sync(directory: Path) -> None:
    if os.name != 'nt':
        fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def install_new(src: Path, dst: Path) -> None:
    """Atomic rename without replacement (also works on Linux FAT/exFAT)."""
    if os.name == 'nt':
        os.rename(src, dst)  # Windows refuses an occupied destination.
    else:
        missing = {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
            rc = libc.renamex_np(os.fsencode(src), os.fsencode(dst), 4)  # RENAME_EXCL
        elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
            rc = libc.renameat2(-100, os.fsencode(src), -100, os.fsencode(dst), 1)
        else:
            rc = None
        if rc is not None and rc != 0 and ctypes.get_errno() not in missing:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(dst))
        if rc != 0:
            # POSIX fallback; do not emulate no-replace using exists()+replace().
            os.link(src, dst, follow_symlinks=False)
            _sync(dst.parent)
            if not os.path.samestat(src.lstat(), dst.lstat()):
                raise SafetyError(f"Linked file changed during move: {src}")
            src.unlink()
    _sync(dst.parent)
    if src.parent != dst.parent:
        _sync(src.parent)


_SCHEMA = '''
CREATE TABLE IF NOT EXISTS batches (
 id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'open',
 started TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished TEXT
);
CREATE TABLE IF NOT EXISTS ops (
 id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id),
 kind TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL,
 srcmark TEXT NOT NULL, dstmark TEXT NOT NULL, revmark TEXT, dstctime INTEGER,
 state TEXT NOT NULL DEFAULT 'intent'
);
CREATE INDEX IF NOT EXISTS ops_batch ON ops(batch,id);
CREATE INDEX IF NOT EXISTS ops_destination ON ops(dst,id);
CREATE TABLE IF NOT EXISTS proofs (
 backup INTEGER NOT NULL REFERENCES ops(id), output INTEGER NOT NULL REFERENCES ops(id),
 PRIMARY KEY(backup,output)
);
'''


class Ledger:
    def __init__(self, root: Path):
        self.root = Path(root).resolve(strict=True)

    def __enter__(self) -> Ledger:
        name = self.root / 'couchchd_history.db'
        for suffix in ('', '-wal', '-shm', '-journal'):
            p = Path(str(name) + suffix)
            if os.path.lexists(p) and (not stat.S_ISREG(p.lstat().st_mode) or p.stat().st_nlink != 1):
                raise SafetyError(f"Unsafe history database: {p}")
        self.db = sqlite3.connect(name, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript(_SCHEMA)
            # A scoped purge can commit while a batch is open. If power failed
            # before finish, allow a later explicit global purge to resume only
            # when there are no ambiguous filesystem intents left.
            self.db.execute("""UPDATE batches SET status='partial_purge' WHERE status='open'
                AND id IN (SELECT batch FROM ops WHERE state IN ('purging','purged'))
                AND id NOT IN (SELECT batch FROM ops WHERE state='intent')""")
            self.db.execute("UPDATE batches SET status='interrupted' WHERE status='open'")
        except BaseException:
            self.db.close()
            raise
        return self

    def __exit__(self, *exc):
        self.db.close()

    def _path(self, path: Path | str) -> Path:
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        parts = path.relative_to(self.root).parts
        if not parts or '..' in parts or parts[0].casefold().startswith('couchchd_history.db') or parts[0].casefold() == 'couchchd.lock':
            raise SafetyError(f"Unsafe journal path: {path}")
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise SafetyError(f"Refusing symbolic link: {current}")
        return path

    def _library(self, path: Path) -> Path:
        p = self._path(path)
        if any(x.casefold() in {'_couchchd_backup', '.couchchd_tmp'} for x in p.relative_to(self.root).parts):
            raise SafetyError(f"Reserved library location: {p}")
        return p

    def _mkdir(self, path: Path) -> None:
        if path == self.root:
            return
        self._path(path)
        if not path.is_dir():
            self._mkdir(path.parent)
            path.mkdir(exist_ok=True)
            _sync(path.parent)

    def _stage(self, dst: Path) -> Path:
        base = dst.parent / '.couchchd_tmp'
        self._mkdir(base)
        directory = Path(tempfile.mkdtemp(dir=base, prefix='journal-'))
        _sync(base)
        return directory / dst.name

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def begin(self) -> int:
        return self.db.execute('INSERT INTO batches DEFAULT VALUES').lastrowid

    def _open(self, batch: int) -> None:
        if not self.db.execute("SELECT 1 FROM batches WHERE id=? AND status='open'", (batch,)).fetchone():
            raise SafetyError(f"Batch {batch} is not open")

    def finish(self, batch: int) -> None:
        self._open(batch)
        if self.db.execute("SELECT 1 FROM ops WHERE batch=? AND state NOT IN ('done','undone','purged')", (batch,)).fetchone():
            raise SafetyError(f"Batch {batch} contains unfinished operations")
        status = 'finished'
        if self._irreversible(batch):
            remaining = self.db.execute("SELECT 1 FROM ops WHERE batch=? AND kind='quarantine' AND state='done'", (batch,)).fetchone()
            status = 'partial_purge' if remaining else 'purged'
        self.db.execute("UPDATE batches SET status=?, finished=CURRENT_TIMESTAMP WHERE id=?", (status, batch))

    def _irreversible(self, batch: int, after: int = 0) -> bool:
        return self.db.execute("SELECT 1 FROM ops WHERE batch=? AND id>? AND state IN ('purging','purged') LIMIT 1",
                               (batch, after)).fetchone() is not None

    def history(self) -> list[dict]:
        return [dict(r) for r in self.db.execute('''SELECT b.*, count(o.id) AS operations
            FROM batches b LEFT JOIN ops o ON b.id=o.batch GROUP BY b.id ORDER BY b.id DESC''')]

    def _set_mark(self, op: int, reverse: bool, mark: str) -> None:
        col = 'revmark' if reverse else 'dstmark'
        self.db.execute(f'UPDATE ops SET {col}=? WHERE id=?', (mark, op))

    def _transfer(self, op: int, src: Path, dst: Path, mark: str, *, reverse=False, expected=None) -> str:
        """Intent and predicted inode must be durable before the new name appears."""
        self._mkdir(dst.parent)
        if os.path.lexists(dst):
            raise FileExistsError(dst)
        sig = _signature(src.lstat())
        if not _quick(src, mark) or (expected is not None and sig != tuple(expected)):
            raise SafetyError(f"Source changed before move: {src}")
        self._set_mark(op, reverse, mark)
        try:
            install_new(src, dst)
            return mark
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
        # Cross-device transfer: never expose an unverified partial destination.
        stage = self._stage(dst)
        owned = None
        try:
            with src.open('rb') as inp, stage.open('xb', buffering=0) as out:
                try:
                    shutil.copyfileobj(inp, out, 1024 * 1024)
                    shutil.copystat(src, stage)
                    os.fsync(out.fileno())
                finally:
                    owned = _signature(os.fstat(out.fileno()))
            new = _mark(stage, expected=owned)
            if json.loads(new)[-1] != json.loads(mark)[-1] or _signature(src.lstat()) != sig:
                raise SafetyError(f"Cross-device copy did not verify: {src}")
            self._set_mark(op, reverse, new)
            install_new(stage, dst)
            if _signature(src.lstat()) != sig or not _quick(dst, new):
                raise SafetyError(f"Files changed before source removal: {src}")
            src.unlink()
            _sync(src.parent)
            return new
        finally:
            # Reclaim our failed private copy (including ENOSPC) but never an
            # unrelated replacement at the same name.
            if owned is not None and os.path.lexists(stage) and _signature(stage.lstat()) == owned:
                stage.unlink()
            self._tidy(stage)

    @staticmethod
    def _tidy(stage: Path) -> None:
        # Only remove the private, now-empty staging directory, never a tree.
        try:
            stage.parent.rmdir()
        except OSError:
            pass

    def _record(self, batch: int, kind: str, src: Path, dst: Path, *, expected=None, proofs=(), required_mark=None) -> int:
        self._open(batch)
        if os.path.lexists(dst):
            raise FileExistsError(dst)
        if expected is None:
            expected = _signature(src.lstat())
        mark = _mark(src, sync=True, expected=expected)
        if required_mark is not None and mark != required_mark:
            raise SafetyError(f'File no longer matches the verified publication: {src}')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            op = self.db.execute('INSERT INTO ops(batch,kind,src,dst,srcmark,dstmark) VALUES(?,?,?,?,?,?)',
                                 (batch, kind, self._rel(src), self._rel(dst), mark, mark)).lastrowid
            self.db.executemany('INSERT INTO proofs VALUES(?,?)', ((op, proof) for proof in proofs))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        moved = self._transfer(op, src, dst, mark, expected=expected)
        if not _quick(dst, moved):
            raise SafetyError(f"Destination changed while moving: {dst}")
        self.db.execute("UPDATE ops SET state='done',dstctime=? WHERE id=?", (dst.lstat().st_ctime_ns, op))
        return op

    def publish(self, batch: int, path: Path, writer: Callable[[Path], object]) -> Path:
        """writer must produce a verified file at a private, initially absent path."""
        self._open(batch)
        dst = self._library(path)
        if os.path.lexists(dst):
            raise FileExistsError(dst)
        stage = self._stage(dst)
        try:
            writer(stage)
            self._path(stage)
            if not stage.is_file() or stage.stat().st_size == 0:
                raise SafetyError(f"Writer produced no output: {dst}")
            self._record(batch, 'publish', stage, dst)
            return dst
        finally:
            self._tidy(stage)

    def move(self, batch: int, src: Path, dst: Path) -> None:
        src = self._library(src)
        row = self.db.execute("SELECT dstmark FROM ops WHERE dst=? AND state='done' ORDER BY id DESC LIMIT 1",
                              (self._rel(src),)).fetchone()
        if is_output(src) and row is None:
            raise SafetyError(f'Untracked game output: {src}')
        self._record(batch, 'move', src, self._library(dst), required_mark=row[0] if row else None)

    def save_metadata(self, batch: int, src: Path, *, expected) -> None:
        src = self._library(src)
        dst = self._path(self.root / '_couchchd_backup' / '.metadata' / str(batch) / str(self.checkpoint(batch)) / self._rel(src))
        self._record(batch, 'metadata', src, dst, expected=expected)

    def quarantine(self, batch: int, path: Path, *, outputs: Iterable[Path] | None = None, expected=None) -> Path:
        src = self._library(path)
        query = "SELECT id,dst,dstmark,dstctime FROM ops WHERE batch=? AND kind='publish' AND state='done'"
        if outputs is None:
            rows = [r for r in self.db.execute(query, (batch,)) if can_replace_source(Path(r['dst']))]
        else:
            rows = []
            for p in set(outputs):
                p = self._library(p)
                row = self.db.execute(query + ' AND dst=?', (batch, self._rel(p))).fetchone()
                if row is None or not can_replace_source(p):
                    raise SafetyError(f'No verified output in this batch: {p}')
                rows.append(row)
        if not rows:
            raise SafetyError('Cannot quarantine without a verified output from this batch')
        for row in rows:
            p = self._path(row['dst'])
            if not _quick(p, row['dstmark']) or p.lstat().st_ctime_ns != row['dstctime']:
                raise SafetyError(f'Verified output changed before quarantine: {p}')
        dst = self._path(self.root / '_couchchd_backup' / self._rel(src))
        if os.path.lexists(dst):
            dst = self._path(self.root / '_couchchd_backup' / '.batches' / str(batch) / self._rel(src))
        self._record(batch, 'quarantine', src, dst, expected=expected, proofs=[r['id'] for r in rows])
        return dst

    def checkpoint(self, batch: int) -> int:
        return self.db.execute('SELECT coalesce(max(id),0) FROM ops WHERE batch=?', (batch,)).fetchone()[0]

    def _reverse(self, row) -> None:
        op, kind = row['id'], row['kind']
        src, dst = self._path(row['src']), self._path(row['dst'])
        generated = kind == 'publish'
        keep = generated and is_output(dst)
        if keep:
            src = self._path(self.root / '_couchchd_backup' / '.undo' / str(row['batch']) / self._rel(dst))
        elif generated:
            # Unrelated replacements never satisfy both the fingerprint and identity.
            if os.path.lexists(dst):
                _require(dst, row['dstmark'])
                dst.unlink()
                _sync(dst.parent)
            self.db.execute("UPDATE ops SET state='undone' WHERE id=?", (op,))
            return
        restored = row['revmark'] or row['srcmark']
        if os.path.lexists(src):
            if keep and row['revmark'] is None:
                raise FileExistsError(src)
            _require(src, restored)
            if os.path.lexists(dst):
                # Crash between the verified copy and source unlink, in either direction.
                _require(dst, row['dstmark'])
                dst.unlink()
                _sync(dst.parent)
        elif os.path.lexists(dst):
            _require(dst, row['dstmark'])
            restored = self._transfer(op, dst, src, row['dstmark'], reverse=True)
        elif not generated:
            raise SafetyError(f"Both journaled locations are missing: {src}, {dst}")
        self.db.execute('BEGIN IMMEDIATE')
        try:
            # A reverse copy across devices necessarily creates a new inode.
            if not generated:
                self.db.execute("UPDATE ops SET dstmark=?,dstctime=? WHERE id<? AND dst=? AND dstmark=? AND state='done'",
                                (restored, src.lstat().st_ctime_ns, op, row['src'], row['srcmark']))
            self.db.execute("UPDATE ops SET state='undone' WHERE id=?", (op,))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def rollback_since(self, batch: int, checkpoint: int) -> int:
        if self._irreversible(batch, checkpoint):
            raise SafetyError('Cannot roll back operations after backups were purged')
        rows = list(self.db.execute("SELECT * FROM ops WHERE batch=? AND id>? AND state IN ('done','intent') ORDER BY id DESC", (batch, checkpoint)))
        for row in rows:
            # Earlier fingerprints can be updated by a cross-device restoration.
            self._reverse(self.db.execute('SELECT * FROM ops WHERE id=?', (row['id'],)).fetchone())
        return len(rows)

    def undo(self) -> int:
        row = self.db.execute("SELECT id,status FROM batches WHERE status!='undone' ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return 0
        if row['status'] in {'purging', 'purged', 'partial_purge'} or self._irreversible(row['id']):
            raise SafetyError('Cannot undo past an explicitly purged batch')
        self.db.execute("UPDATE batches SET status='undoing' WHERE id=?", (row['id'],))
        count = self.rollback_since(row['id'], 0)
        self.db.execute("UPDATE batches SET status='undone' WHERE id=?", (row['id'],))
        return count

    def _publications(self) -> dict[str, tuple[int, str, int]]:
        """Follow verified outputs through relocations, including later batches."""
        live = {}
        for row in self.db.execute("SELECT * FROM ops WHERE state='done' AND kind IN ('publish','move') ORDER BY id"):
            if row['kind'] == 'publish' and is_output(Path(row['dst'])):
                live[row['dst']] = row['id'], row['dstmark'], row['dstctime']
            elif row['kind'] == 'move':
                prior = live.pop(row['src'], None)
                if prior and prior[1] == row['srcmark']:
                    live[row['dst']] = prior[0], row['dstmark'], row['dstctime']
        return live

    def verified(self) -> list[Path]:
        found = []
        for name, (_, mark, ctime) in self._publications().items():
            try:
                path = self._path(name)
                if _quick(path, mark) and path.lstat().st_ctime_ns == ctime:
                    found.append(path)
            except (OSError, SafetyError):
                continue
        return found

    def _purge_plan(self, rows):
        """Authenticate every requested backup and proof before the first unlink."""
        live = {key: (name, mark) for name, (key, mark, _) in self._publications().items()}
        # Cache large output hashes shared by several quarantined tracks.
        authenticated = {}
        backups = []
        for row in rows:
            keys = [p[0] for p in self.db.execute('SELECT output FROM proofs WHERE backup=?', (row['id'],))]
            if not keys or not all(key in live for key in keys):
                raise SafetyError('No live verified output for this backup')
            for key in keys:
                if key not in authenticated:
                    name, mark = live[key]
                    path = self._path(name)
                    sig = _signature(path.lstat())
                    _require(path, mark)
                    authenticated[key] = path, sig
            backup = self._path(row['dst'])
            if backup.relative_to(self.root).parts[0] != '_couchchd_backup':
                raise SafetyError(f"Not a backup: {backup}")
            sig = None
            if row['state'] != 'purging' or os.path.lexists(backup):
                sig = _signature(backup.lstat())
                _require(backup, row['dstmark'])
            backups.append((row['id'], backup, sig, [authenticated[k] for k in keys]))
        return backups

    def _apply_purge(self, backups) -> int:
        removed = 0
        for op, backup, sig, proofs in backups:
            if sig is not None:
                for path, check in proofs:
                    if _signature(self._path(path).lstat()) != check:
                        raise SafetyError(f"Output changed before purge: {path}")
                if _signature(self._path(backup).lstat()) != sig:
                    raise SafetyError(f"Backup changed before purge: {backup}")
                self.db.execute("UPDATE ops SET state='purging' WHERE id=?", (op,))
                backup.unlink()
                _sync(backup.parent)
                removed += 1
            self.db.execute("UPDATE ops SET state='purged' WHERE id=?", (op,))
        return removed

    def purge_job(self, batch: int, paths: list[Path]) -> int:
        """Confirmed immediate cleanup: only this job's named quarantine records.

        A saved descriptor may name tracks anywhere beneath its original folder.
        Keep potentially shared tracks in quarantine when a descriptor outside the
        request could need them. Global purge can later check and retire both.
        """
        self._open(batch)
        selected = {}
        for path in paths:
            name = self._rel(self._path(path))
            row = self.db.execute("""SELECT * FROM ops WHERE batch=? AND dst=? AND kind='quarantine'
                                    AND state IN ('done','purging','purged') ORDER BY id DESC LIMIT 1""",
                                  (batch, name)).fetchone()
            if row is None:
                raise SafetyError(f'Not a backup for this open batch: {path}')
            selected[row['id']] = row
        if not selected:
            return 0
        def folder_key(path):
            return unicodedata.normalize('NFC', path.as_posix()).casefold()

        possible_owners = set()
        for row in self.db.execute("SELECT id,src FROM ops WHERE kind='quarantine' AND state IN ('intent','done','purging')"):
            src = Path(row['src'])
            if row['id'] not in selected and src.suffix.casefold() in {'.cue', '.gdi'}:
                possible_owners.add(folder_key(src.parent))

        def eligible(row):
            src = Path(row['src'])
            return (row['state'] != 'purged' and
                    (src.suffix.casefold() not in {'.bin', '.img', '.raw'} or
                     not any(folder_key(parent) in possible_owners for parent in src.parents)))

        # Descriptor cleanup comes first: if unlink fails, we have not removed a
        # track needed by a surviving descriptor within this request, either.
        rows = sorted(filter(eligible, selected.values()),
                      key=lambda r: Path(r['src']).suffix.casefold() not in {'.cue', '.gdi'})
        return self._apply_purge(self._purge_plan(rows))

    def purge(self) -> int:
        """Check every batch before deleting anything: tracks can span batches."""
        pending = self.db.execute("""SELECT 1 FROM ops JOIN batches ON batch=batches.id
            WHERE kind='quarantine' AND state!='undone'
            AND status NOT IN ('finished','partial_purge','purging','purged')""").fetchone()
        if pending:
            raise SafetyError('Resolve interrupted backup batches before purging')
        rows = self.db.execute("""SELECT ops.* FROM ops JOIN batches ON batch=batches.id
            WHERE kind='quarantine' AND state IN ('done','purging')
            AND status IN ('finished','partial_purge','purging') ORDER BY ops.id""")
        backups = self._purge_plan(rows)
        # One durable barrier prevents Undo even after a crash midway through unlink.
        self.db.execute("""UPDATE batches SET status='purging' WHERE status='partial_purge'
            OR (status='finished' AND id IN (SELECT batch FROM ops WHERE kind='quarantine' AND state='done'))""")
        removed = self._apply_purge(backups)
        self.db.execute("UPDATE batches SET status='purged' WHERE status='purging'")
        return removed
