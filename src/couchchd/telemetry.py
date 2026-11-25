"""Per-source progress snapshots and low-frequency native process measurements."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ItemProgress:
    key: str
    name: str
    system: str
    stage: str
    percent: float | None = None
    mib_s: float | None = None
    cpu_percent: float | None = None
    detail: str = ''


class StageSampler:
    """One sampler per native command; all rates are measured, never a CPU budget."""

    def __init__(self, pid: int, device_cpus: int, logical_bytes: int, now: float):
        self.mib_per_percent = logical_bytes / (100 * 1024**2)
        self.percent = None
        self.percent_at = now
        self.cpu_at = now
        self.stat = None
        if sys.platform.startswith('linux') and device_cpus > 0:
            try:
                ticks = os.sysconf('SC_CLK_TCK')
                if ticks > 0:
                    self.ticks_per_second = ticks * device_cpus
                    self.stat = Path(f'/proc/{pid}/stat')
            except (AttributeError, OSError, ValueError):
                pass
        self.ticks = self._cpu_ticks()

    def _cpu_ticks(self) -> int | None:
        if self.stat is not None:
            try:
                # /proc/PID/stat sums all native threads (no /task walk). comm
                # can contain whitespace, parentheses and non-UTF-8 bytes.
                fields = self.stat.read_bytes().rsplit(b')', 1)[1].split()
                return int(fields[11]) + int(fields[12])  # utime + stime
            except (IndexError, OSError, ValueError):
                pass  # procfs absent/restricted, malformed, or child already exited
        return None

    def sample(self, percent: float | None, now: float) -> tuple[float | None, float | None]:
        speed = cpu = None
        if percent is not None and percent != self.percent:
            if self.percent is not None and percent > self.percent and now > self.percent_at:
                speed = (percent - self.percent) * self.mib_per_percent / (now - self.percent_at)
            self.percent, self.percent_at = percent, now
        ticks = self._cpu_ticks()
        if ticks is not None and self.ticks is not None and ticks >= self.ticks and now > self.cpu_at:
            cpu = min(100.0, 100 * (ticks - self.ticks) / ((now - self.cpu_at) * self.ticks_per_second))
        self.ticks, self.cpu_at = ticks, now
        return speed, cpu
