"""Host storage, power, temperature and suspend guards. No privileged access required."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path


_SYS = Path('/sys')
_MOUNTINFO = Path('/proc/self/mountinfo')
_MEMINFO = Path('/proc/meminfo')
_worker = threading.local()
_ESCAPE = re.compile(rb'\\([0-7]{3})')


def _existing(path: Path) -> Path:
    path = path.resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def free_bytes(path: Path) -> int:
    """Bytes available to the current user on the destination volume."""
    return shutil.disk_usage(_existing(path)).free


def _fs_name(name: str) -> str:
    name = name.lower()
    return {'vfat': 'fat32', 'msdos': 'fat32', 'fat': 'fat32', 'nfs4': 'nfs',
            'smbfs': 'cifs', 'smb3': 'cifs', 'fuse.sshfs': 'sshfs'}.get(name, name)


def fs_type(path: Path) -> str:
    """Find the deepest enclosing mount (statvfs on Linux has no f_fstypename)."""
    path = _existing(path)
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        root = ctypes.create_unicode_buffer(32768)
        if not kernel.GetVolumePathNameW(str(path), root, len(root)):
            return ''
        if kernel.GetDriveTypeW(root) == 4:  # DRIVE_REMOTE, including mapped drives
            return 'cifs'
        name = ctypes.create_unicode_buffer(256)
        if kernel.GetVolumeInformationW(root, None, 0, None, None, None, name, wintypes.DWORD(len(name))):
            return _fs_name(name.value)
        return ''
    stat = os.statvfs(path)
    if name := getattr(stat, 'f_fstypename', ''):
        return _fs_name(os.fsdecode(name))
    try:
        mounts = _MOUNTINFO.read_bytes().splitlines()
    except OSError:
        return ''
    deepest = -1
    result = ''
    for entry in mounts:
        left, sep, right = entry.partition(b' - ')
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        mount = Path(os.fsdecode(_ESCAPE.sub(lambda m: bytes([int(m[1], 8)]), fields[4])))
        if len(mount.parts) >= deepest and path.is_relative_to(mount):
            result = os.fsdecode(right.split()[0])
            deepest = len(mount.parts)
    return _fs_name(result)


def cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def memory_bytes() -> int | None:
    """Usable physical RAM (not free pages, which fluctuate during a batch)."""
    try:
        with _MEMINFO.open('rb') as meminfo:
            for line in meminfo:
                if line.startswith(b'MemTotal:'):
                    match = re.fullmatch(rb'MemTotal:\s+(\d+)\s+kB\s*', line)
                    return int(match[1]) * 1024 if match else None
    except (OSError, ValueError):
        pass
    return None


def _text(path: Path) -> str:
    try:
        return path.read_text('ascii').strip().lower()
    except (OSError, UnicodeError):
        return ''


def _power() -> tuple[bool, int | None]:
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class PowerStatus(ctypes.Structure):
            _fields_ = [('ac', wintypes.BYTE), ('flags', wintypes.BYTE), ('percent', wintypes.BYTE),
                        ('saving', wintypes.BYTE), ('seconds', wintypes.DWORD), ('full', wintypes.DWORD)]
        status = PowerStatus()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return status.ac == 0 and status.flags != 128, status.percent if status.percent <= 100 else None
        return False, None
    levels = []
    discharging = False
    for supply in (_SYS / 'class/power_supply').glob('*'):
        if _text(supply / 'type') != 'battery' or _text(supply / 'scope') == 'device':
            continue
        if _text(supply / 'status') != 'discharging':
            continue
        discharging = True
        capacity = _text(supply / 'capacity')
        if capacity.isdecimal() and int(capacity) <= 100:
            levels.append(int(capacity))
    return discharging, min(levels, default=None)


def on_battery() -> bool:
    return _power()[0]


def _overheated(*, portable: bool = False) -> bool:
    def hot(sensor: Path) -> bool:
        try:
            value = int(_text(sensor))
            return value >= 80_000 if portable else value > 88_000
        except ValueError:
            return False
    for zone in (_SYS / 'class/thermal').glob('thermal_zone*'):
        kind = _text(zone / 'type')
        if any(key in kind for key in ('cpu', 'pkg', 'soc', 'acpitz')) and hot(zone / 'temp'):
            return True
    for chip in (_SYS / 'class/hwmon').glob('hwmon*'):
        name = _text(chip / 'name')
        if name in ('k10temp', 'zenpower', 'coretemp', 'cpu_thermal') or (portable and name == 'amdgpu'):
            if any(hot(sensor) for sensor in chip.glob('temp*_input')):
                return True
    return False


@contextmanager
def portable_budget(threads: int, progress, cancel):
    """Run ONLY on a dedicated batch pool thread. Children inherit its mask at fork/exec."""
    allowed = None
    mask = None
    hot = _overheated(portable=True)
    if hot:
        threads = 1
        progress('Thermal limit: starting on one CPU at 80°C or above.')
    try:
        allowed = os.sched_getaffinity(0)
        mask = set(sorted(allowed)[-threads:])
        if mask != allowed:
            os.sched_setaffinity(0, mask)
        threads = len(mask)
    except (AttributeError, OSError):
        # Sandboxes may deny changing affinity. -np still bounds compression
        # workers, but is NOT a CPU ceiling (chdman has a separate I/O queue).
        mask = None
        threads = 1
        progress('CPU affinity unavailable: using chdman -np 1 without a hard CPU ceiling.')

    def fail_hot():
        cancel.set()
        raise InterruptedError('Thermal limit: cannot enforce one CPU; stopped with originals retained.')

    def throttle(pid):
        nonlocal hot
        if mask is not None and len(mask) == 1:
            return  # Already constrained before launch, including every helper.
        if not hot and not _overheated(portable=True):
            return
        if mask is None:
            fail_hot()
        # Affinity on Linux applies to a TID, not an entire thread group.
        # Pin the launcher too: verify/info must stay cool for the rest of this disc.
        single = {min(mask)}
        try:
            if not hot:
                os.sched_setaffinity(0, single)
                hot = True
                progress('Thermal limit: reducing this disc to one CPU (80°C or above).')
            os.sched_setaffinity(pid, single)
            with os.scandir(f'/proc/{pid}/task') as tasks:
                for task in tasks:
                    try:
                        os.sched_setaffinity(int(task.name), single)
                    except ProcessLookupError:
                        pass  # A short-lived helper exited.
        except ProcessLookupError:
            pass  # The child exited; the next child inherits the single-CPU mask.
        except FileNotFoundError:
            if not Path('/proc/self/task').is_dir():  # procfs unavailable, not just an exited child
                fail_hot()
        except (AttributeError, OSError):
            fail_hot()

    _worker.throttle = throttle
    try:
        if hot and mask is None:
            fail_hot()
        yield threads
    finally:
        del _worker.throttle
        if allowed is not None and mask is not None and (allowed != mask or hot):
            try:
                os.sched_setaffinity(0, allowed)
            except (AttributeError, OSError):
                pass  # CPU hotplug/cpuset change: keep the kernel's current, narrower mask.


def throttle_child(pid: int) -> None:
    """Polled by the same worker monitoring its live chdman; desktop policy is unchanged."""
    throttle = getattr(_worker, 'throttle', None)
    if throttle is not None:
        throttle(pid)


def _suspend_clock_offset() -> float:
    # CLOCK_BOOTTIME advances in suspend; CLOCK_MONOTONIC does not. Also works
    # without a session bus or logind (e.g. stripped-down SBC distributions).
    if hasattr(time, 'CLOCK_BOOTTIME'):
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
    return 0.0


class Watchdog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sleeping = False
        self._resume_at = 0.0
        self._offset = _suspend_clock_offset()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listener: asyncio.Task[None] | None = None
        self._reader: threading.Thread | None = None

    def __enter__(self) -> Watchdog:
        if sys.platform.startswith('linux'):
            self._loop = asyncio.new_event_loop()
            self._listener = self._loop.create_task(self._listen())
            self._reader = threading.Thread(target=self._run_listener, daemon=True)
            self._reader.start()
        return self

    def _run_listener(self) -> None:
        assert self._loop is not None and self._listener is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._listener)
        except asyncio.CancelledError:
            pass

    async def _listen(self) -> None:
        bus = None
        try:
            from dbus_next import BusType, Message, MessageType
            from dbus_next.aio import MessageBus

            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            interface = 'org.freedesktop.login1.Manager'
            path = '/org/freedesktop/login1'
            match = (f"type='signal',sender='org.freedesktop.login1',path='{path}',"
                     f"interface='{interface}',member='PrepareForSleep'")

            def receive(msg: Message) -> None:
                if (msg.message_type == MessageType.SIGNAL and msg.interface == interface
                        and msg.path == path and msg.member == 'PrepareForSleep' and msg.signature == 'b'):
                    self._prepare_for_sleep(msg.body[0])

            bus.add_message_handler(receive)
            reply = await bus.call(Message(destination='org.freedesktop.DBus', path='/org/freedesktop/DBus',
                                           interface='org.freedesktop.DBus', member='AddMatch',
                                           signature='s', body=[match]))
            if reply.message_type == MessageType.ERROR:
                logging.getLogger(__name__).debug('Cannot subscribe to logind: %s', reply.error_name)
                return
            await bus.wait_for_disconnect()
        except Exception as exc:
            # Not all Linux hosts run logind/a system bus. The suspend clock
            # remains active even if a running bus goes away.
            logging.getLogger(__name__).debug('System bus not available: %s', exc)
        finally:
            if bus is not None:
                bus.disconnect()

    def _prepare_for_sleep(self, entering: bool) -> None:
        with self._lock:
            self._sleeping = entering
            if not entering:
                self._resume_at = time.monotonic() + 3.0
            self._offset = _suspend_clock_offset()

    def pause_reason(self) -> str:
        with self._lock:
            offset = _suspend_clock_offset()
            if offset - self._offset > 0.25:
                self._resume_at = time.monotonic() + 3.0
                self._sleeping = False
            self._offset = offset
            if self._sleeping:
                return 'Suspend: waiting for the system to wake'
            if time.monotonic() < self._resume_at:
                return 'Resume: waiting for storage to remount'
        battery, level = _power()
        if battery and level is not None and level <= 15:
            return f'Battery at {level}%; connect power to resume'
        return ''

    def limit(self) -> int:
        return 1 if _overheated() else cpu_count()

    def __exit__(self, *exc: object) -> None:
        if self._loop is not None and self._listener is not None and self._reader is not None:
            self._loop.call_soon_threadsafe(self._listener.cancel)
            self._reader.join()
            self._loop.close()
            self._loop = self._reader = self._listener = None
