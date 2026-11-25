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
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    launcher = tmp_path / 'a %u "folder"/run-couchchd'
    entry = install_desktop(launcher)
    assert entry.parent == tmp_path / 'applications'
    assert r'%%u \\"folder\\"/run-couchchd"' in entry.read_text()


def test_migrate_only_exact_managed_desktop_and_keep_replacement_icon(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    launcher = tmp_path / 'My Apps/run-couchchd'
    assert 'No registered' in refresh_desktop_artwork(launcher)
    assert not list(tmp_path.iterdir())
    name = 'io.github.vip3rousmango.CouchCHD'
    entry = tmp_path / 'applications' / (name + '.desktop')
    entry.parent.mkdir()
    legacy = ('[Desktop Entry]\nType=Application\nName=CouchCHD\n'
              f'Exec="{launcher}"\nIcon=applications-games\nTerminal=false\nCategories=Game;Utility;\n')
    entry.write_text(legacy)
    entry.chmod(0o640)
    refresh_desktop_artwork(launcher)
    assert entry.read_text() == legacy.replace('Icon=applications-games', f'Icon={name}')
    assert entry.stat().st_mode & 0o777 == 0o640
    icon = tmp_path / f'icons/hicolor/scalable/apps/{name}.svg'
    assert '<svg' in icon.read_text()
    icon.write_bytes(b'custom artwork')
    refresh_desktop_artwork(launcher)
    assert icon.read_bytes() == b'custom artwork'
    entry.write_text(legacy + 'Actions=custom;\n')
    icon.unlink()
    assert 'Customized' in refresh_desktop_artwork(launcher)
    assert install_desktop(launcher) == entry and not icon.exists()
    assert entry.read_text() == legacy + 'Actions=custom;\n'
