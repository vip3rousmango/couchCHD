"""Read-only reports for a single library. A plan is a suggestion, not purge authority."""
from __future__ import annotations

import stat
import tempfile
import unicodedata
from collections import Counter, defaultdict
from functools import cache
from itertools import chain
from pathlib import Path

from . import cso, hardware, multidisc, profiles, scanner
from .batch import GIB, space_requirements
from .cloud_ingest import NETWORK
from .output_formats import can_replace_source


def _key(path):
    return unicodedata.normalize('NFC', str(path)).casefold()


def _local(root, path):
    """Reject links (including parents), escapes and private application paths."""
    relative = path.relative_to(root)
    if '..' in relative.parts or any(not scanner.visible_directory(p) and p != '.chd_discs'
                                     for p in relative.parts[:-1]):
        raise ValueError(f'Unsafe library path: {path}')
    if path.resolve(strict=False) != path:
        raise ValueError(f'Linked library path: {path}')
    return path


def _playlists(root, scan):
    members = {}
    findings = []
    for value in scan['playlists']:
        path = Path(value)
        try:
            _local(root, path)
            if not path.is_file():
                raise ValueError('Playlist is missing or not a regular file.')
            with path.open('rb') as f:
                data = f.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024 or b'\0' in data:
                raise ValueError('Playlist exceeds 1 MiB or contains binary data.')
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = data.decode('cp1252')
            lines = [s for line in text.splitlines() if (s := line.strip()) and not s.startswith('#')]
            if not lines:
                raise ValueError('Playlist has no disc entries.')
        except (OSError, ValueError, RuntimeError) as exc:
            findings.append((path, 'playlist', str(exc), 'Restore a readable text playlist with one relative disc path per line.'))
            continue
        for line in dict.fromkeys(lines):
            try:
                relative = scanner.safe_relative_name(line, descriptor=True)
                member = _local(root, path.parent / relative)
                members.setdefault(_key(member), path)
                if not member.is_file():
                    findings.append((path, 'playlist_member', f'Playlist member not found: {line}',
                                     'Restore that disc or correct its relative path and letter case in the playlist.'))
            except (OSError, ValueError, RuntimeError) as exc:
                findings.append((path, 'unsafe_playlist_member', str(exc),
                                 'Replace this entry with a safe, relative path to the disc inside your library.'))
    return members, findings


def _owners(root, scan):
    owners = {}

    def record(track, descriptor):
        key = _key(track)
        if key not in owners:
            owners[key] = (track, set())
        owners[key][1].add(descriptor)

    for game in scan['games']:
        for track in game['tracks']:
            record(Path(track), Path(game['path']))
    # A CUE missing disc audio can still reference another game's live track.
    for issue in scan['issues']:
        path = Path(issue['path'])
        if path.suffix.lower() in {'.cue', '.gdi'}:
            try:
                for ref in scanner.references(_local(root, path)):
                    record(path.parent / ref, path)
            except (OSError, ValueError, RuntimeError):
                pass  # The scan issue itself is included in the report.
    return owners


