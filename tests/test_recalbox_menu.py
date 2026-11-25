"""Exercise the persistent Recalbox layout and the command ES runs, without a display."""
import os
import shlex
import stat
import subprocess
from xml.etree import ElementTree as ET

import pytest

from couchchd.recalbox_menu import register


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
