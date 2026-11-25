"""Add a launcher to an existing, shell-compatible Linux EmulationStation system.

The frontends own their configuration and gamelists. Read them; never rewrite them.
In particular a directory called 'roms/ports' alone is not evidence of a frontend.
"""
from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import stat
import xml.etree.ElementTree as ET

from .menu_entry import install_script

_CLASSIC_SYSTEMS = Path('/etc/emulationstation/es_systems.cfg')
_ESDE_PREFIXES = (Path('/usr/local/share/es-de'), Path('/usr/share/es-de'))
_RETRODECK = 'net.retrodeck.retrodeck'


def _mode(path: Path) -> int:
    """Reject links (including dangling links) before inspecting their children."""
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError(f'expected an absolute path without traversal: {path}')
    for component in (*reversed(path.parents), path):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            return 0
        if stat.S_ISLNK(mode):
            raise ValueError(f'not following symbolic link {component}')
    return mode


def _text(path: Path) -> str:
    if not stat.S_ISREG(_mode(path)):
        raise ValueError(f'not a regular file: {path}')
    with path.open('rb') as handle:
        data = handle.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise ValueError(f'file too large for menu detection: {path}')
    return data.decode('utf-8-sig')


def _xml(path: Path) -> ET.Element:
    # Both frontends use pugixml, which accepts several top-level elements
    # (ES-DE settings, loadExclusive and alternativeEmulator actually use this).
    text = re.sub(r'^\s*<\?xml\s+[^?]*\?>', '', _text(path), count=1)
    try:
        return ET.fromstring('<document>' + text + '</document>')
    except ET.ParseError as exc:
        raise ValueError(f'cannot read {path}: {exc}') from exc


def _settings(path: Path) -> dict[str, str]:
    doc = _xml(path)
    values = {n.get('name'): n.get('value', '') for scope in (doc.find('settings'), doc)
              if scope is not None for n in scope if n.tag in ('string', 'bool')}
    if values.get('ParseGamelistOnly', '').lower() in ('true', '1'):
        raise ValueError('gamelist-only mode is enabled; enable ROM scanning first')
    return values


def _ports(path: Path) -> tuple[ET.Element | None, bool]:
    doc = _xml(path)
    systems = doc.find('systemList')
    if systems is None:
        raise ValueError(f'missing systemList in {path}')
    entries = [s for s in systems.findall('system') if s.findtext('name') == 'ports']
    if len(entries) > 1:
        raise ValueError(f'multiple ports system definitions in {path}')
    return (entries[0] if entries else None, doc.find('loadExclusive') is not None)


def _path(value: str) -> Path:
    if re.search(r'%[A-Za-z_]+%', value):
        raise ValueError(f'unresolved frontend path variable in {value}')
    path = Path.home() / value[2:] if value.startswith('~/') else Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError(f'expected an absolute configured path, got {value!r}')
    return path


def _extension(value: str) -> str:
    for ext in re.split(r'[,\s]+', value):
        if ext.lower() == '.sh':
            return ext
    raise ValueError('the configured Ports system does not list shell scripts (.sh)')


def _install(directory: Path, launcher: Path, root: Path | None, ext: str, command: str,
             *, esde: bool) -> Path:
    # Accept known command forms, not substrings in a RetroArch/runcommand/Wine wrapper.
    words = command.split()
    if esde:
        words = [w for w in words if w not in ('%ENABLESHORTCUTS%', '%RUNINBACKGROUND%')]
    shells = ('sh', 'bash', '/bin/sh', '/bin/bash', '/usr/bin/sh', '/usr/bin/bash')
    compatible = (not esde and words == ['%ROM%']) or (
        len(words) == 2 and words[1] == '%ROM%' and
        (words[0] in shells or (esde and words[0] == '%EMULATOR_OS-SHELL%')))
    if not compatible:
        raise ValueError('the selected Ports launch command is not a supported shell command')
    # Do not create a missing external library or a tree of directories at a stale mount path.
    present = _mode(directory)
    if not present and not stat.S_ISDIR(_mode(directory.parent)):
        raise ValueError(f'configured Ports parent directory is unavailable: {directory.parent}')
    entry = directory / ('CouchCHD' + ext)
    if present:
        for sibling in directory.iterdir():
            if (sibling.name != entry.name and sibling.stem.casefold() == 'couchchd'
                    and sibling.suffix.lower() in ('.sh', '.desktop', '.appimage')):
                raise FileExistsError(f'leaving existing launcher {sibling} unchanged')
    install_script(entry, launcher, args=('--root', str(root)) if root is not None else ())
    return entry


