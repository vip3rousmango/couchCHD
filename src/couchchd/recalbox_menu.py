"""Register a native Recalbox port without changing the read-only system list."""
import os
import re
import shlex
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.parsers import expat
from xml.sax.saxutils import quoteattr

from .menu_entry import _ASSETS, read_regular, store_entry
from .stock_art import upgrade_stock


SHARE = Path('/recalbox/share')
_MEDIA = Path('/media')
_MOUNT_NAME = re.compile(r'[A-Za-z0-9 ._+-]+')
_UUID = '27790d47-96e4-43be-a995-8c2ca3e6ea0d'
_ROM_PATH = '%ROOT%/ports/couchchd'
_MARKER = b'CouchCHD managed Recalbox menu entry\n'
_PREVIEW4_GAMELIST = b'<gameList><game><path>./CouchCHD.game</path><name>CouchCHD</name></game></gameList>\n'
_IMAGE = 'CouchCHD.png'
_GAMELIST = _PREVIEW4_GAMELIST.replace(b'</game>', b'<image>./media/CouchCHD.png</image></game>')


def _fallback_mount(launcher: Path) -> Path | None:
    if launcher.parts[-2:] != ('couchchd', 'run-couchchd') or '..' in launcher.parts:
        return None
    mount = launcher.parents[1]
    candidate = mount.parent == _MEDIA or (mount.parent == SHARE.parent and mount.name.startswith('share'))
    if candidate and _MOUNT_NAME.fullmatch(mount.name):
        return mount
    return None


def _launcher(share: Path, selected: Path | None) -> Path:
    default = share / 'system/couchchd/run-couchchd'
    if selected is None or selected == default:
        return default
    # The bootstrap probes executable storage and checks its filesystem and
    # permissions. Do not register a stale directory after that disk is removed.
    mount = _fallback_mount(selected)
    if (share != SHARE or not (share / 'system').is_dir() or mount is None or not mount.is_mount()
            or any(p.is_symlink() for p in (selected, *selected.parents))):
        raise ValueError(f'Not an installed Recalbox volume launcher: {selected}')
    return selected


def _entry(launcher: Path) -> bytes:
    # GameRunner appends emulator flags. Ignore those; never launch the UI for
    # a screensaver demo, and never execute the contents of a ROM-share file.
    script = ('for opt do [ "$opt" != -demo ] || exit 0; done; '
              'if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}${QT_QPA_PLATFORM:-}" ]; then '
              'export QT_QPA_PLATFORM=eglfs; fi; exec "$0"')
    command = shlex.join(('/bin/sh', '-c', script, str(launcher)))
    return f'''  <system uuid="{_UUID}" name="couchchd" fullname="CouchCHD">
    <descriptor path="{_ROM_PATH}" theme="ports" extensions=".game" port="1" command={quoteattr(command)}/>
    <properties type="port" pad="mandatory"/>
    <emulatorList><emulator name="couchchd"><core name="native" priority="1" extensions=".game"/></emulator></emulatorList>
  </system>'''.encode('ascii', errors='xmlcharrefreplace')


def _xml(data: bytes, path: Path) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f'Malformed XML in {path}: {exc}') from exc


def _signature(node: ET.Element) -> tuple:
    return node.tag, node.attrib, tuple(map(_signature, node))


def _merge(data: bytes | None, path: Path, share: Path, launcher: Path) -> bytes:
    entry = _entry(launcher)
    if data is None:
        return b'<?xml version="1.0"?>\n<systemList>\n' + entry + b'\n</systemList>\n'
    root = _xml(data, path)
    if root.tag != 'systemList':
        raise ValueError(f'Expected <systemList> in {path}')
    matches = [s for s in root.findall('system') if s.get('uuid') == _UUID or s.get('name') == 'couchchd'
               or s.find(f"descriptor[@path='{_ROM_PATH}']") is not None]
    previous = None
    if matches:
        if len(matches) != 1:
            raise ValueError(f'Conflicting CouchCHD system in {path}; leaving it unchanged')
        if _signature(matches[0]) == _signature(ET.fromstring(entry)):
            return data
        descriptor = matches[0].find('descriptor')
        command = shlex.split(descriptor.get('command', '')) if descriptor is not None else []
        if command:
            old = Path(command[-1])
            if old == share / 'system/couchchd/run-couchchd' or (share == SHARE and _fallback_mount(old)):
                # A disconnected old volume is fine. Only a byte-identical
                # stanza written by CouchCHD can be retargeted to a new one.
                previous = _entry(old).lstrip()
        if previous is None:
            raise ValueError(f'Conflicting CouchCHD system in {path}; leaving it unchanged')

    # Insert at the real root closing tag, not a match in a comment. Preserve
    # existing XML bytes verbatim rather than round-tripping other systems.
    end = 0
    start = -1
    depth = 0
    parser = expat.ParserCreate()

    def on_start(name: str, attrs: dict[str, str]) -> None:
        nonlocal start, depth
        if depth == 1 and name == 'system' and attrs.get('uuid') == _UUID:
            start = parser.CurrentByteIndex
        depth += 1

    def on_end(_name: str) -> None:
        nonlocal end, depth
        depth -= 1
        end = parser.CurrentByteIndex

    # Without a start callback Expat reports the *opening* offset for an
    # empty element's end event. With both callbacks it points past "/>".
    parser.StartElementHandler = on_start
    parser.EndElementHandler = on_end
    parser.Parse(data, True)
    if previous is not None:
        if start < 0 or not data.startswith(previous, start):
            raise ValueError(f'Conflicting CouchCHD system in {path}; leaving it unchanged')
        return data[:start] + entry.lstrip() + data[start + len(previous):]
    if data[end:end+2] == b'</':
        return data[:end] + b'\n' + entry + b'\n' + data[end:]
    if data[end-2:end] == b'/>':
        return data[:end-2] + b'>\n' + entry + b'\n</systemList>' + data[end:]
    raise ValueError(f'Cannot safely extend the encoding of {path}; use UTF-8')