def health(root, scan):
    """Explain problems without opening the ledger, organizing discs or editing playlists."""
    root = Path(root).resolve(strict=True)
    issues = []

    def visible_path(path):
        # Organized discs are intentionally hidden from *input* discovery, but
        # belong to the same frontend system as visible discs and playlists.
        if path.parent.name == '.chd_discs':
            return path.parent.parent / path.name
        return path

    def add(path, category, reason, action):
        issues.append(dict(path=str(path), system=scanner.library_system(root, visible_path(path)), category=category,
                           reason=reason, action=action))

    for issue in scan['issues']:
        path = Path(issue['path'])
        category, reason = issue['category'], issue['reason']
        if category == 'active_transfer':
            action = 'Let the transfer finish, wait at least 10 seconds, then scan again.'
        elif path.suffix.casefold() == '.zip':
            action = ('Keep the archive. Check that it opens and contains one supported disc with all its tracks; '
                      'restore a complete copy or prepare that disc in a separate folder and rescan.')
        elif category == 'needs_unpacking':
            action = 'Unpack into a separate game folder, keeping all tracks together, then rescan.'
        elif category == 'linked_folder':
            action = 'Select the real system folder as your library instead of its link.'
        elif path.suffix.lower() in {'.cue', '.gdi'}:
            category = 'missing_track' if 'missing track path:' in reason.casefold() else 'broken_descriptor'
            action = ('Restore the named track beside the disc sheet, or correct its FILE path, then rescan.'
                      if category == 'missing_track' else
                      'Check the disc sheet and track names against the original dump; restore a matching set and rescan.')
        else:
            action = 'Check the source is a complete supported dump in the correct system folder, then rescan.'
        add(path, category, reason, action)

    _, findings = _playlists(root, scan)
    for finding in findings:
        add(*finding)
    for track, users in _owners(root, scan).values():
        if len(users) > 1:
            names = ', '.join(str(p.relative_to(root)) for p in sorted(users))
            add(track, 'shared_track', f'This track is referenced by: {names}.',
                'Keep this track in place. Removing or renaming it could break more than one game.')

    groups = defaultdict(list)
    paths = {Path(g['path']) for g in scan['games']} | {Path(p) for p in scan['chds']}
    paths.update(Path(i['path']) for i in scan['issues'] if Path(i['path']).suffix.lower() in {'.cue', '.gdi', '.iso'})
    for path in sorted(paths):
        disc = multidisc._disc(path.stem)
        if disc:
            folder = scanner.system_folder(root, visible_path(path)) or path.parent
            title, number, total = disc
            groups[folder, _key(title)].append((path, title, number, total))
    for group in groups.values():
        path, title = group[0][:2]
        numbers = {d[2] for d in group}
        totals = {d[3] for d in group if d[3] is not None}
        missing = sorted(set(range(1, max(numbers | totals) + 1)) - numbers)
        if missing:
            missing_text = ', '.join(map(str, missing))
            reason = f'{title}: Disc {missing_text} is missing.' if len(missing) == 1 else f'{title}: Missing discs {missing_text}.'
            add(path, 'disc_gap', reason, 'Restore the missing discs of the same edition, or correct their disc numbers; rescan.')
        if 0 in numbers or len(totals) > 1 or totals and max(numbers) > min(totals):
            add(path, 'disc_number', f'{title}: disc numbers and declared totals disagree.',
                'Check the numbering and edition against your originals before grouping these discs.')
    issues.sort(key=lambda i: (_key(i['path']), i['path'], i['category'], i['reason']))
    return {'issues': issues, 'count': len(issues)}


