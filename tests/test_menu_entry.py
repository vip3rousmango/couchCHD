import os
from pathlib import Path
import subprocess

import pytest

from couchchd.menu_entry import install_desktop, install_script, refresh_desktop_artwork

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Linux launchers')


def test_ports_exec_preserves_paths_and_display(tmp_path):
    root = tmp_path / 'Games on SD'
    launcher = tmp_path / "CouchCHD's binary"
    launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$@" "$QT_QPA_PLATFORM"\n')
    launcher.chmod(0o755)
    entry = root / 'ports/CouchCHD.sh'
    for _ in range(2):
        install_script(entry, launcher, args=('--root', str(root)))
    env = {k:v for k,v in os.environ.items() if k not in {'DISPLAY','WAYLAND_DISPLAY','QT_QPA_PLATFORM'}}
    assert subprocess.check_output([entry], env=env, text=True).splitlines() == ['--root', str(root), 'eglfs']
    assert subprocess.check_output([entry], env=dict(env, QT_QPA_PLATFORM='wayland'), text=True).splitlines()[-1] == 'wayland'
    entry.write_text('my custom launcher')
    with pytest.raises(FileExistsError):
        install_script(entry, launcher)
    assert entry.read_text() == 'my custom launcher'


def test_does_not_publish_through_link(tmp_path):
    foreign = tmp_path / 'foreign'; foreign.mkdir()
    ports = tmp_path / 'ports'; ports.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(ValueError):
        install_script(ports/'CouchCHD.sh', Path('/usr/bin/false'))
    assert not list(foreign.iterdir())


