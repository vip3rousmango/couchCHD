"""Player attestations and confirmed, scoped cleanup. All calls require the library lock."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import unicodedata

from . import multidisc, scanner
from .ledger import Ledger, SafetyError, _require, _signature, _track_names
from .output_formats import can_replace_source, is_output


_DESCRIPTORS = {'.cue', '.gdi'}
_TRACKS = {'.bin', '.img', '.raw'}
_PLAYLISTS = {'.m3u', '.m3u8'}
_SIDECARS = _DESCRIPTORS | _PLAYLISTS


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()


def _key(path: Path) -> str:
    return unicodedata.normalize('NFC', os.path.normpath(path)).casefold()


@dataclass(frozen=True)
class _File:
    path: Path
    mark: str
    sig: tuple[int, ...]

    def identity(self):
        return str(self.path), self.mark, self.sig


def _live(db, name, mark, ctime):
    try:
        path = db._path(name)
        st = path.lstat()
        sig = _signature(st)
        if stat.S_ISREG(st.st_mode) and sig[-1] == ctime and list(sig[:4]) == json.loads(mark)[:4]:
            return _File(path, mark, sig)
    except (ValueError, OSError):
        pass
    return None


def _outputs(db):
    return {op: file for name, (op, mark, ctime) in db._publications().items()
            if is_output(Path(name)) and (file := _live(db, name, mark, ctime)) is not None}


def _tested(db):
    return {r[0] for r in db.db.execute('SELECT output FROM play_tests')}


def _id(op, file):
    # A stale picker must not mark a new publication at the same path. Do not
    # expose SQLite rowids as cross-library identifiers.
    return _digest((op, file.identity()))


def review(db: Ledger) -> list[dict]:
    tested = _tested(db)
    return [{'id': _id(op, file), 'path': str(file.path), 'tested': op in tested}
            for op, file in sorted(_outputs(db).items(), key=lambda item: str(item[1].path))]


def set_play_tested(db: Ledger, id: str, tested: bool) -> None:
    if not isinstance(tested, bool):
        raise ValueError('Choose tested or not tested.')
    for op, file in _outputs(db).items():
        if _id(op, file) == id:
            if tested:
                _require(file.path, file.mark)
                if _signature(db._library(file.path).lstat()) != file.sig:
                    raise SafetyError('Output changed while recording play test.')
                db.db.execute('INSERT OR IGNORE INTO play_tests VALUES (?)', (op,))
            else:
                db.db.execute('DELETE FROM play_tests WHERE output=?', (op,))
            return
    raise SafetyError('Selected output is no longer verified. Refresh the review list.')


def _sidecars(root):
    def fail(exc):
        raise exc

    for folder, dirs, names in os.walk(root, onerror=fail):
        dirs[:] = [d for d in dirs if d.casefold() not in {'_couchchd_backup', '.couchchd_tmp'}]
        for name in names:
            path = Path(folder) / name
            if path.suffix.casefold() in _SIDECARS:
                yield path


def _members(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_size > 1024 * 1024:
        raise SafetyError(f'Cannot safely check playlist: {path}')
    with path.open('rb') as f:
        data = f.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024 or _signature(path.lstat()) != _signature(st):
        raise SafetyError(f'Playlist changed during review: {path}')
    members = set()
    for line in data.decode('utf-8-sig').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            members.add(_key(path.parent / line.replace('\\', '/')))
    sig = _signature(st)
    mark = json.dumps([*sig[:4], hashlib.sha256(data).hexdigest()])
    return members, _File(path, mark, sig)


def _playlist_proofs(db, live, tested, lists):
    """A tested disc alone is insufficient for reclaiming a set's originals."""
    by_name = defaultdict(set)
    for op, file in live.items():
        by_name[_key(file.path)].add(op)
    valid = tested & live.keys()
    # Organization can have happened in a later batch. If the managed playlist
    # is missing, replaced, or no longer names a hidden disc, preserve originals.
    journals = {_key(db.root / r['dst']): r for r in db.db.execute(
        "SELECT dst,dstmark,dstctime FROM ops WHERE kind='publish' AND state='done' ORDER BY id")
        if Path(r['dst']).suffix.casefold() in _PLAYLISTS}
    deps = defaultdict(set)
    groups = []
    referenced = set()
    for path, (members, playlist) in lists.items():
        referenced.update(members)
        ids = set().union(*(by_name.get(m, ()) for m in members))
        if not ids:
            continue
        groups.append(ids)
        if any(len(by_name.get(m, ())) != 1 for m in members):
            valid.difference_update(ids)
        r = journals.get(_key(path))
        if r is not None and (playlist.mark != r['dstmark'] or playlist.sig[-1] != r['dstctime']):
            valid.difference_update(ids)
        for op in ids:
            deps[op].add(playlist)
    for op in tuple(valid):
        path = live[op].path
        if path.parent.name == '.chd_discs':
            disc = multidisc._disc(path.stem)
            playlist = path.parent.parent / (disc[0] + '.m3u') if disc else None
            if (playlist not in lists or _key(playlist) not in journals
                    or _key(path) not in lists[playlist][0]):
                valid.remove(op)
    # Custom playlists can overlap; require the entire connected set.
    components = {}
    for group in groups:
        component = set(group)
        for op in group:
            component.update(components.get(op, ()))
        for op in component:
            components[op] = component
    for group in {frozenset(g) for g in components.values()}:
        if not group <= valid:
            valid.difference_update(group)
        else:
            checks = {live[op] for op in group}.union(*(deps[op] for op in group))
            for op in group:
                deps[op] = checks
    return valid, deps, referenced


