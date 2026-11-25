"""Exercise the persistent Recalbox layout and the command ES runs, without a display."""
import os
import shlex
import stat
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from couchchd import recalbox_menu
from couchchd.cli import main
from couchchd.recalbox_menu import refresh_artwork, register
from couchchd.ui import launcher_art


pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Recalbox installer targets Linux')


def test_register_port_and_run_without_emulator_flags(tmp_path):
    share = tmp_path / 'my SHARE & media'
    register(share)
    config = share / 'system/.emulationstation/systemlist.xml'
    system = ET.parse(config).find("system[@name='couchchd']")
    desc = system.find('descriptor')
    assert desc.get('port') == '1' and desc.get('extensions') == '.game'
    assert system.find('emulatorList/emulator/core').get('extensions') == '.game'
    roms = share / 'roms' / desc.get('path').removeprefix('%ROOT%/')
    marker = roms / ET.parse(roms / 'gamelist.xml').findtext('game/path')
    assert marker.is_file() and not marker.stat().st_mode & 0o111
    cover = roms / ET.parse(roms / 'gamelist.xml').findtext('game/image')
    assert cover.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    launcher = share / 'system/couchchd/run-couchchd'
    launcher.parent.mkdir()
    launcher.write_text('#!/bin/sh\nprintf "started:%s\\n" "$#"\n')
    launcher.chmod(0o755)
    command = desc.get('command')

    def run(*flags):
        return subprocess.check_output(['/bin/sh', '-c', command + ' ' + shlex.join(flags)], text=True)

    assert run('-rotation', '0', '-systemtype', 'port') == 'started:0\n'
    assert run('-rotation', '0', '-systemtype', 'port', '-demo', '1') == ''


def test_preserve_user_configuration_and_reinstall(tmp_path):
    config = tmp_path / 'system/.emulationstation/systemlist.xml'
    config.parent.mkdir(parents=True)
    before = b'<?xml version="1.0"?>\r\n<systemList>\r\n  <!-- personal order -->\r\n  <system uuid="my-system"/>\r\n'
    after = b'</systemList>\r\n<!-- </systemList> -->\r\n'
    config.write_bytes(before + after)
    config.chmod(0o640)
    register(tmp_path)
    result = config.read_bytes()
    assert result.startswith(before) and result.endswith(after)
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    games = tmp_path / 'roms/ports/couchchd/gamelist.xml'
    edited = games.read_bytes().replace(b'<name>CouchCHD', b'<name>My copy of CouchCHD')
    games.write_bytes(edited)
    register(tmp_path)
    assert config.read_bytes() == result and games.read_bytes() == edited
    assert len(ET.fromstring(result).findall("system[@name='couchchd']")) == 1


@pytest.mark.parametrize('before', [b'<systemList/>', b'<systemList /> <!-- </systemList> -->'])
def test_empty_custom_system_list(tmp_path, before):
    config = tmp_path / 'system/.emulationstation/systemlist.xml'
    config.parent.mkdir(parents=True)
    config.write_bytes(before)
    register(tmp_path)
    assert ET.parse(config).find("system[@name='couchchd']") is not None


@pytest.mark.parametrize(('relative', 'data'), [
    ('system/.emulationstation/systemlist.xml', b'<broken'),
    ('system/.emulationstation/systemlist.xml', b'<systemList><system name="couchchd"/></systemList>'),
    ('system/.emulationstation/systemlist.xml',
     b'<systemList><system name="tools"><descriptor path="%ROOT%/ports/couchchd"/></system></systemList>'),
    ('roms/ports/couchchd/CouchCHD.game', b'my game'),
    ('roms/ports/couchchd/gamelist.xml', b'<gameList><game><path>my.game</path></game></gameList>'),
])
def test_refuse_conflicts_without_overwriting(tmp_path, relative, data):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    with pytest.raises(ValueError):
        register(tmp_path)
    assert path.read_bytes() == data
    assert {p for p in tmp_path.rglob('*') if p.is_file()} == {path}


@pytest.mark.parametrize('relative', ['roms/ports', 'system/.emulationstation/systemlist.xml'])
def test_refuse_linked_destinations(tmp_path, relative):
    share = tmp_path / 'share'
    link = share / relative
    link.parent.mkdir(parents=True)
    other = tmp_path / 'other'
    other.mkdir()
    link.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError):
        register(share)
    assert link.is_symlink() and not list(other.iterdir())


