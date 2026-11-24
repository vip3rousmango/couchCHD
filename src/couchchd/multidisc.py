"""Group complete, unambiguous sets of journal-authenticated CHDs."""
from __future__ import annotations

from collections import defaultdict
import copy
import os
from pathlib import Path
import re
import shutil
import warnings
import xml.etree.ElementTree as ET

from .ledger import Ledger, SafetyError


_DISC = re.compile(r'^(?P<title>.+?)\s*\((?:Disc|Disk|CD)\s*(?P<n>[0-9]{1,3})(?:\s*of\s*(?P<total>[0-9]{1,3}))?\)(?P<variant>.*)$', re.I)
_IMAGES = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif'}
_SOURCES = {'.chd', '.iso', '.gdi', '.cue'}


def _disc(stem: str):
    match = _DISC.fullmatch(stem)
    if not match or any(ord(c) < 32 for c in stem) or '\\' in stem:
        return None
    # Preserve release/region qualifiers on either side; never combine revisions.
    title = ' '.join(x for x in (match['title'].strip(), match['variant'].strip()) if x)
    if title in {'.', '..'} or _DISC.search(match['variant']):
        return None
    return title, int(match['n']), int(match['total']) if match['total'] else None


def _signature(p: Path):
    s = p.lstat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _artwork(folder: Path, first: Path, title: str):
    media = folder / 'media'
    copies = {}
    if not media.is_dir() or media.is_symlink():
        return copies
    for directory, dirs, files in os.walk(media):
        here = Path(directory)
        dirs[:] = [d for d in dirs if not d.startswith('.') and not (here / d).is_symlink()]
        for name in files:
            source = here / name
            if source.suffix.lower() not in _IMAGES or source.is_symlink() or not source.is_file():
                continue
            # Both media/covers/Game (Disc 1).png and media/Game (Disc 1)-image.png.
            base = source.stem
            if base[:len(first.stem)].casefold() != first.stem.casefold():
                continue
            tail = base[len(first.stem):]
            if tail and tail[0] not in '-_. ':
                continue
            dest = here / (title + tail + source.suffix)
            if not os.path.lexists(dest):
                copies[source] = dest
    return copies


def _local(folder: Path, text: str | None) -> Path | None:
    if not text:
        return None
    name = Path(text.strip().replace('\\', '/'))
    if name.is_absolute() or '..' in name.parts:
        return None
    return folder / name


def _metadata(folder: Path, first: Path, playlist: Path, art):
    xml = folder / 'gamelist.xml'
    if not os.path.lexists(xml):
        return None
    if xml.is_symlink() or not xml.is_file():
        raise SafetyError(f'Unsafe gamelist: {xml}')
    sig = _signature(xml)
    raw = xml.read_bytes()
    # Insert only the clone. Re-serializing the entire XML would discard comments,
    # formatting and extension elements. Leave unfamiliar encodings/DTDs alone.
    text = raw.decode('utf-8-sig')
    enc = re.match(r'''<\?xml\s+[^?]*?encoding\s*=\s*['"]([^'"]+)''', text, re.I)
    if (enc and enc[1].lower() not in {'utf-8', 'utf8', 'us-ascii'}) or '<!DOCTYPE' in text.upper():
        raise SafetyError(f'Cannot safely update this gamelist encoding or DTD: {xml}')
    tree = ET.fromstring(raw)
    if tree.tag != 'gameList':
        raise SafetyError(f'Unrecognized gamelist: {xml}')
    matches = []
    for game in tree.findall('game'):
        path = _local(folder, game.findtext('path'))
        if path is None:
            continue
        if path == playlist:
            return None  # Existing playlist metadata is authoritative.
        if path.stem == first.stem and path.suffix.casefold() in _SOURCES:
            matches.append(game)
    if not matches:
        return None
    if len(matches) != 1:
        raise SafetyError(f'Ambiguous Disc 1 metadata: {xml}')
    clone = copy.deepcopy(matches[0])
    clone.find('path').text = './' + playlist.name
    name = clone.find('name')
    if name is not None and name.text:
        parsed = _disc(name.text)
        if parsed:
            name.text = parsed[0]
    for field in clone:
        existing = _local(folder, field.text)
        if existing in art:
            field.text = './' + art[existing].relative_to(folder).as_posix()
    # Match the actual closing tag, not a substring inside a trailing comment.
    from xml.parsers import expat
    parser = expat.ParserCreate()
    close_at = None
    depth = 0

    def start(tag, attrs):
        nonlocal depth
        depth += 1

    def end(tag):
        nonlocal depth, close_at
        depth -= 1
        if depth == 0:
            close_at = parser.CurrentByteIndex

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.Parse(raw, True)
    clone.tail = None
    fragment = ET.tostring(clone, encoding='us-ascii', xml_declaration=False)
    updated = raw[:close_at] + b'  ' + fragment + b'\n' + raw[close_at:]
    return xml, sig, updated


