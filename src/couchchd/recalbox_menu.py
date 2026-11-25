"""Register a native Recalbox port without changing the read-only system list."""
import os
import shlex
import stat
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.parsers import expat
from xml.sax.saxutils import quoteattr

from .ledger import install_new


_UUID = '27790d47-96e4-43be-a995-8c2ca3e6ea0d'
_ROM_PATH = '%ROOT%/ports/couchchd'
_MARKER = b'CouchCHD managed Recalbox menu entry\n'
_GAMELIST = b'<gameList><game><path>./CouchCHD.game</path><name>CouchCHD</name></game></gameList>\n'


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


def _merge(data: bytes | None, path: Path, entry: bytes) -> bytes:
    if data is None:
        return b'<?xml version="1.0"?>\n<systemList>\n' + entry + b'\n</systemList>\n'
    root = _xml(data, path)
    if root.tag != 'systemList':
        raise ValueError(f'Expected <systemList> in {path}')
    matches = [s for s in root.findall('system') if s.get('uuid') == _UUID or s.get('name') == 'couchchd'
               or s.find(f"descriptor[@path='{_ROM_PATH}']") is not None]
    if matches:
        if len(matches) != 1 or _signature(matches[0]) != _signature(ET.fromstring(entry)):
            raise ValueError(f'Conflicting CouchCHD system in {path}; leaving it unchanged')
        return data

    # Insert at the real root closing tag, not a match in a comment. Preserve
    # existing XML bytes verbatim rather than round-tripping other systems.
    end = 0
    parser = expat.ParserCreate()

    def on_end(_name: str) -> None:
        nonlocal end
        end = parser.CurrentByteIndex

    # Without a start callback Expat reports the *opening* offset for an
    # empty element's end event. With both callbacks it points past "/>".
    parser.StartElementHandler = lambda _name, _attrs: None
    parser.EndElementHandler = on_end
    parser.Parse(data, True)
    if data[end:end+2] == b'</':
        return data[:end] + b'\n' + entry + b'\n' + data[end:]
    if data[end-2:end] == b'/>':
        return data[:end-2] + b'>\n' + entry + b'\n</systemList>' + data[end:]
    raise ValueError(f'Cannot safely extend the encoding of {path}; use UTF-8')


def _read_regular(path: Path) -> bytes | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise ValueError(f'Will not follow or replace {path}')
    return path.read_bytes()


def _store(path: Path, data: bytes, old: bytes | None) -> None:
    if data == old:
        return
    fd, name = tempfile.mkstemp(prefix='.couchchd-', dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            if old is not None:
                info, staged = path.lstat(), os.fstat(f.fileno())
                if (info.st_uid, info.st_gid) != (staged.st_uid, staged.st_gid):
                    os.fchown(f.fileno(), info.st_uid, info.st_gid)
                if stat.S_IMODE(info.st_mode) != stat.S_IMODE(staged.st_mode):
                    os.fchmod(f.fileno(), stat.S_IMODE(info.st_mode))
            os.fsync(f.fileno())
        if old is None:
            install_new(tmp, path)
        else:
            if _read_regular(path) != old:
                raise ValueError(f'{path} changed during setup; rerun the installer')
            os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def register(share: Path) -> None:
    """Publish one non-executable .game marker, its metadata, and its port descriptor."""
    config = share / 'system/.emulationstation/systemlist.xml'
    roms = share / 'roms/ports/couchchd'
    marker, gamelist = roms / 'CouchCHD.game', roms / 'gamelist.xml'
    for folder in (share, share / 'system', config.parent, share / 'roms', roms.parent, roms):
        if folder.is_symlink():
            raise ValueError(f'Will not install through symlink {folder}')
        folder.mkdir(exist_ok=True)
    original = _read_regular(config)
    updated = _merge(original, config, _entry(share / 'system/couchchd/run-couchchd'))
    previous = _read_regular(marker)
    if previous not in (None, _MARKER):
        raise ValueError(f'Will not replace unmanaged {marker}')
    games = _read_regular(gamelist)
    if games is not None:
        tree = _xml(games, gamelist)
        if tree.tag != 'gameList' or not any(p.text in ('CouchCHD.game', './CouchCHD.game')
                                             for p in tree.findall('./game/path')):
            raise ValueError(f'Will not replace unmanaged {gamelist}')
    _store(marker, _MARKER, previous)
    if games is None:
        _store(gamelist, _GAMELIST, None)
    _store(config, updated, original)
    print('When ready: START > QUIT > RESTART RECALBOX, then open Ports > CouchCHD.\n'
          'If Ports is hidden: START > SYSTEM-LISTS & GAMELISTS SETTINGS > SHOW PORTS SYSTEM.')
