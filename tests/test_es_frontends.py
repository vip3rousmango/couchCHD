import shlex
from xml.sax.saxutils import escape, quoteattr

import pytest

from couchchd import es_frontends as es


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path.resolve() / 'deck'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('ESDE_APPDATA_DIR', raising=False)
    monkeypatch.delenv('FLATPAK_ID', raising=False)
    monkeypatch.setattr(es, '_ESDE_PREFIXES', ())
    monkeypatch.setattr(es, '_CLASSIC_SYSTEMS', home / 'etc/es_systems.cfg')
    launcher = home / "Native app's launcher"
    launcher.write_text('#!/bin/sh\nexit 0\n')
    launcher.chmod(0o755)
    return home, launcher


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def settings(profile, root, *, modern=True, extra=''):
    file = 'settings/es_settings.xml' if modern else 'es_settings.xml'
    return write(profile / file, '<?xml version="1.0"?>\n' + extra +
                 f'<string name="ROMDirectory" value={quoteattr(str(root))} />')


def system(path='%ROMPATH%/ports', commands='<command>/bin/sh %ROM%</command>', extensions='.sh'):
    return ('<systemList><system><name>ports</name><fullname>Ports</fullname>'
            f'<path>{escape(str(path))}</path><extension>{extensions}</extension>{commands}'
            '</system></systemList>')


def assert_entry(entry, launcher, root=None):
    assert entry.stat().st_mode & 0o111
    cmd = shlex.split(entry.read_text().splitlines()[-1])
    assert cmd == ['exec', str(launcher), *(['--root', str(root)] if root else [])]


def test_desktop_only(home):
    h, launcher = home
    (h / 'Emulation/roms/ports').mkdir(parents=True)
    messages = es.register(launcher)
    assert any('No configured' in m and 'Steam' in m for m in messages)
    assert list((h / 'Emulation/roms/ports').iterdir()) == []
    assert not (h / '.emulationstation').exists() and not (h / 'ES-DE').exists()


def test_classic_config_precedence_and_idempotence(home):
    h, launcher = home
    root = h / 'rom drive'
    root.mkdir()
    write(es._CLASSIC_SYSTEMS, system(h / 'wrong ports'))
    config = write(h / '.emulationstation/es_systems.cfg', system('~/rom drive/ports', '<command>%ROM%</command>', '.zip,.SH'))
    before = config.read_bytes()
    for _ in range(2):
        assert any('Restart EmulationStation' in m for m in es.register(launcher))
        assert_entry(root / 'ports/CouchCHD.SH', launcher, root)
    assert config.read_bytes() == before and not (h / 'wrong ports').exists()


def test_classic_global_and_manual_no_ports(home):
    h, launcher = home
    write(es._CLASSIC_SYSTEMS, system(h / 'local launchers'))
    assert any('installed' in m for m in es.register(launcher))
    assert_entry(h / 'local launchers/CouchCHD.sh', launcher)
    config = write(h / '.emulationstation/es_systems.cfg', '<systemList />')
    assert any('no ports system' in m for m in es.register(launcher))
    assert config.read_text() == '<systemList />'


@pytest.mark.parametrize('old', [False, True])
def test_esde_emudeck_appimage_generated_info(home, old):
    h, launcher = home
    profile = h / 'ES-DE'
    root = h / 'SD Card/Emulation/roms'
    settings(profile, root, modern=not old, extra='<bool name="ShowHiddenFiles" value="false" />\n')
    directory = root / 'ports'
    write(directory / 'systeminfo.txt', 'System name:\nports\n\nFull system name:\nPorts\n\n'
          'Supported file extensions:\n.desktop .sh\n\nLaunch command:\n'
          '%ENABLESHORTCUTS% %EMULATOR_OS-SHELL% %ROM%\n\nAlternative launch commands:\nother %ROM%\n')
    write(profile / 'custom_systems/es_systems.xml', '<systemList><system><name>other</name></system></systemList>')
    assert any('Rescan ROM directory' in m for m in es.register(launcher))
    assert_entry(directory / 'CouchCHD.sh', launcher, root)
    assert not (h / 'ROMs').exists()


def test_custom_esde_and_selected_alternative(home, monkeypatch):
    h, launcher = home
    profile = h / '.config/custom ES-DE'
    root = h / 'Game Libraries'
    root.mkdir()
    monkeypatch.setenv('ESDE_APPDATA_DIR', str(profile))
    settings(profile, '~/Game Libraries')
    config = write(profile / 'custom_systems/es_systems.xml', '<?xml version="1.0"?>\n<loadExclusive/>\n' +
                   system('%ROMPATH%/Launchers', '<command label="Emulator">emu %ROM%</command>'
                          '<command label="Script">%RUNINBACKGROUND% %EMULATOR_OS-SHELL% %ROM%</command>'))
    before = config.read_bytes()
    write(profile / 'gamelists/ports/gamelist.xml', '<alternativeEmulator><label>Script</label></alternativeEmulator><gameList/>')
    assert any('installed' in m for m in es.register(launcher))
    assert_entry(root / 'Launchers/CouchCHD.sh', launcher, root)
    assert config.read_bytes() == before and not (root / 'ports').exists()