def _recalbox(root: Path) -> bool:
    return root.is_relative_to('/recalbox/share') or Path('/recalbox/recalbox.version').exists()


def _frontend_running() -> bool:
    proc = Path('/proc')
    if not proc.is_dir():
        return True
    for path in proc.glob('[0-9]*/comm'):
        try:
            if path.read_text().strip().lower().startswith('emulationstatio'):
                return True
        except FileNotFoundError:
            continue
        except PermissionError:
            return True
    return False


def _referenced(folder: Path, files: list[Path]) -> bool:
    names = {p.name.casefold() for p in files}
    for p in folder.iterdir():
        if p.suffix.casefold() not in {'.m3u', '.m3u8'}:
            continue
        if p.is_symlink() or not p.is_file():
            return True
        for line in p.read_text(encoding='utf-8-sig').splitlines():
            if line and not line.startswith('#') and Path(line.strip().replace('\\', '/')).name.casefold() in names:
                return True
    return False


def _group(folder: Path, title: str, paths: list[Path], ledger: Ledger, batch: int) -> str | None:
    tagged = sorted((_disc(p.stem)[1], p) for p in paths)
    if len(tagged) < 2:
        return None
    numbers = [n for n, _ in tagged]
    if numbers != list(range(1, len(numbers) + 1)):
        raise SafetyError('duplicate or missing disc numbers')
    known = {p.stem for p in paths}
    playlist = folder / (title + '.m3u')
    hidden = folder / '.chd_discs'
    if hidden.is_symlink() or (os.path.lexists(hidden) and not hidden.is_dir()):
        raise SafetyError('unsafe .chd_discs directory')
    for p in folder.iterdir():
        if p.name.casefold() == playlist.name.casefold():
            raise SafetyError('playlist already exists')
        if p.suffix.casefold() not in _SOURCES:
            continue
        d = _disc(p.stem)
        if d and d[0].casefold() == title.casefold():
            if p.stem not in known or (p.suffix.casefold() == '.chd' and p not in paths):
                raise SafetyError('another edition or unconverted disc exists')
            if d[2] is not None and d[2] != len(numbers):
                raise SafetyError('declared disc count does not match')
    if hidden.is_dir():
        for p in hidden.iterdir():
            d = _disc(p.stem)
            if p.suffix.casefold() == '.chd' and d and d[0].casefold() == title.casefold():
                raise SafetyError('hidden disc set already exists')
    if _referenced(folder, paths):
        raise SafetyError('an existing playlist uses these discs')
    if _recalbox(folder) and (folder / 'gamelist.xml').exists():
        message = 'Recalbox caches gamelist.xml; stop EmulationStation before relinking metadata.'
        warnings.warn(message, UserWarning, stacklevel=2)
        if _frontend_running():
            raise SafetyError(message)
    first = tagged[0][1]
    art = _artwork(folder, first, title)
    metadata = _metadata(folder, first, playlist, art)
    checkpoint = ledger.checkpoint(batch)
    try:
        for _, disc in tagged:
            ledger.move(batch, disc, hidden / disc.name)
        entries = ''.join('.chd_discs/' + p.name + '\n' for _, p in tagged)
        ledger.publish(batch, playlist, lambda stage: stage.write_bytes(entries.encode('utf-8')))
        for src, dst in art.items():
            ledger.publish(batch, dst, lambda stage, src=src: shutil.copyfile(src, stage))
        if metadata:
            xml, signature, updated = metadata
            ledger.save_metadata(batch, xml, expected=signature)
            ledger.publish(batch, xml, lambda stage: stage.write_bytes(updated))
    except BaseException:
        ledger.rollback_since(batch, checkpoint)
        raise
    return f'Created {playlist}. Re-run Steam ROM Manager if your Steam shortcuts pointed to a disc.'


def organize(root: Path, batch: int, ledger: Ledger, outputs: list[Path]) -> list[str]:
    """Group verified siblings of newly converted outputs; never adopt foreign CHDs."""
    root = Path(root).resolve(strict=True)
    touched = set()
    for path in outputs:
        p = Path(path)
        p = p if p.is_absolute() else root / p
        d = _disc(p.stem)
        if d:
            touched.add((p.parent, d[0]))
    groups = defaultdict(list)
    for p in ledger.verified():
        if not p.is_relative_to(root) or p.parent.name == '.chd_discs':
            continue
        d = _disc(p.stem)
        if d and (p.parent, d[0]) in touched:
            groups[p.parent, d[0]].append(p)
    messages = []
    for (folder, title), paths in sorted(groups.items()):
        try:
            message = _group(folder, title, paths, ledger, batch)
            if message:
                messages.append(message)
        except (OSError, UnicodeError, SafetyError, ET.ParseError) as exc:
            messages.append(f'Skipped {title}: {exc}')
    return messages