def _primary(db, row, proof_names):
    """Check current source policy too, not just historical quarantine permission."""
    src = db.root / row['src']
    system = scanner.library_system(db.root, src)
    if (system in {'xbox', '3ds'} or src.suffix.casefold() in {'.zip', '.7z'}
            or not scanner.is_input_name(src, system)):
        return False
    folder = scanner.system_folder(db.root, src)
    modes = [scanner.default_mode(system, src.suffix.lower())]
    if system == 'psp':
        modes.append('cso')
    names = {scanner.output_path(src, mode).name for mode in modes}
    return any(p.parent in (folder, src.parent) and p.name in names for p in proof_names)


def _plan(db):
    # A partial undo / unfinished conversion may have incomplete descriptor or
    # playlist ownership records. Do not guess at the surviving relationships.
    if db.db.execute("SELECT 1 FROM batches WHERE status NOT IN ('finished','purged','partial_purge','undone') LIMIT 1").fetchone():
        raise SafetyError('Resolve interrupted batches before reclaiming reviewed originals.')
    live = _outputs(db)
    tested = _tested(db)
    lists, active_sheets = {}, []
    for path in _sidecars(db.root):
        if path.suffix.casefold() in _PLAYLISTS:
            lists[path] = _members(path)
        else:
            active_sheets.append(path)
    valid, deps, referenced = _playlist_proofs(db, live, tested, lists)
    proofs = defaultdict(set)
    published = {}
    for r in db.db.execute('SELECT backup,output,dst FROM proofs JOIN ops ON output=ops.id'):
        proofs[r['backup']].add(r['output'])
        published[r['output']] = db.root / r['dst']
    selected, backups = {}, {}
    candidates, sheets = [], []
    for row in db.db.execute("SELECT * FROM ops WHERE kind='quarantine' AND state IN ('done','purging','purged') ORDER BY id"):
        op, keys = row['id'], proofs[row['id']]
        src = db._library(Path(row['src']))
        ext = src.suffix.casefold()
        if ext in _DESCRIPTORS:
            sheets.append((row, src))
        if row['state'] == 'purged' or not keys or not keys <= valid or _key(src) in referenced or os.path.lexists(src):
            continue
        if any(not can_replace_source(published[k]) or not can_replace_source(live[k].path) for k in keys):
            continue
        file = _live(db, row['dst'], row['dstmark'], row['dstctime'])
        base = db.root / '_couchchd_backup'
        if (file is None or file.path not in (base / row['src'], base / '.batches' / str(row['batch']) / row['src'])
                or file.path.lstat().st_nlink != 1 or _key(file.path) in referenced):
            continue
        backups[op] = file
        primary = _primary(db, row, (published[k] for k in keys))
        if ext in _TRACKS:
            candidates.append((row, src, primary))
        elif primary:
            selected[op] = row

    if candidates:
        # The job which last moved a shared track need not be its only owner.
        # Consult live AND saved descriptors, including other batches. A missing,
        # unreadable or changed saved descriptor protects its whole subtree.
        protected, unknown = set(), set()
        owners = defaultdict(set)
        retired = {r['backup']: json.loads(r['tracks']) for r in db.db.execute('SELECT * FROM retired_sheets')}
        for path in active_sheets:
            names = _track_names(path)
            if names:
                protected.update(_key(path.parent / name) for name in names)
            else:
                unknown.add(_key(path.parent))
        for row, src in sheets:
            op = row['id']
            keys = proofs[op]
            if row['state'] == 'purged':
                names = retired.get(op, ())
                eligible = (keys and keys <= valid and _primary(db, row, (published[k] for k in keys))
                            and all(can_replace_source(live[k].path) and can_replace_source(published[k]) for k in keys))
            else:
                file = backups.get(op) or _live(db, row['dst'], row['dstmark'], row['dstctime'])
                names = _track_names(file.path) if file else ()
                eligible = op in selected
            if not names:
                unknown.add(_key(src.parent))
            elif not eligible:
                protected.update(_key(src.parent / name) for name in names)
            else:
                for name in names:
                    owners[_key(src.parent / name)].update(keys)
        for row, src, primary in candidates:
            name = _key(src)
            if (name not in protected and not any(_key(p) in unknown for p in src.parents)
                    and (primary or proofs[row['id']] & owners[name])):
                selected[row['id']] = row
                proofs[row['id']].update(owners[name])

    # Delete descriptors first. If interrupted, a surviving descriptor never
    # loses its track; unconfirmed/residual originals stay in quarantine.
    rows = sorted(selected.values(), key=lambda r: (Path(r['src']).suffix.casefold() not in _DESCRIPTORS, r['id']))
    guards = {r['id']: set().union(*(deps[k] | {live[k]} for k in proofs[r['id']])) for r in rows}
    scope = [(r['id'], r['batch'], backups[r['id']].identity(), sorted(proofs[r['id']]),
              sorted(f.identity() for f in guards[r['id']])) for r in rows]
    summary = dict(token=_digest((str(db.root), scope)), count=len(rows),
                   bytes=sum(backups[r['id']].sig[2] for r in rows),
                   files=[str(backups[r['id']].path) for r in rows], batches=sorted({r['batch'] for r in rows}))
    return summary, rows, guards


