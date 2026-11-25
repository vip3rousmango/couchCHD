"""Desktop installer integration: do not use the user's Steam data or a live display."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from couchchd import cli

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='native Linux Steam installer')


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / '.local/share'))
    for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'QT_QPA_PLATFORM', 'SteamAppId', 'FLATPAK_ID'):
        monkeypatch.delenv(key, raising=False)
    launcher = tmp_path / 'CouchCHD tools/run-couchchd'
    launcher.parent.mkdir()
    launcher.touch(mode=0o755)
    return launcher


def install(launcher):
    return cli.main(['--register-frontend', 'desktop', str(launcher)])


def test_installer_registers_stable_launcher_with_art_without_display(desktop, monkeypatch):
    vdf = pytest.importorskip('vdf')
    from couchchd.ui import steam
    config = Path.home() / '.local/share/Steam/userdata/123/config'
    config.mkdir(parents=True)
    monkeypatch.setenv('APPIMAGE', str(Path.home() / 'downloaded.AppImage'))
    env = dict(os.environ, PYTHONPATH=str(Path(cli.__file__).resolve().parents[1]))
    result = subprocess.run([sys.executable, '-m', 'couchchd', '--register-frontend', 'desktop', str(desktop)],
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    entry = vdf.binary_loads((config / 'shortcuts.vdf').read_bytes())['shortcuts']['0']
    assert entry['Exe'] == f'"{desktop}"'
    assert entry['StartDir'] == f'"{desktop.parent}"'
    assert entry['LaunchOptions'] == ''
    grid = config / 'grid'
    appid = entry['appid'] & 0xffffffff
    assert entry['icon'] == str(grid / f'{appid}_icon.png')
    for suffix, name, size in (('p', 'grid', (600, 900)), ('', 'landscape', (920, 430)),
                               ('_hero', 'hero', (1920, 620)), ('_logo', 'logo', (640, 240)),
                               ('_icon', 'icon', (256, 256))):
        art = (grid / f'{appid}{suffix}.png').read_bytes()
        assert art == (steam.ASSETS / f'{name}.png').read_bytes()
        assert art[:8] == b'\x89PNG\r\n\x1a\n'
        assert tuple(int.from_bytes(art[i:i+4], 'big') for i in (16, 20)) == size


@pytest.mark.parametrize('layout', ['absent', 'flatpak-only', 'ambiguous'])
def test_skips_without_an_identifiable_native_account(desktop, capsys, layout):
    pytest.importorskip('vdf')
    steam = Path.home() / ('.var/app/com.valvesoftware.Steam/.local/share/Steam' if layout == 'flatpak-only'
                          else '.local/share/Steam')
    for user in {'absent': (), 'flatpak-only': ('123',), 'ambiguous': ('123', '456')}[layout]:
        (steam / f'userdata/{user}/config').mkdir(parents=True)
    assert install(desktop) == 0
    assert 'Steam' in capsys.readouterr().out
    assert list((Path.home() / '.local/share/applications').glob('*.desktop'))
    assert not list(steam.rglob('shortcuts.vdf'))
    assert not list(steam.rglob('grid'))


@pytest.mark.parametrize('terminal,exits', [(True, True), (True, False), (False, False)])
def test_live_steam_gets_bounded_visible_exit_or_retry(desktop, monkeypatch, capsys, terminal, exits):
    pytest.importorskip('vdf')
    from couchchd.ui import steam
    elapsed = 0
    registered = []

    def register(launcher):
        if not exits or not elapsed:
            raise steam.SteamRunning('client is live')
        registered.append(launcher)
        return 'Steam shortcut ready'

    def advance(seconds):
        nonlocal elapsed
        assert seconds > 0
        elapsed += seconds
        assert elapsed <= 120, 'installer waited indefinitely'

    monkeypatch.setattr(steam, 'register', register)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)  # curl | sh owns stdin
    monkeypatch.setattr(sys.stderr, 'isatty', lambda: terminal)
    monkeypatch.setattr(cli.time, 'monotonic', lambda: elapsed)
    monkeypatch.setattr(cli.time, 'sleep', advance)
    assert install(desktop) == 0
    out, err = capsys.readouterr()
    assert 'Steam > Exit' in err and 'Desktop Mode' in err
    assert (elapsed > 0) == terminal
    if exits:
        assert registered == [desktop]
        assert 'Steam shortcut ready' in out
    else:
        assert not registered
        assert 'same install command' in out and 'Settings' in out and 'Add to Steam' in out
    assert list((Path.home() / '.local/share/applications').glob('*.desktop'))


def test_live_reinstall_updates_registered_grid_but_not_shortcuts(desktop, monkeypatch, capsys):
    vdf = pytest.importorskip('vdf')
    from couchchd.ui import steam
    monkeypatch.setattr(sys.stderr, 'isatty', lambda: False)
    root = Path.home() / '.local/share/Steam'
    config = root / 'userdata/123/config'
    grid = config / 'grid'
    grid.mkdir(parents=True)
    (root / 'steam.pid').write_text(str(os.getpid()))
    original = vdf.binary_dumps({'shortcuts': {'2': {
        'AppName': 'My discs', 'appid': -12, 'Exe': f'"{desktop}"', 'icon': ''}}})
    shortcuts = config / 'shortcuts.vdf'
    shortcuts.write_bytes(original)
    appid = -12 & 0xffffffff
    hero = grid / f'{appid}_hero.svg'
    hero.write_bytes((steam.ASSETS / 'preview4-hero.svg').read_bytes())
    assert install(desktop) == 0
    assert hero.read_bytes() == (steam.ASSETS / 'hero.svg').read_bytes()
    assert (grid / f'{appid}p.png').read_bytes() == (steam.ASSETS / 'grid.png').read_bytes()
    assert shortcuts.read_bytes() == original
    assert not (config / 'shortcuts.vdf.couchchd-backup').exists()
    out = capsys.readouterr().out
    assert 'registration deferred' in out and 'restart Steam' in out
