from pathlib import Path

import pytest

vdf = pytest.importorskip('vdf')
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


def test_existing_shortcut_bytes_are_never_recoded():
    # Legacy installations can have local-codepage names. Decoding and then
    # serializing the existing tree would corrupt this name with U+FFFD.
    old = vdf.binary_dumps({'shortcuts': {'4': {'AppName': 'PLACE', 'tags': {'0': 'RPG'}}}}).replace(b'PLACE', b'T\xffST!')
    updated, _ = _shortcut(old, Path('/apps/CouchCHD'))
    assert updated.startswith(old[:-2])
    assert set(vdf.binary_loads(updated)['shortcuts']) == {'4', '5'}


@pytest.mark.parametrize('blob', [b'', b'not vdf', b'\x00shortcuts\x00\x08', vdf.binary_dumps({'other': {}})])
def test_invalid_existing_shortcuts_abort(blob):
    with pytest.raises(RuntimeError):
        _shortcut(blob, Path('/apps/CouchCHD'))
