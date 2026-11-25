import os
from pathlib import Path

import pytest

vdf = pytest.importorskip('vdf')
from couchchd.ui import steam
from couchchd.ui.steam import _shortcut


def test_new_shortcut_has_persistent_executable_and_unsigned_art_id(tmp_path):
    launcher = tmp_path / 'My Programs' / 'CouchCHD.AppImage'
    grid = tmp_path / 'grid'
    blob, appid, _ = _shortcut(None, launcher, grid)
    entry = vdf.binary_loads(blob)['shortcuts']['0']
    assert entry['Exe'] == f'"{launcher}"'
    assert entry['StartDir'] == f'"{launcher.parent}"'
    assert entry['AppName'] == 'CouchCHD'
    assert entry['appid'] & 0xffffffff == appid
    assert entry['icon'] == str(grid / f'{appid}_icon.png')
    assert _shortcut(blob, launcher, grid) == (blob, appid, os.fsencode(entry['icon']))


def test_existing_shortcut_bytes_are_never_recoded(tmp_path):
    # Legacy installations can have local-codepage names. Decoding and then
    # serializing the existing tree would corrupt this name with U+FFFD.
    old = vdf.binary_dumps({'shortcuts': {'4': {'AppName': 'PLACE', 'tags': {'0': 'RPG'}}}}).replace(b'PLACE', b'T\xffST!')
    updated, _, _ = _shortcut(old, tmp_path / 'CouchCHD', tmp_path / 'grid')
    assert updated.startswith(old[:-2])
    assert set(vdf.binary_loads(updated)['shortcuts']) == {'4', '5'}


@pytest.mark.parametrize('blob', [b'', b'not vdf', b'\x00shortcuts\x00\x08', vdf.binary_dumps({'other': {}})])
def test_invalid_existing_shortcuts_abort(tmp_path, blob):
    with pytest.raises(RuntimeError):
        _shortcut(blob, tmp_path / 'CouchCHD', tmp_path / 'grid')


@pytest.mark.parametrize('relative', [
    'applications/io.github.vip3rousmango.CouchCHD.desktop',
    f'couchchd/builds/{"a" * 64}/CouchCHD-x86_64.AppImage',
    f'couchchd/builds/{"a" * 64}/run',
    'tmp/appimage_extracted_ab12cd/couchchd',
    'tmp/.mount_CouchC1ab2cd/AppRun',
])
def test_repair_replaces_only_launch_fields(tmp_path, monkeypatch, relative):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    launcher = tmp_path / 'couchchd/run-couchchd'
    old_exe = str(tmp_path / relative)
    old_options = '--appimage-extract-and-run' if relative.endswith('.AppImage') else ''
    signed_id = -123456789
    other = {'AppName': 'PLACE', 'Exe': '/opt/some-other-program'}
    name = 'CouchCHD' if relative.startswith('tmp/') else 'My disc tools'
    item = {'AppName': name, 'eXe': old_exe, 'appid': signed_id,
            'StartDir': '"wrong directory"', 'LaunchOptions': old_options,
            'LastPlayTime': 100, 'AllowDesktopConfig': 0, 'custom': vdf.UINT_64(2**63 + 8),
            'icon': '/my/custom.ico', 'tags': {'0': 'PLACE', 'Exe': old_exe}}

    def document(entry):
        return vdf.binary_dumps({'shortcuts': {'2': other, '9': entry, '10': other}}).replace(b'PLACE', b'T\xffST!')

    expected = document(dict(item, eXe=f'"{launcher}"', StartDir=f'"{launcher.parent}"', LaunchOptions=''))
    updated, appid, icon = _shortcut(document(item), launcher, tmp_path / 'grid')
    assert icon == b'/my/custom.ico'
    assert updated == expected
    assert appid == signed_id & 0xffffffff
    assert _shortcut(updated, launcher, tmp_path / 'grid') == (expected, appid, icon)