def _classic(home: Path, launcher: Path, hint: Path | None) -> str | None:
    profile = home / '.emulationstation'
    systems = profile / 'es_systems.cfg'
    if not _mode(systems):
        systems = _CLASSIC_SYSTEMS
    if not _mode(systems):
        return None
    settings = profile / 'es_settings.cfg'
    if _mode(settings):
        _settings(settings)
    ports, _ = _ports(systems)
    if ports is None:
        raise ValueError(f'no ports system in {systems}; configure a Ports system with .sh and /bin/sh %ROM% first')
    directory = _path(ports.findtext('path', ''))
    # A custom ports folder need not be located inside the user's game library.
    root = hint if hint is not None else directory.parent if directory.name == 'ports' else None
    entry = _install(directory, launcher, root, _extension(ports.findtext('extension', '')),
                     ports.findtext('command', ''), esde=False)
    return f'EmulationStation: installed {entry}. Restart EmulationStation when ready to refresh Ports.'


def _resource_dirs(data: Path):
    target = 'linuxarm' if platform.machine().lower() in ('aarch64', 'arm64') else 'linux'
    for directory in (data, *_ESDE_PREFIXES):
        yield directory / 'resources/systems' / target

def _check_shell_rules(data: Path) -> None:
    custom = data / 'custom_systems/es_find_rules.xml'
    bundled = (r / 'es_find_rules.xml' for r in _resource_dirs(data))
    for path in (custom, *bundled):
        if not _mode(path):
            continue
        shell = _xml(path).find('ruleList/emulator[@name="OS-SHELL"]')
        if shell is None and path == custom:
            continue  # Custom rules are merged by emulator name.
        if shell is not None:
            rules = shell.findall('rule')
            names = [entry.text for rule in rules for entry in rule.findall('entry')]
            if names and all(r.get('type') == 'systempath' for r in rules) and all(
                    name in ('sh', 'bash') for name in names):
                return
        raise ValueError(f'cannot verify an OS-SHELL shell interpreter in {path}')
    # AppImage find rules are embedded; the upstream OS-SHELL mapping is bash/sh.



def _system_info(directory: Path) -> tuple[str, str]:
    # Native AppImages embed es_systems.xml. ES-DE itself exports the effective
    # extensions and default command in this file via Create/update system directories.
    info = directory / 'systeminfo.txt'
    if not _mode(info):
        raise ValueError('cannot inspect bundled Ports; in ES-DE use Utilities > Create/update system directories, then rerun setup')
    lines = _text(info).splitlines()
    fields = dict(zip(lines, lines[1:]))
    if fields.get('System name:') != 'ports':
        raise ValueError(f'{info} does not describe the bundled ports system')
    return fields.get('Supported file extensions:', ''), fields.get('Launch command:', '')


def _alternative(data: Path, directory: Path, ext: str, settings: dict[str, str]) -> str:
    path = data / 'gamelists/ports/gamelist.xml'
    legacy = directory / 'gamelist.xml'
    if settings.get('LegacyGamelistFileLocation', '').lower() in ('true', '1') and _mode(legacy):
        path = legacy
    if not _mode(path):
        return ''
    doc = _xml(path)
    label = (doc.findtext('alternativeEmulator/label') or
             doc.findtext('gameList/alternativeEmulator/label') or '')
    if settings.get('AlternativeEmulatorPerGame', 'true').lower() not in ('false', '0'):
        for game in doc.findall('gameList/game'):
            item = Path(game.findtext('path', ''))
            if item in (Path('CouchCHD' + ext), directory / ('CouchCHD' + ext)):
                return game.findtext('altemulator') or label
    return label


