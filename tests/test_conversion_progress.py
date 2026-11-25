"""Exercise live progress and verification through a real child process and disc."""
import errno
import subprocess
import sys
import threading

import pytest

from couchchd import converter, scanner


@pytest.fixture
def disc_and_tool(tmp_path, monkeypatch):
    source = tmp_path / 'My disc.iso'
    source.write_bytes(bytes(16 * 2048) + b'\x01CD001\x01' + bytes(2048 - 7))
    game = scanner.inspect(source, 'psp', fresh=False)
    tool = tmp_path / 'native.py'
    tool.write_text(r'''
import os, sys, time
from pathlib import Path

cmd, *args = sys.argv[1:]
root = Path(__file__).parent
fault = os.environ.get('PROGRESS_TEST_FAULT', '')

def live(prefix, percent):
    # Two writes, no line feed. Do not finish the command until the user-facing
    # callback has seen the actual percentage (an exit-time log scrape fails).
    os.write(2, b'\r' + prefix + b', ' + percent[:2])
    os.write(2, percent[2:] + b'% complete... (ratio=3.3%)  \r')
    deadline = time.monotonic() + 8
    while not (root / cmd).exists():
        if time.monotonic() > deadline:
            raise RuntimeError('no live update for ' + cmd)
        time.sleep(.01)

if cmd == 'help':
    print('chdman - MAME Compressed Hunks of Data (CHD) manager 0.267')
    sys.exit(1)
if cmd == 'createdvd':
    Path(args[args.index('-o') + 1]).write_bytes(b'candidate')
    if not fault:
        live(b'Compressing', b'12.5')
        for n in range(600):
            os.write(2, b'Compressing, %d.0%% complete... (ratio=3.3%%)  \r' % (13 + n % 70))
    if fault == 'full':
        os.write(2, b'No space left on device\n' + b'x' * 200000)
        sys.exit(1)
if cmd == 'verify':
    if not fault:
        live(b'Verifying', b'65.4')
    if fault == 'bad':
        os.write(2, b'Error: mismatched hash\n')
    if fault != 'missing':
        os.write(1, b'Raw SHA1 verification successful!\n')
    os.write(2, b'x' * 200000 + b'\r')  # larger than the retained output; no LF
    os.write(1, b'Overall SHA1 verification successful!\n')
if cmd == 'info':
    print("Metadata:     Tag='DVD '  Index=0  Length=1 bytes")
    print('Logical size: 34,816 bytes\nHunk Size: 2,048 bytes\nUnit Size: 2,048 bytes\nTotal Units: 17')
''', encoding='utf-8')
    # Run the stand-in through Python, not a POSIX-only executable shebang.
    popen = subprocess.Popen
    def python_child(argv, **kwargs):
        return popen([sys.executable, *argv], **kwargs)
    monkeypatch.setattr(converter.subprocess, 'Popen', python_child)
    return game, tool


def test_carriage_return_updates_visible_before_create_and_verify_finish(disc_and_tool):
    game, tool = disc_and_tool
    messages = []
    def show(message):
        messages.append(message)
        if message == 'Compressing: My disc.iso (12.5%)':
            (tool.parent / 'createdvd').touch()
        if message == 'Verifying: My disc.iso (65.4%)':
            (tool.parent / 'verify').touch()
    output = game.source.with_suffix('.chd')
    converter.convert(game, output, tool, cancel=threading.Event(), threads=1, progress=show)
    assert output.read_bytes() == b'candidate'
    assert messages.index('Compressing: My disc.iso') < messages.index('Compressing: My disc.iso (12.5%)')
    assert messages.index('Verifying: My disc.iso') < messages.index('Verifying: My disc.iso (65.4%)')
    assert len(messages) < 20  # a burst of native CR updates does not flood the UI
    assert all('3.3%' not in message and '100%' not in message for message in messages)


@pytest.mark.parametrize('fault', ['bad', 'missing', 'full'])
def test_noisy_native_output_cannot_hide_failed_verification_or_full_disk(disc_and_tool, monkeypatch, fault):
    game, tool = disc_and_tool
    output = game.source.with_suffix('.chd')
    original = game.source.read_bytes()
    cancel = threading.Event()
    monkeypatch.setenv('PROGRESS_TEST_FAULT', fault)
    with pytest.raises(OSError if fault == 'full' else ValueError) as failure:
        converter.convert(game, output, tool, cancel=cancel, threads=1)
    if fault == 'full':
        assert failure.value.errno == errno.ENOSPC and cancel.is_set()
    assert not output.exists() and not list(tool.parent.rglob('*.part'))
    assert game.source.read_bytes() == original


@pytest.mark.parametrize('abort', ['cancel', 'callback_error'])
def test_interrupted_live_verification_never_publishes(disc_and_tool, abort):
    game, tool = disc_and_tool
    output = game.source.with_suffix('.chd')
    original = game.source.read_bytes()
    cancel = threading.Event()
    def show(message):
        if message == 'Compressing: My disc.iso (12.5%)':
            (tool.parent / 'createdvd').touch()
        if message == 'Verifying: My disc.iso (65.4%)':
            if abort == 'cancel':
                cancel.set()
            else:
                raise RuntimeError('progress callback failed')
    with pytest.raises(InterruptedError if abort == 'cancel' else RuntimeError,
                       match='cancelled' if abort == 'cancel' else 'callback failed'):
        converter.convert(game, output, tool, cancel=cancel, threads=1, progress=show)
    assert game.source.read_bytes() == original
    assert not output.exists() and not list(tool.parent.rglob('*.part'))