@pytest.mark.parametrize('filename', ['CouchCHD.AppImage', 'CouchCHD.desktop', 'CouchCHD-x86_64.AppImage'])
def test_distinct_external_build_is_preserved(tmp_path, filename):
    item = {'Exe': str(tmp_path / 'experimental' / filename), 'AppName': 'Experimental disc tools', 'appid': -22}
    original = vdf.binary_dumps({'shortcuts': {'3': item}})
    launcher = tmp_path / 'installed/run-couchchd'
    blob, _, _ = _shortcut(original, launcher, tmp_path / 'grid')
    assert blob.startswith(original[:-2])
    assert vdf.binary_loads(blob)['shortcuts']['4']['Exe'] == f'"{launcher}"'


def test_manual_entry_without_startdir_gets_one_and_keeps_custom_options_on_current_target(tmp_path):
    launcher = tmp_path / 'run-couchchd'
    item = {'Exe': str(launcher), 'appid': -100, 'LaunchOptions': '--root "/my games"'}
    grid = tmp_path / 'grid'
    blob, appid, _ = _shortcut(vdf.binary_dumps({'shortcuts': {'5': item}}), launcher, grid)
    assert vdf.binary_loads(blob)['shortcuts'] == {
        '5': dict(item, Exe=f'"{launcher}"', StartDir=f'"{tmp_path}"', icon=str(grid / f'{appid}_icon.png'))}


@pytest.mark.parametrize(('entries', 'reason'), [
    ({'0': {'Exe': '/apps/CouchCHD-x86_64.AppImage', 'AppName': 'CouchCHD-x86_64', 'appid': -1},
      '1': {'Exe': '/apps/CouchCHD.desktop', 'AppName': 'CouchCHD', 'appid': -2}}, 'Multiple CouchCHD'),
    ({'0': {'Exe': '/apps/CouchCHD.AppImage', 'AppName': 'CouchCHD', 'appid': -1},
      '1': {'Exe': '/apps/other', 'appid': -1}}, 'unique app ID'),
    ({'0': {'Exe': '/tmp/appimage_extracted_abc/AppRun', 'AppName': 'CouchCHD', 'appid': -1}},
     'unrecognized target'),
    ({'0': {'Exe': '/apps/CouchCHD.AppImage', 'AppName': 'CouchCHD', 'LaunchOptions': 'env FOO=1 %command%', 'appid': -1}},
     'custom Steam launch options'),
    ({'0': {'Exe': '/apps/CouchCHD.AppImage', 'exe': '/other', 'appid': -1}}, 'Cannot read'),
])
def test_conflicts_are_not_merged_or_duplicated(tmp_path, entries, reason):
    entries = {i: dict(entry, Exe=str(Path(entry['Exe']).absolute())) for i, entry in entries.items()}
    with pytest.raises(RuntimeError, match=reason):
        _shortcut(vdf.binary_dumps({'shortcuts': entries}), tmp_path / 'installed/run-couchchd', tmp_path / 'grid')


def test_exported_launcher_wins_over_appimage(tmp_path, monkeypatch):
    launcher = tmp_path / 'run-couchchd'
    appimage = tmp_path / 'CouchCHD.AppImage'
    launcher.touch()
    appimage.touch()
    monkeypatch.setenv('COUCHCHD_LAUNCHER', str(launcher))
    monkeypatch.setenv('APPIMAGE', str(appimage))
    assert steam._launcher() == launcher