def test_legacy_esde_local_resource_and_portable_root(home, monkeypatch):
    h, launcher = home
    profile = h / '.emulationstation'
    root = h / 'actual library'
    root.mkdir()
    settings(profile, '%ESPATH%/../ROMs', modern=False)
    monkeypatch.setattr(es.platform, 'machine', lambda: 'aarch64')
    write(profile / 'resources/systems/linuxarm/es_systems.xml', system('%ROMPATH%/programs'))
    assert any('unresolved' in m for m in es.register(launcher))
    assert any('installed' in m for m in es.register(launcher, roms=root))
    assert_entry(root / 'programs/CouchCHD.sh', launcher, root)


@pytest.mark.parametrize(('content', 'reason'), [
    ('<loadExclusive/><systemList/>', 'exclusive'),
    (system(extensions='.desktop .zip'), 'does not list'),
    (system(commands='<command>wine %ROM%</command>'), 'not a supported shell command'),
    (system(commands='<command>echo sh %ROM%</command>'), 'not a supported shell command'),
    ('<broken', 'cannot read'),
])
def test_conflicting_custom_system_never_falls_through(home, content, reason):
    h, launcher = home
    profile, root = h / 'ES-DE', h / 'ROMs'
    root.mkdir()
    settings(profile, '')
    write(profile / 'custom_systems/es_systems.xml', content)
    assert any(reason in m for m in es.register(launcher))
    assert list(root.iterdir()) == []


def test_gamelist_only_or_non_shell_alternative(home):
    h, launcher = home
    profile, root = h / 'ES-DE', h / 'ROMs'
    root.mkdir()
    settings(profile, root, extra='<bool name="ParseGamelistOnly" value="true"/>')
    write(profile / 'custom_systems/es_systems.xml', system(commands='<command label="Shell">sh %ROM%</command>'
          '<command label="Emulator">retroarch -L core %ROM%</command>'))
    assert any('gamelist-only' in m for m in es.register(launcher))
    settings(profile, root)
    write(profile / 'gamelists/ports/gamelist.xml', '<gameList><game><path>./CouchCHD.sh</path>'
          '<altemulator>Emulator</altemulator></game></gameList>')
    assert any('not a supported shell command' in m for m in es.register(launcher))
    assert not (root / 'ports').exists()


def test_custom_shell_rules_and_name_collision(home):
    h, launcher = home
    profile, root = h / 'ES-DE', h / 'ROMs'
    root.mkdir()
    settings(profile, root)
    write(profile / 'custom_systems/es_systems.xml',
          system(commands='<command>%EMULATOR_OS-SHELL% %ROM%</command>'))
    rules = profile / 'custom_systems/es_find_rules.xml'
    rule = '<ruleList><emulator name="OS-SHELL"><rule type="systempath"><entry>{}</entry></rule></emulator></ruleList>'
    write(rules, rule.format('wine'))
    assert any('cannot verify' in m for m in es.register(launcher))
    write(rules, rule.format('bash'))
    write(root / 'ports/CouchCHD.desktop', 'existing entry')
    assert any('unchanged' in m for m in es.register(launcher))
    assert not (root / 'ports/CouchCHD.sh').exists()


def test_missing_local_appimage_definition_and_wrong_hint(home):
    h, launcher = home
    profile, root = h / 'ES-DE', h / 'ROMs'
    root.mkdir()
    settings(profile, root)
    assert any('Create/update system directories' in m for m in es.register(launcher))
    assert any('does not match' in m for m in es.register(launcher, roms=h))
    assert not (root / 'ports').exists()


def test_unowned_launcher_and_symlink_are_not_overwritten(home):
    h, launcher = home
    directory = h / 'media/ports'
    custom = write(directory / 'CouchCHD.sh', 'mine!')
    write(h / '.emulationstation/es_systems.cfg', system(directory))
    assert any('unchanged' in m for m in es.register(launcher))
    assert custom.read_text() == 'mine!'
    custom.unlink()
    (h / 'media').rename(h / 'real')
    (h / 'media').symlink_to(h / 'real', target_is_directory=True)
    assert any('link' in m for m in es.register(launcher))
    assert not (h / 'real/ports/CouchCHD.sh').exists()


def test_symlinked_profile_not_read(home):
    h, launcher = home
    actual = h / 'actual/ES-DE'
    settings(actual, h)
    write(actual / 'custom_systems/es_systems.xml', system())
    (h / 'ES-DE').symlink_to(actual, target_is_directory=True)
    assert any('symbolic link' in m for m in es.register(launcher))
    assert not (h / 'ports').exists()


def test_retrodeck_flatpak_not_confused_with_native_esde(home, monkeypatch):
    h, launcher = home
    sandbox = h / '.var/app/net.retrodeck.retrodeck/config/ES-DE'
    root = h / 'retrodeck/roms'
    root.mkdir(parents=True)
    settings(sandbox, root)
    write(sandbox / 'custom_systems/es_systems.xml', system())
    monkeypatch.setenv('ESDE_APPDATA_DIR', str(sandbox))
    messages = es.register(launcher)
    assert any('RetroDECK Flatpak' in m and 'Steam' in m for m in messages)
    assert not (root / 'ports').exists()
    monkeypatch.setenv('FLATPAK_ID', 'some.other.Frontend')
    write(es._CLASSIC_SYSTEMS, system(root / 'ports'))
    assert any('Inside a Flatpak' in m for m in es.register(launcher))
    assert not (root / 'ports').exists()
