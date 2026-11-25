"""Materialize one seekable disc under caller-owned private scratch, never archive paths."""
import hashlib
from collections import defaultdict
from contextlib import nullcontext

from . import disc_zip, scanner

_CHUNK = 1024 * 1024


def _transfer(stream, count, destination, cancel):
    digest = hashlib.sha256() if destination else None
    with destination.open('xb') if destination else nullcontext() as out:
        while count:
            if cancel.is_set():
                raise InterruptedError('ZIP preparation cancelled; archive retained')
            data = stream.read(min(count, _CHUNK))
            if not data or len(data) > count:
                raise ValueError('ZIP member does not match its declared size')
            if out:
                digest.update(data)
                out.write(data)
            count -= len(data)
    if destination:
        # Verify the seekable bytes the native tool will actually see. A ZIP CRC
        # proves what was decoded, not that the scratch write succeeded intact.
        actual = hashlib.sha256()
        with destination.open('rb') as staged:
            while chunk := staged.read(_CHUNK):
                if cancel.is_set():
                    raise InterruptedError('ZIP preparation cancelled; archive retained')
                actual.update(chunk)
        if actual.digest() != digest.digest():
            raise OSError('ZIP member differs after staging')


def prepare(game, work, cancel):
    archive = game.archive
    suffix = archive.entry.suffix.lower()
    descriptor = suffix in {'.cue', '.gdi'}
    if suffix not in {'.cue', '.gdi', '.iso', '.gcm'}:
        raise ValueError('Unsupported archived disc entry')
    primary = work / ('zip-source' + suffix)
    # Each distinct GDI extent goes straight to its final input file. In
    # particular, do not first expand a shared track and then copy its slices.
    spans = defaultdict(dict)
    if descriptor:
        for number, line in enumerate(game.sheet):
            if isinstance(line, scanner.FileLine):
                spans[line.path].setdefault((line.offset, line.length), work / f'zip-track{number:04d}.bin')
    for member in archive.members:
        with disc_zip.open_member(game, member.path) as src:
            if member.path == archive.entry:
                _transfer(src, member.size, None if descriptor else primary, cancel)
            elif member.path in spans:
                pos = 0
                for (offset, length), target in sorted(spans[member.path].items(), key=lambda kv: kv[0][0]):
                    length = member.size - offset if length is None else length
                    if offset < pos or length <= 0 or offset + length > member.size:
                        raise ValueError('Invalid archived track extent')
                    _transfer(src, offset - pos, None, cancel)
                    _transfer(src, length, target, cancel)
                    pos = offset + length
                _transfer(src, member.size - pos, None, cancel)
            else:
                raise ValueError('Unexpected member in archived disc')
    if descriptor:
        with primary.open('x', encoding='utf-8', newline='\n') as sheet:
            for line in game.sheet:
                if isinstance(line, scanner.FileLine):
                    name = spans[line.path][line.offset, line.length].name
                    line = line.before + (f'"{name}"' if suffix == '.cue' else name) + line.after
                sheet.write(line + '\n')
    # Full on-disk geometry validation is required even for an archive whose
    # bounded scan only inspected headers (e.g. Nintendo partition tables).
    staged = scanner.inspect(primary, game.system, fresh=False)
    if (staged.mode, staged.frames) != (game.mode, game.frames):
        raise ValueError('Staged disc geometry differs from the scanned ZIP')
    return staged