@pytest.mark.skipif(os.name == 'nt', reason='UNIX native Steam paths and PID')
def test_register_requires_offline_then_preserves_backup_and_art(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('SteamAppId', raising=False)
    root = tmp_path / '.local/share/Steam'
    config = root / 'userdata/123/config'
    config.mkdir(parents=True)
    launcher = tmp_path / 'run-couchchd'
    launcher.touch(mode=0o755)
    signed_id = -10
    original = vdf.binary_dumps({'shortcuts': {'4': {
        'AppName': 'CouchCHD', 'Exe': str(tmp_path / 'CouchCHD.desktop'), 'appid': signed_id}}})
    shortcuts = config / 'shortcuts.vdf'
    shortcuts.write_bytes(original)
    pid = root / 'steam.pid'
    pid.write_text(str(os.getpid()))
    with pytest.raises(steam.SteamRunning):
        steam.register(launcher)
    assert set(config.iterdir()) == {shortcuts}
    assert shortcuts.read_bytes() == original
    pid.unlink()

    grid = config / 'grid'
    grid.mkdir()
    appid = signed_id & 0xffffffff
    for suffix in ('P.JPG', '_HERO.WebP', '_ICON.ico'):
        (grid / f'{appid}{suffix}').write_bytes(b'user artwork')
    placement = grid / f'{appid}.json'
    placement.write_text('{"pinnedPosition":"BottomLeft"}')
    steam.register(launcher)
    assert (config / 'shortcuts.vdf.couchchd-backup').read_bytes() == original
    assert vdf.binary_loads(shortcuts.read_bytes())['shortcuts'] == {'4': {
        'AppName': 'CouchCHD', 'Exe': f'"{launcher}"', 'appid': signed_id, 'StartDir': f'"{tmp_path}"',
        'icon': str(grid / f'{appid}_ICON.ico')}}
    assert {p.name for p in grid.iterdir()} == {
        f'{appid}{suffix}' for suffix in ('P.JPG', '_HERO.WebP', '_ICON.ico', '.json', '.png', '_logo.png')}
    assert all((grid / f'{appid}{suffix}').read_bytes() == b'user artwork'
               for suffix in ('P.JPG', '_HERO.WebP', '_ICON.ico'))
    assert placement.read_text() == '{"pinnedPosition":"BottomLeft"}'
    steam.register(launcher)
    assert (config / 'shortcuts.vdf.couchchd-backup').read_bytes() == original


@pytest.mark.skipif(os.name == 'nt', reason='Linux native/Flatpak Steam layout')
def test_flatpak_only_even_through_native_alias_is_not_modified(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    flatpak = tmp_path / '.var/app/com.valvesoftware.Steam/.local/share/Steam'
    profile = flatpak / 'userdata/123/config'
    profile.mkdir(parents=True)
    alias = tmp_path / '.steam/steam'
    alias.parent.mkdir()
    alias.symlink_to(flatpak)
    with pytest.raises(RuntimeError, match='Flatpak'):
        steam.register(tmp_path / 'run-couchchd')
    assert not list(profile.iterdir())


@pytest.mark.skipif(os.name == 'nt', reason='UNIX native Steam layout')
def test_running_with_ambiguous_accounts_is_not_a_retryable_registration(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('SteamAppId', '42')
    root = tmp_path / '.local/share/Steam'
    for account in ('123', '456'):
        (root / 'userdata' / account / 'config').mkdir(parents=True)
    (root / 'config').mkdir()
    (root / 'config/loginusers.vdf').write_text(vdf.dumps({'users': {
        '123': {'MostRecent': '1'}, '456': {'MostRecent': '1'}}}))
    with pytest.raises(RuntimeError, match='identify the active Steam account') as caught:
        steam.register(tmp_path / 'run-couchchd')
    assert not isinstance(caught.value, steam.SteamRunning)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('SteamAppId', raising=False)
    monkeypatch.delenv('SteamGameId', raising=False)
    root = tmp_path / '.local/share/Steam'
    config = root / 'userdata/123/config'
    config.mkdir(parents=True)
    monkeypatch.setattr(steam, '_steam_dir', lambda: root)
    launcher = tmp_path / 'couchchd/run-couchchd'
    launcher.parent.mkdir()
    launcher.touch(mode=0o755)
    file = config / 'shortcuts.vdf'
    file.write_bytes(vdf.binary_dumps({'shortcuts': {'7': {
        'AppName': 'My renamed app', 'Exe': f'"{launcher}"', 'appid': -12, 'icon': ''}}}))
    return launcher, file, (-12 & 0xffffffff)


def test_live_refresh_uses_recorded_id_and_never_edits_shortcuts(profile, monkeypatch):
    launcher, file, appid = profile
    original = file.read_bytes()
    monkeypatch.setenv('SteamGameId', str((appid << 32) | 0x02000000))
    monkeypatch.setenv('SteamAppId', '0')
    grid = file.parent / 'grid'
    grid.mkdir()
    (grid / f'{appid}_LOGO.JpEg').write_bytes(b'my logo')
    message = steam.refresh_artwork(launcher)
    assert 'portrait, landscape, hero, icon' in message and 'exit Steam' in message
    assert 'No missing' in steam.refresh_artwork(launcher)
    assert file.read_bytes() == original
    assert {p.name for p in file.parent.iterdir()} == {'grid', 'shortcuts.vdf'}
    assert (grid / f'{appid}_LOGO.JpEg').read_bytes() == b'my logo'
    assert not (grid / f'{appid}_logo.png').exists()
    assert not (file.parents[3] / 'controller_base').exists()


def test_refresh_replaces_only_recognized_stock_for_the_same_slot(profile, monkeypatch):
    from hashlib import sha256
    from couchchd import stock_art

    launcher, file, appid = profile
    previous = b'old portrait'
    monkeypatch.setitem(stock_art._PREVIOUS, 'grid', (len(previous), sha256(previous).hexdigest()))
    grid = file.parent / 'grid'
    grid.mkdir()
    portrait = grid / f'{appid}p.png'
    portrait.write_bytes(previous)
    alternate = grid / f'{appid}p.jpg'
    alternate.write_bytes(b'preferred portrait')
    steam.refresh_artwork(launcher)
    assert portrait.read_bytes() == previous and alternate.read_bytes() == b'preferred portrait'
    alternate.unlink()
    portrait.write_bytes(b'my! portrait')  # Same byte length; not CouchCHD artwork.
    steam.refresh_artwork(launcher)
    assert portrait.read_bytes() == b'my! portrait'
    portrait.write_bytes(previous)
    steam.refresh_artwork(launcher)
    assert portrait.read_bytes() == (steam.ASSETS / 'grid.png').read_bytes()


@pytest.mark.parametrize('conflict', ['wrong-game', 'wrong-app', 'other-installation', 'other-account',
                                      'duplicate', 'unregistered', 'malformed', 'linked-grid'])
def test_live_refresh_fails_closed(profile, monkeypatch, conflict):
    launcher, file, appid = profile
    if conflict == 'wrong-game':
        monkeypatch.setenv('SteamGameId', str(((appid + 1) << 32) | 0x02000000))
    elif conflict == 'wrong-app':
        monkeypatch.setenv('SteamAppId', '99')
    elif conflict == 'other-account':
        (file.parents[2] / '456/config').mkdir(parents=True)
    elif conflict == 'unregistered':
        file.write_bytes(vdf.binary_dumps({'shortcuts': {}}))
    elif conflict == 'malformed':
        file.write_bytes(b'incomplete')
    elif conflict == 'linked-grid':
        (file.parent / 'grid').symlink_to(launcher.parent, target_is_directory=True)
    else:
        entries = vdf.binary_loads(file.read_bytes())['shortcuts']
        if conflict == 'duplicate':
            entries['8'] = dict(entries['7'], appid=-13)
        else:
            entries['7'].update(AppName='CouchCHD', Exe='/opt/experimental/CouchCHD.AppImage')
        file.write_bytes(vdf.binary_dumps({'shortcuts': entries}))
    original = file.read_bytes()
    with pytest.raises(RuntimeError):
        steam.refresh_artwork(launcher)
    assert file.read_bytes() == original
    assert not list(file.parents[2].rglob('*.png')) and not list(launcher.parent.glob('*.png'))


def test_external_icon_preserved_without_another_steam_icon(profile):
    launcher, file, appid = profile
    entries = vdf.binary_loads(file.read_bytes())
    entries['shortcuts']['7']['icon'] = '/chosen custom art/ICON.ico'
    file.write_bytes(vdf.binary_dumps(entries))
    assert 'attach the shortcut icon' not in steam.refresh_artwork(launcher)
    assert not (file.parent / 'grid' / f'{appid}_icon.png').exists()
    steam.register(launcher)
    assert vdf.binary_loads(file.read_bytes())['shortcuts']['7']['icon'] == '/chosen custom art/ICON.ico'