def _frontend_stopped() -> bool:
    """Recalbox runs emulationstation-starter under this pidfile; fail closed."""
    try:
        pidfile = Path('/var/run/emulationstation.pid')
        if pidfile.exists():
            pid = int(pidfile.read_text().strip())
            if pid <= 0:
                return False
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                pass
        # A manually launched frontend need not have a pidfile. Linux comm
        # is truncated to 15 bytes, including for emulationstation-starter.
        for p in Path('/proc').iterdir():
            if not p.name.isdecimal():
                continue
            try:
                if (p / 'comm').read_bytes().strip().startswith(b'emulationstatio'):
                    return False
            except (FileNotFoundError, ProcessLookupError):
                pass
        return True
    except (ValueError, OSError):
        return False


def _cover(roms: Path) -> None:
    media = roms / 'media'
    if media.is_symlink():
        raise ValueError(f'Will not install through symlink {media}')
    media.mkdir(exist_ok=True)
    dest = media / _IMAGE
    if dest.is_symlink() or (dest.exists() and not dest.is_file()):
        raise ValueError(f'Leaving existing media path unchanged: {dest}')
    if not dest.exists():
        store_entry(dest, (_ASSETS / 'grid.png').read_bytes(), None)
    else:
        upgrade_stock(dest, _ASSETS / 'grid.png')


def _artwork(roms: Path, games: bytes | None) -> str:
    gamelist = roms / 'gamelist.xml'
    if os.path.lexists(roms / 'gamelist.zip'):
        return 'Leaving the archived Recalbox gamelist and its artwork unchanged.'
    # Do not hide a recoverable library by creating fresh XML in front of it.
    if (os.path.lexists(roms / 'gamelist.intermediate-tmp') or
            (games is None and os.path.lexists(roms / 'gamelist.backup'))):
        return 'Leaving Recalbox gamelist recovery files unchanged.'
    if games not in (None, _PREVIEW4_GAMELIST, _GAMELIST):
        return 'Leaving the customized Recalbox gamelist and its artwork unchanged.'
    if games == _PREVIEW4_GAMELIST and not _frontend_stopped():
        return ('Recalbox cover not attached: quit CouchCHD, stop EmulationStation, '
                'then rerun the installer before starting EmulationStation again.')
    _cover(roms)
    store_entry(gamelist, _GAMELIST, games)
    return 'Recalbox Ports cover is installed.'


def refresh_artwork(share: Path, launcher: Path | None = None) -> str:
    """Never register a new port from a running app or edit user-managed metadata."""
    roms = share / 'roms/ports/couchchd'
    config = share / 'system/.emulationstation/systemlist.xml'
    if any(p.is_symlink() for p in (share, *roms.parents, *config.parents, roms)):
        raise ValueError('Leaving redirected Recalbox paths unchanged.')
    system = read_regular(config)
    if (system is None or _merge(system, config, share, _launcher(share, launcher)) != system
            or read_regular(roms / 'CouchCHD.game') != _MARKER):
        return 'No registered CouchCHD Recalbox port; no artwork changed.'
    games = read_regular(roms / 'gamelist.xml')
    if games is None:
        return 'No managed Recalbox gamelist; no artwork changed.'
    return _artwork(roms, games)


def register(share: Path | None = None, *, launcher: Path | None = None) -> None:
    """Publish one non-executable .game marker, its metadata, and its port descriptor."""
    if share is None:
        # Keep the historical CLI contract for SHARE/system/couchchd installs.
        share = (launcher.parents[2] if launcher and launcher.parts[-3:] == ('system', 'couchchd', 'run-couchchd')
                 else SHARE)
    launcher = _launcher(share, launcher)
    config = share / 'system/.emulationstation/systemlist.xml'
    roms = share / 'roms/ports/couchchd'
    marker, gamelist = roms / 'CouchCHD.game', roms / 'gamelist.xml'
    for folder in (share, share / 'system', config.parent, share / 'roms', roms.parent, roms):
        if folder.is_symlink():
            raise ValueError(f'Will not install through symlink {folder}')
        folder.mkdir(exist_ok=True)
    original = read_regular(config)
    updated = _merge(original, config, share, launcher)
    previous = read_regular(marker)
    if previous not in (None, _MARKER):
        raise ValueError(f'Will not replace unmanaged {marker}')
    games = read_regular(gamelist)
    if games is not None:
        tree = _xml(games, gamelist)
        if tree.tag != 'gameList' or not any(p.text in ('CouchCHD.game', './CouchCHD.game')
                                             for p in tree.findall('./game/path')):
            raise ValueError(f'Will not replace unmanaged {gamelist}')
    store_entry(marker, _MARKER, previous)
    if games is None and previous == _MARKER and not _frontend_stopped():
        print('Missing Recalbox gamelist; not recreating it while EmulationStation may be running.')
    else:
        print(_artwork(roms, games))
    store_entry(config, updated, original)
    print('When ready: START > QUIT > RESTART RECALBOX, then open Ports > CouchCHD.\n'
          'If Ports is hidden: START > SYSTEM-LISTS & GAMELISTS SETTINGS > SHOW PORTS SYSTEM.')
