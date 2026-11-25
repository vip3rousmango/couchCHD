"""Convert in a private directory; only publish complete, verified game images."""
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
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import hardware
from .output_formats import SUFFIX_BY_MODE
from .ledger import install_new
from .scanner import FileLine, Game, inspect
from .telemetry import StageSampler


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


@dataclass(frozen=True)
class _Report:
    text: str
    truncated: bool
    disk_full: bool
    checksums_ok: bool


_PERCENT = re.compile(rb'[\r\n](Compressing|Verifying), ([0-9]{1,3}(?:\.[0-9]{1,2})?)% complete')
_SHA1 = re.compile(rb'[\r\n](Raw|Overall) SHA1 verification successful!(?=[\r\n])')
_ERROR = re.compile(rb'[\r\n]Error:', re.I)
_FULL = re.compile(rb'no space left|disk (?:is )?full|ENOSPC|not enough space', re.I)


def _drain(pipe, command: str, updates: deque[str] | None) -> _Report:
    # Info/help need a complete, bounded report. Other commands need only a
    # diagnostic tail; never spool an hours-long progress stream to disk.
    limit = 128 * 1024 if command in ('info', 'help') else 2048
    tail = bytearray()
    truncated = disk_full = error = False
    checksums = set()
    stage = b'Verifying' if command == 'verify' else b'Compressing'
    overlap = b'\n'
    while True:
        chunk = pipe.read1(16 * 1024)
        # All markers matched above are shorter than 128 bytes. Match across
        # split writes, including CR-only updates and EOF with no last newline.
        # Never accumulate an unterminated line.
        data = overlap + (chunk or b'\n')
        disk_full = disk_full or bool(_FULL.search(data))
        if command == 'verify':
            error = error or bool(_ERROR.search(data))
            checksums.update(match[1] for match in _SHA1.finditer(data))
        if updates is not None:
            for match in _PERCENT.finditer(data):
                if match.end() > len(overlap) and match[1] == stage and float(match[2]) <= 100:
                    updates.append(match[2].decode('ascii'))
        if not chunk:
            break
        overlap = data[-128:]
        tail.extend(chunk)
        if len(tail) > limit:
            truncated = True
            del tail[:-limit]
    return _Report(tail.decode('utf-8', 'replace'), truncated, disk_full, len(checksums) == 2 and not error)


def _run(tool: Path, *args: str | Path, cancel: threading.Event, cwd: Path,
         returns: tuple[int, ...] = (0,), progress: Callable[[str], None] | None = None,
         disc: str = '', on_stage=None, logical_bytes: int = 0, device_cpus: int = 0) -> _Report:
    _cancelled(cancel)
    command = str(args[0])
    # One producer drains both streams, one consumer samples the latest value.
    # A slow UI/terminal neither blocks the pipe reader nor builds up a queue.
    updates = deque(maxlen=1) if progress is not None or on_stage is not None else None
    stage = 'verifying' if command == 'verify' else 'compressing'
    if on_stage is not None:
        on_stage(stage)
    if progress is not None:
        title = f'{stage.capitalize()}: {disc}'
        progress(title)
    _cancelled(cancel)
    sent = None
    next_progress = next_thermal_check = float('-inf')
    with ThreadPoolExecutor(max_workers=1) as reader, subprocess.Popen(
            [tool, *args], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=os.name == 'posix') as proc:
        try:
            output = reader.submit(_drain, proc.stdout, command, updates)
            if on_stage is not None:
                started = time.monotonic()
                sampler = StageSampler(proc.pid, device_cpus, logical_bytes, started)
                next_sample = started + 1
            # Don't reap a child until its writer closes. On POSIX a wrapper
            # may exit first; retain the process group for safe cancellation.
            while not output.done() or proc.poll() is None:
                _cancelled(cancel)
                if output.done():
                    output.result()  # a broken reader must not leave a writer blocked
                now = time.monotonic()
                if sys.platform.startswith('linux') and now >= next_thermal_check:
                    hardware.throttle_child(proc.pid)
                    next_thermal_check = now + 1
                if progress is not None and updates and now >= next_progress:
                    latest = updates[-1]
                    if latest != sent:
                        progress(f'{title} ({latest}%)')
                        sent, next_progress = latest, now + 1
                if on_stage is not None and now >= next_sample:
                    percent = float(updates[-1]) if updates else None
                    speed, cpu = sampler.sample(percent, now)
                    on_stage(stage, percent, speed, cpu)
                    next_sample = now + 1
                cancel.wait(.1)
            result = output.result()  # includes any final buffered checksums/errors
        except BaseException:
            if proc.returncode is None:
                try:
                    if os.name == 'posix':
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait()
            raise
    _cancelled(cancel)
    if proc.returncode not in returns:
        detail = result.text.strip()[-1600:]
        if result.disk_full or not shutil.disk_usage(cwd).free:
            cancel.set()
            raise OSError(errno.ENOSPC, detail or 'No space left on device')
        raise RuntimeError(f'chdman {command} exited {proc.returncode}: {detail}')
    if result.truncated and command in ('info', 'help'):
        raise ValueError(f'chdman {command} report too large to validate')
    if progress is not None and updates and updates[-1] != sent:
        progress(f'{title} ({updates[-1]}%)')
    if on_stage is not None:
        # The process has exited; don't display its last CPU/rate during
        # subsequent metadata checks, fsync or publication.
        on_stage(stage, float(updates[-1]) if updates else None)
    return result