def reclaim_preview(db: Ledger) -> dict:
    return _plan(db)[0]


def reclaim_reviewed(db: Ledger, token: str) -> int:
    summary, rows, guards = _plan(db)
    if token != summary['token']:
        raise SafetyError('Cleanup preview changed. Review the current originals and confirm again.')
    # Hash every selected original and all its tested outputs before deleting
    # the first file. Also authenticate playlist/sibling proofs, including discs
    # whose originals were never quarantined on this device.
    plan = db._purge_plan(rows)
    checked = {path: sig for _, _, _, direct in plan for path, sig in direct}
    for op, _, _, direct in plan:
        for file in guards[op]:
            if file.path not in checked:
                _require(db._path(file.path), file.mark)
                checked[file.path] = _signature(file.path.lstat())
            if checked[file.path] != file.sig:
                raise SafetyError(f'Reviewed output or playlist changed: {file.path}')
        direct[:] = [(f.path, f.sig) for f in guards[op]]
    # Long hashes leave time for a new playlist or a shared descriptor to
    # appear. Recheck ownership and the exact scope immediately before unlink.
    if plan and token != reclaim_preview(db)['token']:
        raise SafetyError('Library changed during authentication. Preview the cleanup again.')
    try:
        return db._apply_purge(plan)
    finally:
        db._finish_reclaim()
