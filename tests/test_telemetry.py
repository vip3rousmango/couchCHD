import os
import sys

import pytest

from couchchd import telemetry


def test_process_cpu_uses_all_threads_and_device_capacity(tmp_path, monkeypatch):
    stat = tmp_path / 'stat'
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(os, 'sysconf', lambda key: 100, raising=False)
    def path(filename):
        assert filename == '/proc/42/stat'
        return stat
    monkeypatch.setattr(telemetry, 'Path', path)
    def record(user, kernel):
        # comm is not a whitespace-delimited field. Fields 3..13 precede
        # aggregate user and system CPU; guest/children must not be added.
        stat.write_bytes(b'42 (a ) process \xff)) S ' + b'0 ' * 10 + f'{user} {kernel} 777 888'.encode())
    record(40, 10)
    sampler = telemetry.StageSampler(42, 8, 100 * 1024**2, 100.)
    record(140, 70)
    assert sampler.sample(10., 101.) == (None, 20.)  # 1.6 CPU seconds / 8 available cores
    record(290, 80)
    assert sampler.sample(30., 103.) == (10., 10.)  # 20 logical MiB in 2s
    assert sampler.sample(30., 104.) == (None, 0.)  # unchanged marker is not fresh speed
    assert sampler.sample(5., 105.) == (None, 0.)   # native pass restarted
    stat.unlink()
    assert sampler.sample(15., 107.) == (5., None)  # no fabricated or stale CPU after exit
    stat.write_bytes(b'malformed')
    assert sampler.sample(15., 108.) == (None, None)
    record(700, 80)
    assert sampler.sample(15., 109.) == (None, None)  # missing intermediate counter: rebaseline
    record(1, 0)
    assert sampler.sample(15., 110.) == (None, None)  # regressing counter is not negative CPU


@pytest.mark.parametrize(('platform', 'cores', 'clock'), [('darwin', 8, 100), ('linux', 0, 100), ('linux', 8, -1)])
def test_unsupported_cpu_never_reads_proc_or_guesses_usage(monkeypatch, platform, cores, clock):
    monkeypatch.setattr(sys, 'platform', platform)
    monkeypatch.setattr(os, 'sysconf', lambda _: clock, raising=False)
    monkeypatch.setattr(telemetry, 'Path', lambda _: pytest.fail('unexpected procfs lookup'))
    meter = telemetry.StageSampler(42, cores, 100 * 1024**2, 10.)
    assert meter.sample(30., 11.) == (None, None)
    assert meter.sample(50., 13.) == (10., None)
