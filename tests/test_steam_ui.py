import os
from pathlib import Path

import pytest

vdf = pytest.importorskip('vdf')
from couchchd.ui import steam
from couchchd.ui.steam import _shortcut


def test_new_shortcut_has_persistent_executable_and_unsigned_art_id(tmp_path):
    launcher = tmp_path / 'My Programs' / 'CouchCHD.AppImage'
    blob, appid = _shortcut(None, launcher)
    entry = vdf.binary_loads(blob)['shortcuts']['0']
    assert entry['Exe'] == f'"{launcher}"'
    assert entry['StartDir'] == f'"{launcher.parent}"'
    assert entry['AppName'] == 'CouchCHD'
    assert entry['appid'] & 0xffffffff == appid
    assert _shortcut(blob, launcher) == (blob, appid)


def test_existing_shortcut_bytes_are_never_recoded(tmp_path):
    # Legacy installations can have local-codepage names. Decoding and then
    # serializing the existing tree would corrupt this name with U+FFFD.
    old = vdf.binary_dumps({'shortcuts': {'4': {'AppName': 'PLACE', 'tags': {'0': 'RPG'}}}}).replace(b'PLACE', b'T\xffST!')
    updated, _ = _shortcut(old, tmp_path / 'CouchCHD')
    assert updated.startswith(old[:-2])
    assert set(vdf.binary_loads(updated)['shortcuts']) == {'4', '5'}


@pytest.mark.parametrize('blob', [b'', b'not vdf', b'\x00shortcuts\x00\x08', vdf.binary_dumps({'other': {}})])
def test_invalid_existing_shortcuts_abort(tmp_path, blob):
    with pytest.raises(RuntimeError):
        _shortcut(blob, tmp_path / 'CouchCHD')


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
    updated, appid = _shortcut(document(item), launcher)
    assert updated == expected
    assert appid == signed_id & 0xffffffff
    assert _shortcut(updated, launcher) == (expected, appid)


@pytest.mark.parametrize('filename', ['CouchCHD.AppImage', 'CouchCHD.desktop', 'CouchCHD-x86_64.AppImage'])
def test_distinct_external_build_is_preserved(tmp_path, filename):
    item = {'Exe': str(tmp_path / 'experimental' / filename), 'AppName': 'Experimental disc tools', 'appid': -22}
    original = vdf.binary_dumps({'shortcuts': {'3': item}})
    launcher = tmp_path / 'installed/run-couchchd'
    blob, _ = _shortcut(original, launcher)
    assert blob.startswith(original[:-2])
    assert vdf.binary_loads(blob)['shortcuts']['4']['Exe'] == f'"{launcher}"'


def test_manual_entry_without_startdir_gets_one_and_keeps_custom_options_on_current_target(tmp_path):
    launcher = tmp_path / 'run-couchchd'
    item = {'Exe': str(launcher), 'appid': -100, 'LaunchOptions': '--root "/my games"'}
    blob, _ = _shortcut(vdf.binary_dumps({'shortcuts': {'5': item}}), launcher)
    assert vdf.binary_loads(blob)['shortcuts'] == {
        '5': dict(item, Exe=f'"{launcher}"', StartDir=f'"{tmp_path}"')}


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
        _shortcut(vdf.binary_dumps({'shortcuts': entries}), tmp_path / 'installed/run-couchchd')


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
    for suffix in ('p', '_hero'):
        (grid / f'{signed_id & 0xffffffff}{suffix}.jpg').write_bytes(b'user artwork')
    steam.register(launcher)
    assert (config / 'shortcuts.vdf.couchchd-backup').read_bytes() == original
    assert vdf.binary_loads(shortcuts.read_bytes())['shortcuts'] == {'4': {
        'AppName': 'CouchCHD', 'Exe': f'"{launcher}"', 'appid': signed_id, 'StartDir': f'"{tmp_path}"'}}
    assert sorted(p.read_bytes() for p in grid.iterdir()) == [b'user artwork'] * 2
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
