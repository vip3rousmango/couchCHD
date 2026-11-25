"""Lossless GameCube/Wii RVZ via DolphinTool. Never scrub or materialize a second ISO."""
from __future__ import annotations

import hashlib
import errno
import os
import platform
import re
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import hardware

_GC = bytes.fromhex('c2339f3d')
_WII = bytes.fromhex('5d1c9ea3')
_BLOCK = 131072
_MAX_DISC = 8543666176  # dual-layer Wii development disc


def _at(file, offset, length):
    file.seek(offset)
    data = file.read(length)
    if len(data) != length:
        raise ValueError('Truncated Nintendo disc header')
    return data


def _boot(file, base, size, shift):
    dol, fst, fst_size = (v << shift for v in struct.unpack('>III', _at(file, base + 0x420, 12)))
    # Datel discs need not have a boot DOL; a nonzero pointer must fit.
    if dol and not 0x2440 <= dol <= size - 0x100:
        raise ValueError('Invalid boot DOL offset')
    if not (0x2440 <= fst and 12 <= fst_size <= 128 << 20 and fst + fst_size <= size):
        raise ValueError('Invalid disc filesystem range')
    root, parent, count = struct.unpack('>III', _at(file, base + fst, 12))
    if root >> 24 != 1 or parent or not 1 <= count <= fst_size // 12:
        raise ValueError('Invalid disc filesystem root')


def _inspect(source: Path, expected: str | None) -> int:
    with source.open('rb') as file:
        st = os.fstat(file.fileno())
        size = st.st_size
        if not stat.S_ISREG(st.st_mode) or not 0x2460 <= size <= _MAX_DISC:
            raise ValueError('Unsupported raw Nintendo disc size')
        h = _at(file, 0, 0x440)
        wii, gc = h[0x18:0x1c] == _WII, h[0x1c:0x20] == _GC
        if wii == gc or h[0x200:0x204] == b'NKIT':
            raise ValueError('Expected a raw GameCube or Wii disc (not NKit or a container)')
        system = 'wii' if wii else 'gc'
        if expected is not None and expected != system:
            raise ValueError(f'Found a {system} header, expected {expected}')
        if source.suffix.lower() not in (('.iso',) if wii else ('.iso', '.gcm')):
            raise ValueError(f'Unsupported raw {system} disc extension')
        if gc:
            _boot(file, 0, size, 0)
            return size
        # Offsets in the partition descriptors are 32-bit *words*, not bytes.
        # Never send unbounded partition counts or certificate lengths to the CLI.
        groups = _at(file, 0x40000, 32)
        partitions = []
        game = False
        for count, address in struct.iter_unpack('>II', groups):
            if not count:
                continue
            start = address << 2
            if (len(partitions) + count > 256 or start < 0x40020 or start + count * 8 > size):
                raise ValueError('Invalid Wii partition table')
            for sector, kind in struct.iter_unpack('>II', _at(file, start, count * 8)):
                base = sector << 2
                if base < 0x40020 or base + 0x2c0 > size:
                    raise ValueError('Invalid Wii partition header')
                tsize, tmd, csize, cert, h3, data, length = struct.unpack('>7I', _at(file, base + 0x2a4, 28))
                data, length = data << 2, length << 2
                if (data < 0x2c0 or not length or base + data + length > size or
                        not 0x1e4 <= tsize <= 0x49e4 or csize > 4 << 20 or
                        tmd << 2 < 0x2c0 or (tmd << 2) + tsize > data or
                        (cert << 2) + csize > data or (h3 << 2) > data):
                    raise ValueError('Invalid Wii partition data range')
                if (_at(file, base, 4) != b'\x00\x01\x00\x01' or
                        _at(file, base + (tmd << 2), 4) != b'\x00\x01\x00\x01'):
                    raise ValueError('Invalid Wii ticket or title metadata header')
                if not h[0x60] and ((h3 << 2) + 0x18000 > data or length % 0x8000):
                    raise ValueError('Invalid Wii hashed partition')
                if kind == 0:
                    game = True
                    if h[0x60] and h[0x61]:  # unencrypted, unhashed development/homebrew partition
                        _boot(file, base + data, length, 2)
                partitions.append((base, base + data + length))
        partitions.sort()
        if not game or any(b[0] < a[1] for a, b in zip(partitions, partitions[1:])):
            raise ValueError('Missing game partition or overlapping Wii partitions')
        return size


def validate(source: Path, system: str) -> None:
    _inspect(source, system)


def tool_path() -> Path:
    override = os.environ.get('COUCHCHD_DOLPHIN_TOOL')
    if override:
        paths = [Path(override).expanduser()]
    elif getattr(sys, '_MEIPASS', None):
        # Never silently substitute an untested system emulator in a release.
        paths = [Path(sys._MEIPASS) / 'bin' / platform.machine() / 'rvz' / 'dolphin-tool']
    else:
        paths = [Path(p) for name in ('dolphin-tool', 'DolphinTool') if (p := shutil.which(name))]
        paths.append(Path('/usr/games/dolphin-tool'))
    for path in paths:
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve(strict=True)
    raise FileNotFoundError('DolphinTool not found; install it or set COUCHCHD_DOLPHIN_TOOL')