def test_migrate_preview4_gamelist_only_after_frontend_exit(tmp_path, monkeypatch):
    register(tmp_path)
    roms = tmp_path / 'roms/ports/couchchd'
    cover = roms / 'media/CouchCHD.png'
    gamelist = roms / 'gamelist.xml'
    old = b'<gameList><game><path>./CouchCHD.game</path><name>CouchCHD</name></game></gameList>\n'
    gamelist.write_bytes(old)
    cover.unlink()
    monkeypatch.setattr(recalbox_menu, '_frontend_stopped', lambda: False)
    assert 'stop EmulationStation' in refresh_artwork(tmp_path)
    assert gamelist.read_bytes() == old and not cover.exists()
    monkeypatch.setattr(recalbox_menu, '_frontend_stopped', lambda: True)
    assert 'installed' in refresh_artwork(tmp_path)
    assert roms / ET.parse(gamelist).findtext('game/image') == cover
    cover.write_bytes(b'custom replacement')
    refresh_artwork(tmp_path)
    assert cover.read_bytes() == b'custom replacement'
    customized = old.replace(b'</game>', b'<playcount>7</playcount></game>')
    gamelist.write_bytes(customized)
    cover.unlink()
    assert 'customized' in refresh_artwork(tmp_path)
    assert gamelist.read_bytes() == customized and not cover.exists()
    gamelist.write_bytes(old)
    (roms / 'gamelist.zip').write_bytes(b'frontend archive')
    assert 'archived' in refresh_artwork(tmp_path)
    assert gamelist.read_bytes() == old and not cover.exists()


def test_refresh_never_creates_missing_port(tmp_path):
    assert 'No registered' in refresh_artwork(tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.fixture
def device(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    share = root / 'recalbox/share'
    media = root / 'media'
    disks = (share, share.parent / 'share_int', media / 'Local Disk')
    for disk in disks:
        disk.mkdir(parents=True)
    (share / 'system').mkdir()
    monkeypatch.setattr(recalbox_menu, 'SHARE', share)
    monkeypatch.setattr(recalbox_menu, '_MEDIA', media)
    monkeypatch.setattr(Path, 'is_mount', lambda path: path in disks)
    return share, disks


def _install_wrapper(base):
    base.mkdir(parents=True, exist_ok=True)
    launcher = base / 'run-couchchd'
    launcher.write_text('#!/bin/sh\nprintf "%s:%s\\n" "$0" "$#"\n')
    launcher.chmod(0o755)
    return launcher


@pytest.mark.parametrize('index', [0, 1, 2])
def test_retarget_managed_port_and_refresh_shared_artwork(device, index):
    share, disks = device
    config = share / 'system/.emulationstation/systemlist.xml'
    config.parent.mkdir()
    before = b'<systemList><!-- personal --><system name="keep"/>'
    after = b'</systemList><!-- keep this too -->'
    config.write_bytes(before + after)
    # Use the original CLI contract; an already-installed port may now point
    # at a noexec SHARE. No need to execute its old wrapper to retarget it.
    original = _install_wrapper(share / 'system/couchchd')
    assert main(['--register-frontend', 'recalbox', str(original)]) == 0
    launcher = _install_wrapper(disks[index] / 'couchchd')
    assert main(['--register-frontend', 'recalbox', str(launcher)]) == 0
    registered = config.read_bytes()
    assert registered.startswith(before) and registered.endswith(after)
    command, = [d.get('command') for d in ET.fromstring(registered).findall('system/descriptor')]
    assert subprocess.check_output(['/bin/sh', '-c', command + ' -systemtype port'], text=True) == f'{launcher}:0\n'
    assert main(['--register-frontend', 'recalbox', str(launcher)]) == 0 and config.read_bytes() == registered
    roms = share / 'roms/ports/couchchd'
    assert (roms / 'CouchCHD.game').is_file() and (roms / 'gamelist.xml').is_file()
    cover = roms / 'media/CouchCHD.png'
    cover.unlink()
    assert 'installed' in launcher_art.refresh(launcher) and cover.is_file()
    if disks[index] != share:
        assert not (disks[index] / 'roms').exists() and not (disks[index] / 'system').exists()


def test_relocation_leaves_custom_descriptor_unchanged(device):
    share, disks = device
    register(share)
    config = share / 'system/.emulationstation/systemlist.xml'
    custom = config.read_bytes().replace(b'theme="ports"', b'theme="mine"')
    config.write_bytes(custom)
    launcher = _install_wrapper(disks[2] / 'couchchd')
    with pytest.raises(ValueError, match='Conflicting'):
        register(launcher=launcher)
    assert config.read_bytes() == custom


def test_refuse_unmounted_nested_and_linked_install_locations(device):
    share, disks = device
    alias = disks[2].parent / 'alias'
    alias.symlink_to(disks[2], target_is_directory=True)
    for location in (alias, disks[2].parent / 'unmounted', disks[2] / 'subdir', share.parent / 'boot'):
        launcher = _install_wrapper(location / 'couchchd')
        assert main(['--register-frontend', 'recalbox', str(launcher)]) == 1
    assert not (share / 'system/.emulationstation').exists()