def _esde(home: Path, launcher: Path, hint: Path | None) -> str | None:
    override = os.environ.get('ESDE_APPDATA_DIR')
    data = _path(override) if override is not None else home / 'ES-DE'
    if not _mode(data) and override is None:
        data = home / '.emulationstation'  # pre-3.0 ES-DE
    settings_file = data / 'settings/es_settings.xml'
    if not _mode(settings_file):
        settings_file = data / 'es_settings.xml'
    if not _mode(settings_file):
        if override or data == home / 'ES-DE' and _mode(data):
            raise ValueError(f'no settings in {data}; initialize ES-DE first')
        return None
    if data.is_relative_to(home / '.var/app') or _RETRODECK in data.parts:
        raise ValueError('sandboxed frontend profile; a native launcher cannot be assumed to run inside it')
    settings = _settings(settings_file)
    configured = settings.get('ROMDirectory') or '~/ROMs'
    root = hint if '%ESPATH%' in configured and hint is not None else _path(configured)
    if hint is not None and root != hint:
        raise ValueError(f'--root {hint} does not match ES-DE ROMDirectory {root}')
    if not stat.S_ISDIR(_mode(root)):
        raise ValueError(f'configured ROMDirectory is unavailable: {root}')

    ports = None
    custom = data / 'custom_systems/es_systems.xml'
    if _mode(custom):
        ports, exclusive = _ports(custom)
        if ports is None and exclusive:
            raise ValueError(f'exclusive custom systems file {custom} has no ports system')
    if ports is None:
        for resources in _resource_dirs(data):
            bundled = resources / 'es_systems.xml'
            if _mode(bundled):
                ports, _ = _ports(bundled)
                if ports is None:
                    raise ValueError(f'no ports system in {bundled}')
                break

    if ports is None:
        directory = root / 'ports'
        extensions, command = _system_info(directory)
        ext = _extension(extensions)
        if _alternative(data, directory, ext, settings):
            raise ValueError('a nondefault Ports emulator is configured; cannot verify its command from systeminfo.txt')
    else:
        if not ports.findtext('fullname'):
            raise ValueError('the ES-DE ports system is missing its required fullname')
        directory = _path(ports.findtext('path', '').replace('%ROMPATH%', str(root)))
        ext = _extension(ports.findtext('extension', ''))
        label = _alternative(data, directory, ext, settings)
        commands = ports.findall('command')
        if label:
            if any(not c.get('label') for c in commands):
                raise ValueError('cannot resolve the selected Ports emulator; a command has no label')
            commands = [c for c in commands if c.get('label') == label]
        command = (commands[0].text or '') if commands else ''

    if '%EMULATOR_OS-SHELL%' in command:
        _check_shell_rules(data)
    entry = _install(directory, launcher, root, ext, command, esde=True)
    return f'ES-DE: installed {entry}. Use Utilities > Rescan ROM directory, or restart ES-DE when ready.'


def register(launcher: Path, *, roms: Path | None = None) -> list[str]:
    """Best effort: a broken/missing frontend does not invalidate the desktop install."""
    home = Path.home()
    messages = []
    fallback = 'Use the desktop Applications menu.'
    try:
        if not stat.S_ISREG(_mode(launcher)) or not os.access(launcher, os.X_OK):
            raise ValueError(f'not an executable launcher: {launcher}')
        if roms is not None:
            roms = _path(str(roms))
            if not stat.S_ISDIR(_mode(roms)):
                raise ValueError(f'ROM root not available: {roms}')
        retrodeck = home / '.var/app' / _RETRODECK
        if _mode(retrodeck):
            messages.append(f'RetroDECK Flatpak detected. Its Ports system is not a host shell launcher. {fallback}')
    except (OSError, ValueError) as exc:
        return [f'Frontend menu registration unavailable: {exc}. {fallback}']
    if os.environ.get('FLATPAK_ID'):
        return [*messages, f'Inside a Flatpak sandbox; not registering a host launcher. {fallback}']
    for name, install in (('ES-DE', _esde), ('EmulationStation', _classic)):
        try:
            message = install(home, launcher, roms)
            if message:
                messages.append(message)
        except (OSError, ValueError) as exc:
            messages.append(f'{name} menu registration unavailable: {exc}. {fallback}')
    return messages or [f'No configured ES-DE or EmulationStation frontend found. {fallback}']
