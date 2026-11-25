"""Run chdman on validated inputs and only publish complete, verified outputs."""
from __future__ import annotations

import errno
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .ledger import install_new
from .scanner import FileLine, Game, inspect


def tool_path() -> Path:
    override = os.environ.get('COUCHCHD_CHDMAN')
    arch = {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(platform.machine().lower(), platform.machine().lower())
    exe = 'chdman.exe' if os.name == 'nt' else 'chdman'
    if override:
        candidates = [Path(override).expanduser()]
    else:
        roots = [Path(__file__).resolve().parents[2]]
        if getattr(sys, '_MEIPASS', None):
            roots.insert(0, Path(sys._MEIPASS))
        candidates = [p/'bin'/arch/exe for p in roots] if arch in {'x86_64', 'aarch64'} else []
        installed = shutil.which(exe)
        if installed:
            candidates.append(Path(installed))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve(strict=True)
    raise FileNotFoundError('No executable chdman; check COUCHCHD_CHDMAN, bundled bin directory, or PATH')


def _cancelled(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise InterruptedError('Conversion cancelled; originals retained')


def _run(tool: Path, *args: str | Path, cancel: threading.Event, cwd: Path,
         returns: tuple[int, ...] = (0,)) -> str:
    _cancelled(cancel)
    # chdman progress uses carriage returns for hours at a time. Spill to a temporary
    # file, not an undrained pipe or an unbounded in-memory buffer.
    with tempfile.TemporaryFile() as log:
        proc = subprocess.Popen([tool, *args], cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=os.name == 'posix')
        try:
            while proc.poll() is None:
                if cancel.wait(.1):
                    _cancelled(cancel)
        finally:
            if proc.poll() is None:
                try:
                    if os.name == 'posix':
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait()
        _cancelled(cancel)
        log.seek(max(0, log.tell() - 128 * 1024))
        result = log.read().decode('utf-8', 'replace')
    if proc.returncode not in returns:
        detail = result.strip()[-1600:]
        if re.search(r'no space left|disk (?:is )?full|ENOSPC|not enough space', result, re.I) or not shutil.disk_usage(cwd).free:
            cancel.set()
            raise OSError(errno.ENOSPC, detail or 'No space left on device')
        raise RuntimeError(f'chdman {args[0]} exited {proc.returncode}: {detail}')
    return result


def _version(tool: Path, cwd: Path, cancel: threading.Event) -> None:
    # MAME prints its banner and returns 1 for the help command.
    banner = _run(tool, 'help', cwd=cwd, cancel=cancel, returns=(0, 1))
    match = re.search(r'^chdman\b[^\n]*?\b(?:manager|version)\s+(\d+)\.(\d+)', banner, re.I | re.M)
    if not match or tuple(map(int, match.groups())) < (0, 256):
        raise RuntimeError('chdman 0.256 or later is required')


def _verify(tool: Path, chd: Path, cwd: Path, cancel: threading.Event) -> None:
    report = _run(tool, 'verify', '-i', chd, cwd=cwd, cancel=cancel)
    # Some MAME releases *return zero* for a mismatch or missing checksums. Require
    # positive confirmation of both data and metadata, not merely an exit status.
    if ('Raw SHA1 verification successful!' not in report or 'Overall SHA1 verification successful!' not in report
            or re.search(r'(?:^|[\r\n])Error:', report, re.I)):
        raise ValueError('CHD checksum verification did not confirm both raw and overall SHA1')


def _number(report: str, label: str) -> int:
    matches = re.findall(rf'^{label}:\s*([\d,]+)(?: bytes)?\s*$', report, re.M | re.I)
    if len(matches) != 1:
        raise ValueError(f'Missing or ambiguous CHD {label}')
    return int(matches[0].replace(',', ''))


_TRACK = re.compile(r"^Metadata:\s+Tag='(?:CHT2|CHTR|CHGD)'[^\n]*\n[ \t]*TRACK:(\d+)\s+[^\n]*?\bFRAMES:(\d+)(?:[\s.]|$)", re.M)


def _match(game: Game, report: str) -> None:
    if game.mode == 'createdvd':
        if ("Tag='DVD '" not in report or _number(report, 'Unit Size') != 2048
                or _number(report, 'Hunk Size') != 2048 or _number(report, 'Total Units') != game.frames[0]
                or _number(report, 'Logical size') != game.frames[0] * 2048):
            raise ValueError('DVD metadata does not match source sector geometry')
    else:
        actual = [(int(track), int(frames)) for track, frames in _TRACK.findall(report)]
        if actual != list(enumerate(game.frames, 1)):
            raise ValueError(f'CD track/frame mismatch: expected {game.frames}, found {actual}')


def _copy_extent(src: Path, target: Path, offset: int, length: int, cancel: threading.Event) -> None:
    with src.open('rb') as inp, target.open('xb') as out:
        inp.seek(offset)
        while length:
            _cancelled(cancel)
            chunk = inp.read(min(length, 1024 * 1024))
            if not chunk:
                raise ValueError(f'Short source read: {src}')
            out.write(chunk)
            length -= len(chunk)


def _input(game: Game, work: Path, cancel: threading.Event) -> Path:
    if not game.sheet:
        return game.source
    descriptor = work / ('input.gdi' if game.source.suffix.lower() == '.gdi' else 'input.cue')
    with descriptor.open('x', encoding='utf-8', newline='\n') as f:
        for i, line in enumerate(game.sheet):
            if isinstance(line, FileLine):
                try:
                    filename = Path(os.path.relpath(line.path, work)).as_posix()
                except ValueError:  # Different Windows volumes.
                    filename = ''
                if (line.length is not None or not filename or len(os.fsencode(filename)) > 400
                        or descriptor.suffix == '.gdi' and "'" in filename and ' ' not in filename
                        or any(c in filename for c in '\r\n"\t')):
                    alias = work / f'track{i:04d}.bin'
                    _copy_extent(line.path, alias, line.offset,
                                 line.length if line.length is not None else line.path.stat().st_size, cancel)
                    filename = alias.name
                if descriptor.suffix == '.cue' or ' ' in filename:
                    filename = f'"{filename}"'
                line = line.before + filename + line.after
            f.write(line + '\n')
    # chdman interprets the descriptor directory itself as a path prefix. Its
    # older parsers misidentify POSIX directories containing literal backslashes.
    return Path(descriptor.name)  # _run invokes chdman with cwd=work.


def _workdir(parent: Path) -> Path:
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError(f'Unsafe output directory: {parent}')
    base = parent if parent.name == '.couchchd_tmp' else parent / '.couchchd_tmp'
    base.mkdir(mode=0o700, exist_ok=True)
    if base.is_symlink() or not base.is_dir():
        raise ValueError(f'Unsafe scratch directory: {base}')
    return base.resolve(strict=True)


def _nonempty(path: Path) -> None:
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_size <= 0:
        raise ValueError(f'Expected a nonempty regular file: {path}')


def _sync(path: Path) -> None:
    with path.open('r+b') as file:
        os.fsync(file.fileno())


def convert(game: Game, output: Path, tool: Path, *, cancel: threading.Event, threads: int) -> None:
    if threads < 1:
        raise ValueError('At least one compression thread is required')
    _cancelled(cancel)
    if os.path.lexists(output):
        raise FileExistsError(output)
    game.assert_unchanged()
    base = _workdir(output.absolute().parent)
    tool = tool.resolve(strict=True)
    try:
        with tempfile.TemporaryDirectory(prefix='convert-', dir=base) as tmp:
            work = Path(tmp)
            _version(tool, work, cancel)
            source = _input(game, work, cancel)
            partial = work / (game.source.stem + '.chd.part')
            # One widely supported lossless codec instead of trial-compressing
            # each 2 KiB DVD hunk with MAME's entire default codec set.
            flags = ['-hs', '2048', '-c', 'zlib'] if game.mode == 'createdvd' else []
            _run(tool, game.mode, '-i', source, '-o', partial, *flags, '-np', str(threads), cwd=work, cancel=cancel)
            _nonempty(partial)
            _verify(tool, partial, work, cancel)
            flags = ['-v'] if game.mode == 'createcd' else []  # Never compare truncated track metadata.
            report = _run(tool, 'info', '-i', partial, *flags, cwd=work, cancel=cancel)
            _match(game, report)
            _sync(partial)
            game.assert_unchanged()
            _cancelled(cancel)
            install_new(partial, output)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            cancel.set()  # Other workers will kill and reap their own active chdman.
        raise


def recover(root: Path) -> None:
    """Called under the library lock. Never follow links or remove foreign files."""
    for parent, dirs, files in os.walk(root, topdown=False, followlinks=False):
        path = Path(parent)
        internal = path.parent.name == '.couchchd_tmp' and re.fullmatch(r'(?:convert|extract)-[a-z0-9_]{8}', path.name)
        if path.name != '.couchchd_tmp' and not internal:
            continue
        for name in files:
            item = path / name
            owned = name.endswith('.chd.part') or internal and re.fullmatch(
                r'input\.(?:cue|gdi)|track\d{4}\.bin|(?:disc|publish)\.cue|disc(?: \(Track \d+\))?\.bin', name)
            if owned and stat.S_ISREG(item.lstat().st_mode):
                item.unlink()
        if internal and not any(path.iterdir()):
            path.rmdir()


def extract(chd: Path, dest: Path, tool: Path) -> Path:
    """Verify a CD CHD and publish its BIN(s) first, CUE last. Never overwrite."""
    _nonempty(chd)
    chd = chd.resolve(strict=True)
    original = chd.stat()
    stem = chd.stem
    if any(c in stem for c in '\n\r"') or any(ord(c) < 32 for c in stem):
        raise ValueError('Filename cannot be represented in a CUE')
    tool = tool.resolve(strict=True)
    dest.mkdir(parents=True, exist_ok=True)
    base = _workdir(dest.absolute())
    cancel = threading.Event()
    cue = dest / (stem + '.cue')
    if os.path.lexists(cue):
        raise FileExistsError(cue)
    with tempfile.TemporaryDirectory(prefix='extract-', dir=base) as tmp:
        work = Path(tmp)
        _version(tool, work, cancel)
        _verify(tool, chd, work, cancel)
        report = _run(tool, 'info', '-i', chd, '-v', cwd=work, cancel=cancel)
        if not _TRACK.search(report):
            raise ValueError('Extract to BIN/CUE requires a CD or GD-ROM CHD')
        # A constant basename prevents chdman's %t filename templating from
        # interpreting a game's actual title. GD-ROMs produce split BINs.
        _run(tool, 'extractcd', '-i', chd, '-o', 'disc.cue', cwd=work, cancel=cancel)
        extracted = inspect(work/'disc.cue', 'dreamcast', fresh=False)
        _match(extracted, report)
        now = chd.stat()
        if (not os.path.samestat(now, original) or now.st_ctime_ns != original.st_ctime_ns
                or now.st_mtime_ns != original.st_mtime_ns or now.st_size != original.st_size):
            raise ValueError('Input CHD changed during extraction')
        mapping: dict[Path, Path] = {}
        for item in extracted.files[1:]:
            if item.parent != work or not re.fullmatch(r'disc(?: \(Track \d+\))?\.bin', item.name):
                raise ValueError(f'Unexpected extracted track name: {item}')
            output = dest / (stem + item.name[4:])
            if os.path.lexists(output):
                raise FileExistsError(output)
            mapping[item] = output
        rewritten = work/'publish.cue'
        with rewritten.open('x', encoding='utf-8', newline='\n') as file:
            for line in extracted.sheet:
                if isinstance(line, FileLine):
                    line = f'{line.before}"{mapping[line.path].name}"{line.after}'
                file.write(line + '\n')
        installed = []
        try:
            for src, target in [*mapping.items(), (rewritten, cue)]:
                _sync(src)
                identity = src.stat()
                install_new(src, target)
                installed.append((target, identity))
        except BaseException:
            for target, identity in reversed(installed):
                if target.exists() and os.path.samestat(target.lstat(), identity):
                    target.unlink()
            raise
    return cue