def _check(cancel):
    if cancel.is_set():
        raise InterruptedError('RVZ conversion cancelled; originals retained')


def _run(tool, args, user, threads, cancel, progress):
    env = os.environ.copy()
    if (tool.parent / 'dolphin-tool.bin').is_file():
        # Do not expose the frozen app's libraries (or our private glibc) to
        # /bin/sh. The packaged launcher execs its own loader with --library-path.
        env.pop('LD_LIBRARY_PATH', None)
        env.pop('LD_PRELOAD', None)
    elif getattr(sys, 'frozen', False):
        if orig := env.get('LD_LIBRARY_PATH_ORIG'):
            env['LD_LIBRARY_PATH'] = orig
        else:
            env.pop('LD_LIBRARY_PATH', None)
    env['COUCHCHD_DOLPHIN_THREADS'] = str(min(4, max(1, threads)))
    env['LC_ALL'] = 'C'
    _check(cancel)
    # Stock builds have no -threads option. Set affinity on the spawning Linux
    # worker thread before exec, not on just one TID after Dolphin starts its pool.
    allowed = os.sched_getaffinity(0) if hasattr(os, 'sched_getaffinity') else None
    mask = set(sorted(allowed)[:max(1, threads)]) if allowed else None
    if allowed != mask:
        os.sched_setaffinity(0, mask)
    try:
        proc = subprocess.Popen([tool, *args, '-u', user], env=env, cwd=user, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        if allowed != mask:
            try:
                os.sched_setaffinity(0, allowed)
            except OSError:
                pass  # CPU hotplug/cpuset change: retain the narrower mask.
    tail, pending = b'', b''
    next_sample = 0.0
    with proc, proc.stdout, selectors.DefaultSelector() as selector:
        try:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map() or proc.poll() is None:
                _check(cancel)
                now = time.monotonic()
                if now >= next_sample:
                    hardware.throttle_child(proc.pid)
                    next_sample = now + 1
                for _, _events in selector.select(0.15):
                    chunk = os.read(proc.stdout.fileno(), 8192)
                    if not chunk:
                        selector.unregister(proc.stdout)
                        continue
                    tail = (tail + chunk)[-4096:]
                    data = pending + chunk
                    lines = data.split(b'\n')
                    pending = lines.pop()[-80:]
                    for line in lines:
                        if m := re.fullmatch(rb'RVZ progress (\d{1,3})', line):
                            progress(min(100, int(m[1])))
            rc = proc.wait()
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            raise
    _check(cancel)
    if rc:
        detail = tail.decode(errors='replace').strip()[-1600:]
        if b'no space left' in tail.lower() or not shutil.disk_usage(user).free:
            cancel.set()
            raise OSError(errno.ENOSPC, detail or 'No space left on device')
        raise RuntimeError(f'DolphinTool {args[0]} exited {rc}: {detail}')
    return tail


def convert(source: Path, target: Path, *, cancel, threads: int, on_stage=None) -> None:
    _check(cancel)
    size = _inspect(source, None)
    source, target = source.absolute(), target.absolute()
    tool = tool_path()
    def report(stage, percent=None):
        if on_stage is not None:
            on_stage(stage, percent)
    # Reserve a fresh filename; don't let Dolphin overwrite an existing file/link.
    with target.open('xb'):
        pass
    try:
        with tempfile.TemporaryDirectory(dir=target.parent, prefix='dolphin-') as tmp:
            report('compressing')
            _run(tool, ['convert', '-i', source, '-o', target, '-f', 'rvz', '-b', str(_BLOCK),
                        '-c', 'zstd', '-l', '5'], tmp, threads, cancel, lambda p: report('compressing', p))
            with target.open('rb') as f:
                header = _at(f, 0, 0x48)
                if (header[:4] != b'RVZ\x01' or struct.unpack_from('>QQ', header, 0x24) !=
                        (size, os.fstat(f.fileno()).st_size)):
                    raise ValueError('DolphinTool produced an invalid RVZ header or size')
            # verify -a sha1 computes a digest of the decoded source bytes, not the
            # compressed bytes or a stored checksum. No second multi-GiB ISO needed.
            report('verifying', 0)
            digest = hashlib.sha1(usedforsecurity=False)
            done = last = 0
            buf = bytearray(1024 * 1024)
            view = memoryview(buf)
            with source.open('rb', buffering=0) as f:
                while (n := f.readinto(buf)):
                    _check(cancel)
                    digest.update(view[:n])
                    done += n
                    if done > size:
                        raise ValueError('Raw disc size changed during RVZ verification')
                    pct = min(50, done * 50 // size)
                    if pct != last:
                        report('verifying', pct)
                        last = pct
            result = _run(tool, ['verify', '-i', target, '-a', 'sha1'], tmp, threads, cancel,
                          lambda p: report('verifying', 50 + p / 2))
            hashes = re.findall(rb'(?m)^([0-9a-fA-F]{40})\r?$', result)
            if done != size or len(hashes) != 1 or hashes[0].lower() != digest.hexdigest().encode():
                raise ValueError('RVZ verification failed: decoded disc differs from original')
            report('verifying', 100)
            _check(cancel)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
