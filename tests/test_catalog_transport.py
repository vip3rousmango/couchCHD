import os
import shutil

import pytest

from couchchd import catalog_transport as transport


def put(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_inventory_and_selected_complete_set(tmp_path):
    source = tmp_path / 'home'
    dest = tmp_path / 'device'
    dest.mkdir()
    m3u = b'\xef\xbb\xbf#EXTM3U\r\n.chd_discs/Disc 1.chd\r\n./Disc 2.CHD\r\nDisc 2.CHD\r\n'
    files = {'psx/Edition/Game.m3u': m3u, 'psx/Edition/.chd_discs/Disc 1.chd': b'one',
             'psx/Edition/Disc 2.CHD': b'two', 'psp/racer.cso': b'c', 'PS1/solo.chd': b's',
             'dreamcast/arcade.chd': b'd', 'ngc/cube.RVZ': b'g', 'wii/wii.rvz': b'w',
             'xbox/game.xiso.iso': b'x', '3ds/game.trim.cci': b'3', 'n64/game.7z': b'7',
             'psp/game.chd': b'p', 'ps2/game.chd': b'2'}
    for name, data in files.items():
        put(source, name, data)
    for name in ('psx/Ignore.iso', 'psx/Bad.rvz', 'ps2/Bad.cso', 'ngc/Bad.chd',
                 '.other/psx/No.chd', '_couchchd_backup/psx/No.chd', 'nes/bios/file.7z',
                 'unknown/file.chd', 'psx/.chd_discs/Orphan.chd'):
        put(source, name, b'not selected')
    put(source, 'psx/Empty.chd', b'')
    put(source, 'psx/Incomplete.m3u', b'absent.chd\n')
    (source / 'psx/Link.chd').symlink_to(source / 'PS1/solo.chd')
    (source / 'psx/Linkdir').symlink_to(source / 'PS1', target_is_directory=True)

    games = {g.relative: g for g in transport.list_games(source)}
    members = ('psx/Edition/.chd_discs/Disc 1.chd', 'psx/Edition/Disc 2.CHD', 'psx/Edition/Game.m3u')
    assert set(games) == set(files) - set(members[:-1])
    g = games[members[-1]]
    assert g.members == members and g.size == len(m3u) + 6
    progress = []
    outputs = transport.pull_game(source, g.relative, dest, lambda done, total: progress.append((done, total)))
    assert outputs == tuple(dest / n for n in members)
    assert [p.read_bytes() for p in outputs] == [files[n] for n in members]
    assert progress == sorted(progress) and progress[0] == (0, g.size) and progress[-1] == (g.size, g.size)
    assert {p.relative_to(dest).as_posix() for p in dest.rglob('*') if p.is_file()} == set(members)
    timestamps = [p.stat().st_mtime_ns for p in outputs]
    assert transport.pull_game(source, g.relative, dest) == outputs
    assert [p.stat().st_mtime_ns for p in outputs] == timestamps


def test_direct_platform_source_and_destination(tmp_path):
    source = tmp_path / 'home/PS1'
    put(source, 'nested/Game.chd', b'game')
    dest = tmp_path / 'psx'
    dest.mkdir()
    game, = transport.list_games(source)
    assert game.relative == 'PS1/nested/Game.chd'
    assert transport.destination_paths(dest, game.members) == (dest / 'nested/Game.chd',)
    assert transport.pull_game(source, game.relative, dest) == (dest / 'nested/Game.chd',)
    other = tmp_path / 'psp'
    other.mkdir()
    with pytest.raises(ValueError, match='matching platform'):
        transport.pull_game(source, game.relative, other)
    assert not list(other.iterdir())


@pytest.mark.parametrize('member', ['../secret.chd', '/secret.chd', r'C:\secret.chd', 'Missing.chd',
                                    'drive:other.chd', 'Linked/game.chd', 'bad\x00.chd', 'Game.iso'])
def test_invalid_playlist_cannot_escape_or_publish(tmp_path, member):
    source = tmp_path / 'home'
    put(source, 'psx/Bad.m3u', f'First.chd\n{member}\n'.encode())
    put(source, 'psx/First.chd', b'data')
    private = tmp_path / 'private'
    put(private, 'game.chd', b'not shared')
    (source / 'psx/Linked').symlink_to(private, target_is_directory=True)
    dest = tmp_path / 'other'
    dest.mkdir()
    assert [g.relative for g in transport.list_games(source)] == ['psx/First.chd']
    with pytest.raises(ValueError):
        transport.pull_game(source, 'psx/Bad.m3u', dest)
    assert not list(dest.iterdir())


def test_conflict_space_disconnection_and_replaced_source(tmp_path, monkeypatch):
    source = tmp_path / 'home'
    first = put(source, 'psx/1.chd', b'old')
    put(source, 'psx/2.chd', b'disc')
    put(source, 'psx/Set.m3u', b'1.chd\n2.chd\n')
    dest = tmp_path / 'other'
    different = put(dest, 'psx/2.chd', b'same')  # Same name and size, different bytes.
    with pytest.raises(ValueError, match='Different file'):
        transport.pull_game(source, 'psx/Set.m3u', dest)
    assert different.read_bytes() == b'same' and sorted(different.parent.iterdir()) == [different]
    different.unlink()
    with monkeypatch.context() as partial:
        usage = type('Usage', (), {'f_frsize': 4096, 'f_bavail': 0})
        partial.setattr(os, 'fstatvfs', lambda _: usage())
        with pytest.raises(ValueError, match='free space'):
            transport.pull_game(source, 'psx/Set.m3u', dest)
    with pytest.raises(FileNotFoundError):
        transport.pull_game(source, 'psx/1.chd', tmp_path / 'gone')
    with pytest.raises(ValueError, match='replaced'):
        transport.pull_game(source, 'psx/1.chd', dest, expected_root=(-1, -1))
    with pytest.raises(ValueError, match='overlap'):
        transport.pull_game(source, 'psx/1.chd', source)

    def changed(done, total):
        if done == total:
            first.write_bytes(b'new')  # Earlier member changed after its bytes were already staged.

    with pytest.raises(ValueError, match='changed'):
        transport.pull_game(source, 'psx/Set.m3u', dest, changed)
    assert not list((dest / 'psx').iterdir()) and set(dest.iterdir()) == {dest / 'psx'}


@pytest.mark.parametrize('location', [':sftp,host=example:/games', 'peer,user=other:games',
                                      'sftp://host/roms', '-arg:games', 'peer:../roms', 'peer://etc',
                                      'peer:games/bios/psx', 'relative/path', 'peer:games\\escape'])
def test_named_remotes_only(location):
    with pytest.raises(ValueError):
        transport.parse_remote(location)


@pytest.mark.parametrize(('location', 'base'), [('nas:', ''), ('nas:/', '/'), ('nas:/user/roms/', '/user/roms'),
                                              ('nas:user/roms', 'user/roms')])
def test_relative_and_absolute_remote_paths(location, base):
    assert transport.parse_remote(location) == ('nas', base)


@pytest.mark.skipif(not shutil.which('rclone'), reason='rclone is optional')
def test_real_configured_remote_and_independent_digest(tmp_path, monkeypatch):
    root = tmp_path / 'remote'
    first = put(root, 'psx/One.chd', b'remote bytes')
    put(root, 'psx/One.m3u', b'One.chd\n')
    put(root, 'psx/Other.chd', b'must not be read on pull')
    config = put(tmp_path, 'rclone.conf', b'[testpeer]\ntype = local\n')
    monkeypatch.setenv('RCLONE_CONFIG', str(config))
    location = 'testpeer:' + root.as_posix()
    dest = tmp_path / 'here'
    dest.mkdir()
    assert [g.relative for g in transport.list_games(location)] == ['psx/One.m3u', 'psx/Other.chd']
    outputs = transport.pull_game(location, 'psx/One.m3u', dest)
    assert [p.read_bytes() for p in outputs] == [b'remote bytes', b'One.chd\n']
    assert not (dest / 'psx/Other.chd').exists()
    for p in outputs:
        p.unlink()

    # A remote with coarse timestamps could silently change bytes at the same size.
    st = first.stat()
    def change_remote(done, total):
        if done == st.st_size:
            first.write_bytes(b'REMOTE BYTES')
            os.utime(first, ns=(st.st_atime_ns, st.st_mtime_ns))

    with pytest.raises(ValueError, match='SHA-256 changed'):
        transport.pull_game(location, 'psx/One.m3u', dest, change_remote)
    assert not list((dest / 'psx').iterdir()) and set(dest.iterdir()) == {dest / 'psx'}