def _version(tool: Path, cwd: Path, cancel: threading.Event) -> None:
    # MAME prints its banner and returns 1 for the help command.
    banner = _run(tool, 'help', cwd=cwd, cancel=cancel, returns=(0, 1))
    match = re.search(r'^chdman\b[^\n]*?\b(?:manager|version)\s+(\d+)\.(\d+)', banner.text, re.I | re.M)
    if not match or tuple(map(int, match.groups())) < (0, 256):
        raise RuntimeError('chdman 0.256 or later is required')


def _verify(tool: Path, chd: Path, cwd: Path, cancel: threading.Event, *,
            progress: Callable[[str], None] | None = None, disc: str = '',
            on_stage=None, logical_bytes: int = 0, device_cpus: int = 0) -> None:
    report = _run(tool, 'verify', '-i', chd, cwd=cwd, cancel=cancel, progress=progress, disc=disc,
                  on_stage=on_stage, logical_bytes=logical_bytes, device_cpus=device_cpus)
    # Some MAME releases *return zero* for a mismatch or missing checksums. Require
    # positive confirmation of both data and metadata, not merely an exit status.
    if not report.checksums_ok:
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


def _write_image(game, partial, work, tool, cancel, threads, progress, on_stage, device_cpus, fast_cd):
    if game.mode not in ('createcd', 'createdvd'):
        from . import archive7z, cso, rvz, trim3ds, xiso

        codec = {'7z': archive7z, 'cso': cso, 'rvz': rvz, 'trim3ds': trim3ds, 'xiso': xiso}[game.mode]
        last = None

        def report(stage, percent=None):
            nonlocal last
            if on_stage is not None:
                on_stage(stage, percent)
            if progress is not None:
                key = (stage, int(percent) if percent is not None else None)
                if key != last:
                    suffix = '' if key[1] is None else f' ({key[1]}%)'
                    progress(f'{stage.capitalize()}: {game.source.name}{suffix}')
                    last = key

        codec.convert(game.source, partial, cancel=cancel, threads=threads,
                      on_stage=report if progress is not None or on_stage is not None else None)
        return

    tool = tool.resolve(strict=True) if tool is not None else tool_path()
    _version(tool, work, cancel)
    source = _input(game, work, cancel)
    # CD units carry subcode and are padded to groups of four; DVD units are 2048 bytes.
    logical_bytes = (game.frames[0] * 2048 if game.mode == 'createdvd' else
                     sum((frames + 3) // 4 * 4 for frames in game.frames) * 2448) if on_stage else 0
    if game.mode == 'createdvd':
        flags = ['-hs', '2048', '-c', 'zlib']
    else:
        flags = ['-c', 'cdzl,cdfl'] if fast_cd else []
    _run(tool, game.mode, '-i', source, '-o', partial, *flags, '-np', str(threads),
         cwd=work, cancel=cancel, progress=progress, disc=game.source.name,
         on_stage=on_stage, logical_bytes=logical_bytes, device_cpus=device_cpus)
    _nonempty(partial)
    _verify(tool, partial, work, cancel, progress=progress, disc=game.source.name,
            on_stage=on_stage, logical_bytes=logical_bytes, device_cpus=device_cpus)
    flags = ['-v'] if game.mode == 'createcd' else []
    report = _run(tool, 'info', '-i', partial, *flags, cwd=work, cancel=cancel)
    _match(game, report.text)


def convert(game: Game, output: Path, tool: Path | None = None, *, cancel: threading.Event, threads: int,
            progress: Callable[[str], None] | None = None, on_stage=None, device_cpus: int = 0,
            fast_cd: bool = False) -> None:
    if threads < 1:
        raise ValueError('At least one compression thread is required')
    _cancelled(cancel)
    if os.path.lexists(output):
        raise FileExistsError(output)
    game.assert_unchanged()
    base = _workdir(output.absolute().parent)
    if game.mode not in SUFFIX_BY_MODE:
        raise ValueError(f'Unsupported conversion mode: {game.mode}')
    try:
        with tempfile.TemporaryDirectory(prefix='convert-', dir=base) as tmp:
            work = Path(tmp)
            partial = work / ('output' + SUFFIX_BY_MODE[game.mode])
            _write_image(game, partial, work, tool, cancel, threads, progress, on_stage, device_cpus, fast_cd)
            _nonempty(partial)
            _sync(partial)
            game.assert_unchanged()
            _cancelled(cancel)
            install_new(partial, output)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            cancel.set()  # Other workers will kill and reap their own active chdman.
        raise


_PRIVATE_OUTPUTS = frozenset('output' + ext for ext in SUFFIX_BY_MODE.values())
_STAGED_OUTPUT = re.compile(r'[0-9a-f]{32}(?:' + '|'.join(re.escape(ext) for ext in set(SUFFIX_BY_MODE.values())) + r')\.part')


def recover(root: Path) -> None:
    """Called under the library lock. Never follow links or remove foreign files."""
    for parent, dirs, files in os.walk(root, topdown=False, followlinks=False):
        path = Path(parent)
        internal = path.parent.name == '.couchchd_tmp' and re.fullmatch(r'(?:convert|extract)-[a-z0-9_]{8}', path.name)
        if path.name != '.couchchd_tmp' and not internal:
            continue
        for name in files:
            item = path / name
            owned = name.endswith('.chd.part') or bool(_STAGED_OUTPUT.fullmatch(name)) or internal and (
                name in _PRIVATE_OUTPUTS or re.fullmatch(
                    r'input\.(?:cue|gdi)|track\d{4}\.bin|(?:disc|publish)\.cue|disc(?: \(Track \d+\))?\.bin', name))
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
        report = _run(tool, 'info', '-i', chd, '-v', cwd=work, cancel=cancel).text
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
