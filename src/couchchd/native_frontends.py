"""Install one GUI entry in a native frontend's configured shell-launching system."""
from __future__ import annotations

import os
from pathlib import Path
import xml.etree.ElementTree as ET

from . import profiles
from .menu_entry import install_script

_BATOCERA_CONFIG = Path('/usr/share/emulationstation/es_systems.cfg')
_ROCKNIX_CONFIG = Path('/usr/config/emulationstation/es_systems.cfg')
_RETROPIE_CONFIG = Path('/etc/emulationstation/es_systems.cfg')
_RETROPIE_USER = Path('/opt/retropie/configs/all/emulationstation')


def _unlinked(path: Path) -> Path:
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError(f'Expected an absolute path without ..: {path}')
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError(f'Refusing symlink: {part}')
    return path


def _systems(path: Path) -> list[ET.Element]:
    if not _unlinked(path).is_file():
        raise ValueError(f'Missing frontend configuration: {path}')
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f'Invalid frontend configuration {path}: {exc}') from exc
    if root.tag != 'systemList':
        raise ValueError(f'Expected <systemList> in {path}')
    return root.findall('system')


def _field(node: ET.Element, name: str) -> str:
    return (node.findtext(name) or '').strip()


def _appliance_ports(user: Path, stock: Path, ports: Path) -> None:
    """Verify the distro-defined Ports system. Never redirect a root install via XML."""
    _unlinked(user)
    custom = user / 'es_systems_custom.cfg'
    if _unlinked(custom).exists():
        base = custom
    else:
        base = user / 'es_systems.cfg'
        # ROCKNIX ships a link to the stock file. Only recognize that exact
        # alias; open the independently known stock path, not the link.
        if base.is_symlink():
            if base.readlink() != stock:
                raise ValueError(f'Unrecognized frontend configuration link: {base}')
            base = stock
        elif not base.exists():
            base = stock
    systems = [node for node in _systems(base) if _field(node, 'name') == 'ports']
    if len(systems) != 1:
        raise ValueError(f'No unambiguous Ports system in {base}')
    node = systems[0]
    if (_field(node, 'path') != str(ports) or '.sh' not in _field(node, 'extension').split()
            or not _field(node, 'command')):
        raise ValueError(f'The Ports system in {base} does not launch .sh entries at {ports}')
    if base != stock:
        originals = [n for n in _systems(stock) if _field(n, 'name') == 'ports']
        if not any(_field(n, 'command') == _field(node, 'command') for n in originals):
            raise ValueError(f'Unsupported custom Ports launch command in {base}')
    # Batocera/ROCKNIX can overlay individual system fields. Refuse ambiguous
    # modifications rather than silently placing an entry ES will not launch.
    for directory in (user, stock.parent):
        for overlay in _unlinked(directory).glob('es_systems_*.cfg'):
            if overlay == base:
                continue
            for change in _systems(overlay):
                if (_field(change, 'name') == 'ports'
                        and any(change.find(k) is not None for k in ('path', 'extension', 'command', 'emulators'))):
                    raise ValueError(f'Custom Ports system in {overlay}; automatic registration skipped')


def _retropie(root: Path) -> tuple[Path, str]:
    home = Path.home()
    user = _unlinked(home) / '.emulationstation'
    # RetroPie-Setup moveConfigDir makes ~/.emulationstation a symlink.
    if user.is_symlink():
        if user.readlink() != _RETROPIE_USER:
            raise ValueError(f'Unrecognized frontend configuration link: {user}')
        user = _RETROPIE_USER
    config = _unlinked(user / 'es_systems.cfg')
    systems = _systems(config if config.exists() else _RETROPIE_CONFIG)
    for name in ('retropie', 'ports'):
        nodes = [n for n in systems if _field(n, 'name') == name]
        if len(nodes) != 1:
            continue
        node = nodes[0]
        if '.sh' not in _field(node, 'extension').split():
            continue
        directory = Path(_field(node, 'path')).expanduser()
        command = _field(node, 'command').split()
        if name == 'retropie':
            # The stock module dispatches *.sh via sudo -u <frontend user> bash.
            if (directory == home / 'RetroPie/retropiemenu'
                    and len(command) >= 5 and command[0] == 'sudo'
                    and Path(command[1]).name == 'retropie_packages.sh'
                    and command[2:5] == ['retropiemenu', 'launch', '%ROM%']):
                return directory, 'RetroPie configuration menu'
        elif (directory in (root / 'ports', home / 'RetroPie/roms/ports')
              and command in (['bash', '%ROM%'], ['/bin/bash', '%ROM%'], ['%ROM%'])):
            return directory, 'Ports'
    raise ValueError('No compatible RetroPie configuration menu or shell-launching Ports system in es_systems.cfg')


def register(launcher: Path, frontend: str, *, roms: Path | None = None) -> list[str]:
    """Register without restarting ES or editing a distro-owned config or gamelist."""
    if not _unlinked(launcher).is_file() or not os.access(launcher, os.X_OK):
        raise ValueError(f'Not an executable launcher: {launcher}')
    default = profiles.native_root(frontend)
    root = _unlinked((roms or default).expanduser().absolute())
    if frontend == 'batocera':
        directory = default / 'ports'
        _appliance_ports(default.parent / 'system/configs/emulationstation', _BATOCERA_CONFIG, directory)
        refresh = 'In Game Settings, refresh/update the game lists, then open Ports.'
    elif frontend == 'rocknix':
        directory = default / 'ports'
        _appliance_ports(default.parent / '.config/emulationstation', _ROCKNIX_CONFIG, directory)
        refresh = 'Select Game Settings → Update Gamelists, then open Ports.'
    else:  # RetroPie
        directory, menu = _retropie(root)
        refresh = f'Restart EmulationStation from its Quit menu, then open {menu}.'
    if not root.is_dir() or not _unlinked(directory.parent).is_dir():
        raise ValueError(f'ROM library or frontend menu parent is missing: {root}, {directory.parent}')
    entry = directory / 'CouchCHD.sh'
    install_script(entry, launcher, args=('--root', str(root)))
    return [f'Installed {entry}. {refresh}']
