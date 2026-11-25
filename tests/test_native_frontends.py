"""Exercise the actual generated menu script against an isolated frontend layout."""
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import pytest

from couchchd import native_frontends as native, profiles


pytestmark = pytest.mark.skipif(os.name == 'nt', reason='Linux frontend launch scripts')


def system_config(path, *systems):
    node = ET.Element('systemList')
    for name, folder, ext, command in systems:
        system = ET.SubElement(node, 'system')
        for key, value in [('name', name), ('path', str(folder)), ('extension', ext), ('command', command)]:
            ET.SubElement(system, key).text = value
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(node).write(path, encoding='utf-8')


@pytest.fixture
def launcher(tmp_path):
    exe = tmp_path / "app dir 'quoted'" / 'couchchd'
    exe.parent.mkdir()
    exe.write_text('#!/bin/sh\nprintf "%s\\n" "$@" "${QT_QPA_PLATFORM-unset}"\n')
    exe.chmod(0o755)
    return exe


def launch(entry, **display):
    env = {k: v for k, v in os.environ.items() if k not in ('DISPLAY', 'WAYLAND_DISPLAY', 'QT_QPA_PLATFORM')}
    return subprocess.check_output([entry], env=env | display, text=True).splitlines()


@pytest.fixture(params=['rocknix', 'batocera'])
def appliance(tmp_path, monkeypatch, request):
    frontend = request.param
    default = tmp_path / frontend / 'roms'
    default.mkdir(parents=True)
    stock = tmp_path / 'usr/config/emulationstation/es_systems.cfg'
    system_config(stock, ('ports', default / 'ports', '.sh .squashfs', 'emulatorlauncher -rom %ROM%'))
    monkeypatch.setattr(profiles, '_' + frontend.upper() + '_ROMS', default)
    monkeypatch.setattr(native, '_' + frontend.upper() + '_CONFIG', stock)
    if frontend == 'rocknix':
        user = default.parent / '.config/emulationstation'
        user.mkdir(parents=True)
        (user / 'es_systems.cfg').symlink_to(stock)
    else:
        user = default.parent / 'system/configs/emulationstation'
    return frontend, default, user


def test_appliance_shell_contract(appliance, launcher):
    frontend, default, _ = appliance
    assert default in profiles.roots()
    library = default.parent / 'other roms'
    library.mkdir()
    for _ in range(2):
        message, = native.register(launcher, frontend, roms=library)
        assert 'Ports' in message
    entry = default / 'ports/CouchCHD.sh'
    assert launch(entry) == ['--root', str(library), 'eglfs']
    assert launch(entry, WAYLAND_DISPLAY='wayland-0') == ['--root', str(library), 'unset']
    assert launch(entry, QT_QPA_PLATFORM='xcb')[-1] == 'xcb'
    assert not (library / 'ports').exists()


def test_appliance_keeps_existing_entry(appliance, launcher):
    frontend, root, _ = appliance
    entry = root / 'ports/CouchCHD.sh'
    entry.parent.mkdir()
    entry.write_text('my program')
    with pytest.raises(FileExistsError):
        native.register(launcher, frontend)
    assert entry.read_text() == 'my program'


def test_appliance_refuses_untrusted_port_overlay(appliance, launcher):
    frontend, root, user = appliance
    system_config(user / 'es_systems_local.cfg', ('ports', root.parent, '.sh', '%ROM%'))
    with pytest.raises(ValueError, match='Custom Ports system'):
        native.register(launcher, frontend)
    assert not (root / 'ports').exists()


def test_missing_ports_system_is_not_installed(appliance, launcher):
    frontend, root, user = appliance
    system_config(user / 'es_systems_custom.cfg', ('psx', root / 'psx', '.cue', '%ROM%'))
    with pytest.raises(ValueError, match='Ports system'):
        native.register(launcher, frontend)
    assert not (root / 'ports').exists()


def test_symlinked_menu_and_library_refused(appliance, launcher):
    frontend, root, _ = appliance
    other = root.parent / 'elsewhere'
    other.mkdir()
    (root / 'ports').symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError):
        native.register(launcher, frontend)
    assert not list(other.iterdir())
    with pytest.raises(ValueError, match='symlink'):
        native.register(launcher, frontend, roms=root / 'ports')


@pytest.fixture
def retropie(tmp_path, monkeypatch):
    home = tmp_path / 'home/alice'
    root = home / 'RetroPie/roms'
    root.mkdir(parents=True)
    user = tmp_path / 'opt/retropie/configs/all/emulationstation'
    user.mkdir(parents=True)
    (home / '.emulationstation').symlink_to(user, target_is_directory=True)
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setattr(native, '_RETROPIE_USER', user)
    stock = tmp_path / 'etc/emulationstation/es_systems.cfg'
    monkeypatch.setattr(native, '_RETROPIE_CONFIG', stock)
    menu = home / 'RetroPie/retropiemenu'
    system_config(stock, ('retropie', menu, '.rp .sh',
                          f'sudo {home}/RetroPie-Setup/retropie_packages.sh retropiemenu launch %ROM%'))
    return root, menu, user / 'es_systems.cfg'


def test_retropie_core_configuration_menu(retropie, launcher):
    root, menu, _ = retropie
    assert root in profiles.roots()
    assert 'RetroPie configuration menu' in native.register(launcher, 'retropie')[0]
    assert launch(menu / 'CouchCHD.sh', DISPLAY=':0') == ['--root', str(root), 'unset']


def test_retropie_prefers_active_user_config(retropie, launcher):
    root, menu, config = retropie
    system_config(config, ('ports', root / 'ports', '.sh', 'bash %ROM%'))
    assert 'Ports' in native.register(launcher, 'retropie')[0]
    assert launch(root / 'ports/CouchCHD.sh') == ['--root', str(root), 'eglfs']
    assert not menu.exists()


@pytest.mark.parametrize('extension,command', [('.rp', 'bash %ROM%'), ('.sh', 'retroarch %ROM%')])
def test_retropie_refuses_unlaunchable_menu(retropie, launcher, extension, command):
    root, menu, config = retropie
    system_config(config, ('ports', root / 'ports', extension, command))
    with pytest.raises(ValueError, match='No compatible'):
        native.register(launcher, 'retropie')
    assert not menu.exists() and not (root / 'ports').exists()