def plan(root, scan, systems, psp_format, target_bytes):
    """Choose a stable, small-first prefix; never spend expected compression or purge gains."""
    root = Path(root).resolve(strict=True)
    if type(target_bytes) is not int or target_bytes < 0:
        raise ValueError('Space target must be a nonnegative number of bytes.')
    if psp_format not in {'chd', 'cso'}:
        raise ValueError('PSP output must be chd or cso.')
    systems = profiles.default_systems(psp_format) if systems is None else set(systems)
    free = int(scan['free'])
    root_device = root.stat().st_dev
    owners = defaultdict(set)
    unknown = ''
    try:
        # Match the runner's post-quarantine proof, including unreadable or unsupported
        # sheets outside the selected systems. Never count bytes the runner must retain.
        for descriptor, refs in scanner.live_track_references(root):
            for ref in refs:
                owners[ref].add(descriptor)
    except (OSError, ValueError, RuntimeError) as exc:
        unknown = f'Cannot establish exclusive track ownership. Repair disc sheets and rescan: {exc}'
    playlist_members, playlist_problems = _playlists(root, scan)
    unknown_playlist = next((path for path, category, *_ in playlist_problems if category != 'playlist_member'), None)
    games = scan['games']
    candidates, excluded = [], []
    destinations = Counter()

    def destination(path, system, archive_mode=None):
        folder = scanner.system_folder(root, path)
        if folder is None or not archive_mode and not scanner.is_input_name(path, system):
            return None
        mode = archive_mode or scanner.default_mode(system, path.suffix)
        if system == 'psp' and psp_format == 'cso':
            mode = 'cso'
        return folder / scanner.output_path(path, mode).name

    for row in chain(games, scan['issues']):
        path = Path(row['path'])
        output = destination(path, scanner.library_system(root, path), row.get('mode') if row.get('archive') else None)
        if output:
            destinations[_key(output)] += 1

    @cache
    def entries(folder):
        return {_key(p.name) for p in folder.iterdir()}

    @cache
    def fs_type(folder):
        return hardware.fs_type(folder)

    scratch_free = None
    for row in games:
        path, system, size = Path(row['path']), row['system'], row['size']
        if system not in systems:
            continue
        try:
            if row.get('archive'):
                raise ValueError('The original ZIP is always kept. Use a normal conversion with room for the output '
                                 'and any required private staging; it will not reclaim space.')
            output = destination(path, system)
            if (not output or row['mode'] != scanner.default_mode(system, path.suffix)
                    or system != scanner.library_system(root, path)):
                raise ValueError('Unsupported input type or system.')
            mode = 'cso' if system == 'psp' and psp_format == 'cso' else row['mode']
            if not can_replace_source(output):
                raise ValueError('This format always retains the original; it cannot reclaim space.')
            if mode in row['verified_modes']:
                raise ValueError('Already verified. Review the existing output rather than converting again.')
            if destinations[_key(output)] > 1:
                raise ValueError(f'Multiple inputs would publish {output.name}; rename or separate them first.')
            _local(root, path)
            if _key(output.name) in entries(output.parent):
                raise ValueError(f'Output already exists: {output.name}. Nothing will be overwritten.')
            if output.suffix == '.chd':
                hidden = output.parent / '.chd_discs'
                if _key(hidden.name) in entries(output.parent) and (hidden.is_symlink() or not hidden.is_dir()
                        or _key(output.name) in entries(hidden)):
                    raise ValueError('Conflicting or unsafe organized disc location (.chd_discs).')
            files = [path, *(Path(t) for t in row['tracks'])]
            allocated = logical = 0
            for file in files:
                info = _local(root, file).lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_dev != root_device or info.st_nlink != 1:
                    raise ValueError('Not an exclusively owned regular file on the selected volume.')
                if any(owner != path for owner in owners.get(file, ())):
                    raise ValueError(f'Shared track: {file.name}. Keep it for the other disc sheets.')
                if playlist := playlist_members.get(_key(file)) or unknown_playlist:
                    raise ValueError(f'The playlist {playlist.name} uses or may depend on this input. Review it first.')
                allocated += min(info.st_size, info.st_blocks * 512) if hasattr(info, 'st_blocks') else info.st_size
                logical += info.st_size
            if logical != size:
                raise ValueError('Input size changed since the scan. Rescan before planning.')
            if unknown:
                raise ValueError(unknown)
            if output.parent.stat().st_dev != root_device:
                raise ValueError('Output is on another volume. Select that system folder directly.')
            if mode == 'cso':
                cso.validate(path, system)
            # 30% is a planning assumption, never a measured saving or space reservation.
            estimated = (size * 7 + 9) // 10
            savings = allocated - estimated
            if savings <= 0:
                raise ValueError('No estimated reclaimable space (small, sparse or already compact input).')
            bound = row['output_bound']
            kind = fs_type(output.parent)
            if kind == 'fat32' and bound >= 4 * GIB:
                raise ValueError('FAT32 cannot safely stage this image; use a different filesystem.')
            network = fs_type(path.parent) in NETWORK
            scratch = Path(tempfile.gettempdir()) if network or kind in NETWORK else output.parent
            prepared = scratch if kind in NETWORK else output.parent
            needs = space_requirements(output.parent, prepared, scratch, output_bound=bound,
                                       scratch_bytes=size if network else 0,
                                       sheet_bytes=row.get('sheet_bytes', size if mode == 'createcd' else 0))
            library_need = needs[root_device][1]
            for device, (location, amount) in needs.items():
                if device != root_device:
                    if scratch_free is None:
                        scratch_free = hardware.free_bytes(location)
                    if scratch_free < amount:
                        raise ValueError(f'Local staging needs {amount} free bytes; only {scratch_free} are available.')
            candidates.append((dict(path=str(path), system=system, mode=mode, source_bytes=size,
                                    estimated_output_bytes=estimated, estimated_savings_bytes=savings,
                                    required_free_bytes=library_need), bound))
        except (OSError, ValueError, RuntimeError) as exc:
            excluded.append(dict(path=str(path), system=system, reason=str(exc)))
    for issue in scan['issues']:
        system = scanner.library_system(root, Path(issue['path']))
        if system in systems:
            excluded.append(dict(path=issue['path'], system=system, reason=issue['reason']))
    items = []
    savings = occupied = peak = 0
    for item, bound in sorted(candidates, key=lambda pair: (pair[0]['source_bytes'], _key(pair[0]['path']), pair[0]['path'])):
        if target_bytes and savings >= target_bytes:
            break
        needed = occupied + item['required_free_bytes']
        if needed > free:
            excluded.append(dict(path=item['path'], system=item['system'],
                                 reason=f'Insufficient headroom: needs {needed} starting free bytes including earlier outputs; {free} available.'))
            continue
        peak = max(peak, needed)
        occupied += bound
        savings += item['estimated_savings_bytes']
        items.append(item)
    return dict(target_bytes=target_bytes, free_bytes=free, required_free_bytes=peak,
                estimated_output_bytes=sum(i['estimated_output_bytes'] for i in items), estimated_savings_bytes=savings,
                freed_bytes=0, target_met=savings >= target_bytes, sources=[i['path'] for i in items], items=items,
                excluded=sorted(excluded, key=lambda i: (_key(i['path']), i['path'])),
                note='Estimate assumes outputs are 70% of input size; actual results vary. Conversion uses free space. '
                     'Quarantine does not free any. Reclaim space only by explicitly cleaning up verified, play-tested originals.')