def test_desktop_entry_escapes_field_codes_and_quotes(tmp_path, monkeypatch):
    data = tmp_path / r'my %u\data'
    monkeypatch.setenv('XDG_DATA_HOME', str(data))
    launcher = tmp_path / 'a %u "folder"/run-couchchd'
    entry = install_desktop(launcher)
    assert entry.parent == data / 'applications'
    assert f'Icon={tmp_path}/' + r'my %u\\data/couchchd/artwork/icon.png' + '\n' in entry.read_text()
    assert (data / 'couchchd/artwork/icon.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert r'%%u \\"folder\\"/run-couchchd"' in entry.read_text()


@pytest.mark.parametrize(('old_icon', 'categories'), [
    ('applications-games', 'Game;Utility;'),
    ('io.github.vip3rousmango.CouchCHD', 'Game;Utility;'),
    ('io.github.vip3rousmango.CouchCHD', 'Utility;Compression;'),
])
def test_migrate_only_exact_managed_desktop_and_keep_replacement_icon(tmp_path, monkeypatch, old_icon, categories):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    launcher = tmp_path / 'My Apps/run-couchchd'
    assert 'No registered' in refresh_desktop_artwork(launcher)
    assert not list(tmp_path.iterdir())
    name = 'io.github.vip3rousmango.CouchCHD'
    entry = tmp_path / 'applications' / (name + '.desktop')
    entry.parent.mkdir()
    legacy = ('[Desktop Entry]\nType=Application\nName=CouchCHD\n'
              f'Exec="{launcher}"\nIcon={old_icon}\nTerminal=false\nCategories={categories}\n')
    entry.write_text(legacy)
    entry.chmod(0o640)
    if old_icon == name:
        svg = tmp_path / f'icons/hicolor/scalable/apps/{name}.svg'
        svg.parent.mkdir(parents=True)
        # Exact shipped preview.7 bytes, independent of current artwork.
        svg.write_text('''<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <title>CouchCHD — optical disc</title>
  <circle cx="128" cy="128" r="122" fill="#192221" stroke="#85928a" stroke-width="3"/>
  <g fill="none" stroke="#dbf483">
    <path d="M218 166a98 98 0 1 1 0-76" stroke-width="25"/>
    <path d="M188 153a65 65 0 1 1 0-50" stroke-width="11"/>
    <circle cx="128" cy="128" r="17" stroke-width="8"/>
  </g>
</svg>
''', encoding='utf-8')
        old_svg = svg.read_bytes()
    refresh_desktop_artwork(launcher)
    icon = tmp_path / 'couchchd/artwork/icon.png'
    assert f'Icon={icon}\n' in entry.read_text() and 'Categories=Utility;Compression;\n' in entry.read_text()
    assert entry.stat().st_mode & 0o777 == 0o640
    assert icon.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    if old_icon == name:
        from couchchd.ui import __file__ as ui_file
        current_svg = Path(ui_file).parent / 'assets/icon.svg'
        assert svg.read_bytes() == current_svg.read_bytes()
        # Preview.8/.9 may already have changed Icon= while leaving this
        # linked-from-elsewhere old themed asset behind.
        svg.write_bytes(old_svg)
        install_desktop(launcher)
        assert svg.read_bytes() == current_svg.read_bytes()
    icon.write_bytes(b'custom artwork')
    mtimes = (icon.stat().st_mtime_ns, entry.stat().st_mtime_ns)
    assert str(icon) in refresh_desktop_artwork(launcher)
    assert (icon.stat().st_mtime_ns, entry.stat().st_mtime_ns) == mtimes
    assert icon.read_bytes() == b'custom artwork'
    entry.write_text(legacy + 'Actions=custom;\n')
    icon.unlink()
    assert 'Customized' in refresh_desktop_artwork(launcher)
    assert install_desktop(launcher) == entry and not icon.exists()
    assert entry.read_text() == legacy + 'Actions=custom;\n'


def test_custom_themed_icon_remains_selected_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    name = 'io.github.vip3rousmango.CouchCHD'
    svg = tmp_path / f'icons/hicolor/scalable/apps/{name}.svg'
    svg.parent.mkdir(parents=True)
    svg.write_bytes(b'personal SVG')
    launcher = tmp_path / 'run'
    entry = tmp_path / f'applications/{name}.desktop'
    entry.parent.mkdir()
    entry.write_text('[Desktop Entry]\nType=Application\nName=CouchCHD\n'
                     f'Exec="{launcher}"\nIcon={name}\nTerminal=false\nCategories=Utility;Compression;\n')
    for _ in range(2):
        assert str(svg) in refresh_desktop_artwork(launcher)
        assert f'Icon={svg}\n' in entry.read_text()
    assert svg.read_bytes() == b'personal SVG'
    assert not (tmp_path / 'couchchd/artwork/icon.png').exists()


@pytest.mark.parametrize('linked', ['icons', 'icons/hicolor/scalable/apps/io.github.vip3rousmango.CouchCHD.svg',
                                    'couchchd/artwork', 'couchchd/artwork/icon.png'])
def test_icon_links_refused_without_registering(tmp_path, monkeypatch, linked):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    link = tmp_path / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(tmp_path / 'missing')
    with pytest.raises(ValueError):
        install_desktop(tmp_path / 'run')
    assert not (tmp_path / 'applications').exists()
    assert not (tmp_path / 'missing').exists()


@pytest.mark.parametrize('xdg', [False, True])
def test_atomic_home_alias_registers_utilities_but_not_redirected_entries(tmp_path, monkeypatch, xdg):
    from couchchd import cli, menu_entry
    alias, homes = tmp_path / 'home', tmp_path / 'var/home'
    account = 'Bazzite User'
    logical, physical = alias / account, homes / account
    physical.mkdir(parents=True)
    alias.symlink_to('var/home', target_is_directory=True)
    monkeypatch.setattr(menu_entry, '_HOME_ROOT', alias)
    monkeypatch.setenv('HOME', str(logical))
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    if xdg:
        monkeypatch.setenv('XDG_DATA_HOME', str(logical / '.local/share'))
    launcher = logical / '.local/share/couchchd/run-couchchd'
    launcher.parent.mkdir(parents=True)
    launcher.touch(mode=0o755)
    assert cli.main(['--register-frontend', 'desktop', str(launcher)]) == 0
    apps = physical / '.local/share/applications'
    entry, = apps.glob('*.desktop')
    assert f'Exec="{physical}/.local/share/couchchd/run-couchchd"\n' in entry.read_text()
    assert 'Categories=Utility;Compression;' in entry.read_text()
    foreign = tmp_path / 'unrelated'
    apps.rename(foreign)
    apps.symlink_to(foreign, target_is_directory=True)
    old = (foreign / entry.name).read_bytes()
    assert cli.main(['--register-frontend', 'desktop', str(launcher)]) != 0
    assert (foreign / entry.name).read_bytes() == old
