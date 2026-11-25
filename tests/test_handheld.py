"""Copy-verification tests use arbitrary bytes: playable-format discovery isn't emulator validation."""
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from couchchd import handheld
from couchchd.handheld_profiles import profiles


def put(base, name, data):
    path = base / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_nonledger_mixed_pack_with_existing_card_and_complete_playlist(tmp_path):
    library, card = tmp_path / 'home', tmp_path / 'card'
    put(library, 'PS1/A.chd', b'already there')
    put(library, 'Dreamcast/Set/.chd_discs/First.chd', b'one')
    put(library, 'Dreamcast/Set/Second.chd', b'two')
    playlist = b'\xef\xbb\xbf#EXTM3U\r\n.chd_discs/First.chd\r\n./Second.chd\r\n'
    put(library, 'Dreamcast/Set/Set.m3u', playlist)
    put(library, 'psp/Older.cso', b'cso')
    put(library, 'psp/Newer.chd', b'dvd chd')
    put(library, 'psp/Original.iso', b'originals stay here')
    put(library, 'ps2/Unsupported.chd', b'ps2')
    put(library, 'PS1/Wrong.cso', b'not a PSP image')
    before = tree(library)
    identical = put(card, 'ROMs/PSX/A.chd', b'already there')
    same_stat = identical.stat()
    put(card, 'ROMs/PSX/Other.chd', b'my existing collection')

    rows = handheld.entries(library)
    ids = [row['path'] for row in rows]
    assert set(ids) == {'PS1/A.chd', 'Dreamcast/Set/Set.m3u', 'psp/Older.cso', 'psp/Newer.chd'}
    row = next(r for r in rows if r['system'] == 'dreamcast')
    assert row == dict(path='Dreamcast/Set/Set.m3u', system='dreamcast', title='Set', files=3,
                       size=6 + len(playlist))
    result = handheld.export(library, [*ids, ids[0]], card)
    assert result['skipped'] == ['ROMs/PSX/A.chd']
    assert set(result['copied']) == {'ROMs/dreamcast/Set/.chd_discs/First.chd',
                                     'ROMs/dreamcast/Set/Second.chd', 'ROMs/dreamcast/Set/Set.m3u',
                                     'ROMs/psp/Older.cso', 'ROMs/psp/Newer.chd'}
    assert result['copied'][-1].endswith('Set.m3u')
    for name in result['copied']:
        _, system, suffix = name.split('/', 2)
        assert (card / name).read_bytes() == before[('Dreamcast' if system == 'dreamcast' else system) + '/' + suffix]
    assert result['bytes'] == sum((card / name).stat().st_size for name in result['copied'])
    assert identical.stat().st_ino == same_stat.st_ino and identical.stat().st_mtime_ns == same_stat.st_mtime_ns
    assert (card / 'ROMs/PSX/Other.chd').read_bytes() == b'my existing collection'
    again = handheld.export(library, ids, card)
    assert not again['copied'] and again['bytes'] == 0
    assert set(again['skipped']) == set(result['copied'] + result['skipped'])
    assert tree(library) == before  # Includes no new journal or lock file, even on first use.


def test_direct_platform_and_shared_playlist_members(tmp_path):
    src, dest = tmp_path / 'source/psx', tmp_path / 'card/PS1'
    dest.mkdir(parents=True)
    put(src, 'shared.chd', b'disc')
    for i in (1, 2):
        put(src, f'{i}.m3u', b'shared.chd\n')
    result = handheld.export(src, [r['path'] for r in handheld.entries(src)], dest)
    assert result['copied'] == ['shared.chd', '1.m3u', '2.m3u']
    assert tree(src) == tree(dest)


def test_conflict_checked_across_whole_selection_before_copy(tmp_path):
    src, dest = tmp_path / 'pc', tmp_path / 'card'
    put(src, 'psp/A.cso', b'new')
    put(src, 'psx/B.chd', b'usa')
    put(dest, 'psx/B.chd', b'eur')  # same name AND length; only the digest shows the conflict
    before = tree(dest)
    with pytest.raises(ValueError, match='Different file'):
        handheld.export(src, ['psp/A.cso', 'psx/B.chd'], dest)
    assert tree(dest) == before
    assert [d.name for d in dest.iterdir()] == ['psx']


@pytest.mark.parametrize('line', ['../other.chd', '/tmp/Wrong.chd', 'C:\\disc.chd', 'http://host/a.chd',
                                  '.chd_discs/absent.chd', '.private/disc.chd', 'disc.cso', 'link.chd',
                                  'outside/disc.chd', 'outside/../disc.chd'])
def test_incomplete_or_unconfined_playlist_never_exports(tmp_path, line):
    src, dest = tmp_path / 'src', tmp_path / 'card'
    dest.mkdir()
    outside = put(tmp_path, 'elsewhere/disc.chd', b'not ours')
    put(src, 'psx/Set.m3u', ('good.chd\n' + line + '\n').encode())
    put(src, 'psx/good.chd', b'good')
    (src / 'psx/link.chd').symlink_to(outside)
    (src / 'psx/outside').symlink_to(outside.parent, target_is_directory=True)
    rows = handheld.entries(src)
    assert not any(r['path'].endswith('Set.m3u') or 'link' in r['path'] or 'outside' in r['path'] for r in rows)
    with pytest.raises(ValueError, match='Not a complete playable'):
        handheld.export(src, ['psx/Set.m3u'], dest)
    assert not list(dest.iterdir())


def test_refuses_overlap_invalid_selection_and_linked_paths(tmp_path):
    src, dest = tmp_path / 'src', tmp_path / 'card'
    dest.mkdir()
    image = put(src, 'psx/Game.chd', b'keep')
    inside = src / 'dest'
    inside.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(src, target_is_directory=True)
    for root, selected, card in [(src, [], dest), (src, ['../src/psx/Game.chd'], dest),
                                 (src, [str(image)], dest), (src, ['psx/Game.chd'], inside),
                                 (src, ['psx/Game.chd'], tmp_path), (alias, ['psx/Game.chd'], dest),
                                 (src, ['psx/Game.chd'], alias), (src, ['psx/Game.chd'], tmp_path / 'absent')]:
        with pytest.raises((ValueError, OSError)):
            handheld.export(root, selected, card)
    (dest / 'psx').symlink_to(src / 'psx', target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        handheld.export(src, ['psx/Game.chd'], dest)
    assert tree(src) == {'psx/Game.chd': b'keep'}
    (dest / 'psx').unlink()
    (dest / 'psx').mkdir()
    (dest / 'psx/Game.chd').symlink_to(image)
    with pytest.raises((ValueError, OSError)):
        handheld.export(src, ['psx/Game.chd'], dest)
    assert (dest / 'psx/Game.chd').is_symlink()


def test_case_aliases_and_multiple_platform_folders_are_not_guessed(tmp_path):
    src, dest = tmp_path / 'src', tmp_path / 'card'
    put(src, 'psx/Game.chd', b'image')
    put(dest, 'PS1/GAME.CHD', b'image')
    with pytest.raises(ValueError, match='spelling conflict'):
        handheld.export(src, 'psx/Game.chd', dest)
    (dest / 'psx').mkdir()
    with pytest.raises(ValueError, match='Multiple psx folders'):
        handheld.export(src, 'psx/Game.chd', dest)
    assert handheld.export(src, 'psx/Game.chd', dest / 'psx')['copied'] == ['Game.chd']


def test_no_space_checked_before_creating_files(tmp_path, monkeypatch):
    src, dest = tmp_path / 'src', tmp_path / 'card'
    put(src, 'psx/A.chd', b'x')
    dest.mkdir()
    monkeypatch.setattr(os, 'fstatvfs', lambda fd: SimpleNamespace(f_frsize=4096, f_bavail=0))
    with pytest.raises(ValueError, match='free space'):
        handheld.export(src, ['psx/A.chd'], dest)
    assert not list(dest.iterdir())


def test_install_race_never_replaces_new_card_file_or_publishes_playlist(tmp_path, monkeypatch):
    src, dest = tmp_path / 'src', tmp_path / 'card'
    dest.mkdir()
    put(src, 'psx/Set.m3u', b'.chd_discs/A.chd\n.chd_discs/B.chd\n')
    put(src, 'psx/.chd_discs/A.chd', b'disc A')
    put(src, 'psx/.chd_discs/B.chd', b'disc B')
    install = handheld.install_new

    def occupied(stage, name, **kwargs):
        if name.name == 'B.chd':
            put(dest, 'psx/.chd_discs/B.chd', b'another writer')
        install(stage, name, **kwargs)

    monkeypatch.setattr(handheld, 'install_new', occupied)
    with pytest.raises(ValueError, match='Previously published complete files remain'):
        handheld.export(src, 'psx/Set.m3u', dest)
    assert tree(dest) == {'psx/.chd_discs/A.chd': b'disc A', 'psx/.chd_discs/B.chd': b'another writer'}
    assert not list(dest.glob('.couchchd-export-*'))


def test_remote_hash_current_snapshot_and_staging_are_required_before_install(tmp_path):
    dest = tmp_path / 'dest'
    dest.mkdir()
    data = b'precompressed elsewhere'
    mk = lambda mark: SimpleNamespace(size=len(data), digest=mark, stream=lambda: io.BytesIO(data))
    good = mk(hashlib.sha256(data).digest())
    with pytest.raises(ValueError, match='SHA-256'):
        handheld.publish_pack(dest, {Path('psp/Good.cso'): good, Path('psx/Bad.chd'): mk(bytes(32))})
    assert not list(dest.iterdir())

    def stale():
        raise ValueError('source playlist changed')

    with pytest.raises(ValueError, match='source playlist changed'):
        handheld.publish_pack(dest, {Path('psp/Good.cso'): good}, check=stale)
    assert not list(dest.iterdir())
    put(dest, 'psp/Good.cso', data)
    with pytest.raises(ValueError, match='source playlist changed'):
        handheld.publish_pack(dest, {Path('psp/Good.cso'): good}, check=stale)


def test_pinned_card_directory_never_follows_swap_to_source(tmp_path):
    dest, source = tmp_path / 'card', tmp_path / 'library'
    (dest / 'psx').mkdir(parents=True)
    source.mkdir()

    def substitute():
        (dest / 'psx').rmdir()
        (dest / 'psx').symlink_to(source, target_is_directory=True)

    with pytest.raises((ValueError, OSError)):
        handheld.publish_pack(dest, {Path('psx/A.chd'): SimpleNamespace(size=1, digest=None,
                                                                       stream=lambda: io.BytesIO(b'x'))},
                              check=substitute)
    assert not list(source.iterdir())
    assert not list(dest.glob('.couchchd-export-*'))


def test_corrupt_staged_bytes_fail_destination_readback(tmp_path, monkeypatch):
    src, dest = tmp_path / 'src', tmp_path / 'card'
    dest.mkdir()
    put(src, 'psx/Game.chd', b'intact source')
    sync = os.fsync

    def corrupt(fd):
        import stat
        if stat.S_ISREG(os.fstat(fd).st_mode):
            os.pwrite(fd, b'x', 0)
        sync(fd)

    monkeypatch.setattr(os, 'fsync', corrupt)
    with pytest.raises(ValueError, match='Card copy did not match'):
        handheld.export(src, ['psx/Game.chd'], dest)
    assert not list(dest.iterdir())
    assert tree(src) == {'psx/Game.chd': b'intact source'}


def test_changed_source_during_staging_prevents_playlist_publication(tmp_path, monkeypatch):
    src, dest = tmp_path / 'src', tmp_path / 'card'
    dest.mkdir()
    disc = put(src, 'psx/Disc.chd', b'first edition')
    put(src, 'psx/Set.m3u', b'Disc.chd\n')
    copy = handheld._copy

    def change_disc(path, source, fd, name):
        copy(path, source, fd, name)
        if path.suffix == '.m3u':
            disc.write_bytes(b'different edition')

    monkeypatch.setattr(handheld, '_copy', change_disc)
    with pytest.raises(ValueError, match='File changed'):
        handheld.export(src, ['psx/Set.m3u'], dest)
    assert not list(dest.iterdir())


def test_profiles_are_serializable_destination_rules_not_native_platform_claims():
    data = profiles()
    assert json.loads(json.dumps(data)) == data
    assert len({p['id'] for p in data}) == len(data)
    for profile in data:
        assert profile['native'] is False
        for player in profile['players']:
            assert player['source'].startswith('https://')
            if profile['destination'] == 'rom-root':
                assert player['systems'].keys() <= profile['folders'].keys()
    assert all(not p['default'] for p in next(p for p in data if p['id'] == 'manual')['players'])


def test_player_version_is_explicit_and_enforced_again_at_export(tmp_path):
    root, card = tmp_path / 'library', tmp_path / 'card'
    card.mkdir()
    for name in ('psp/A.cso', 'psp/B.chd', 'psx/C.chd'):
        put(root, name, b'game')
    assert handheld.entries(root, None) == handheld.entries(root)
    assert handheld.entries(root, 'android-t618') == []
    assert [r['path'] for r in handheld.entries(root, 'arkos', players=['ppsspp'])] == ['psp/A.cso']
    assert [r['path'] for r in handheld.entries(root, 'arkos', players=['ppsspp-1.17.1'])] == ['psp/A.cso', 'psp/B.chd']
    for profile, players in [('missing', []), ('adam', ['dolphin-rvz']), ('manual', ['ppsspp', 'ppsspp-1.17.1'])]:
        with pytest.raises(ValueError):
            handheld.entries(root, profile, players=players)
    with pytest.raises(ValueError, match='Not a complete playable'):
        handheld.export(root, 'psp/B.chd', card, 'manual', players=['ppsspp'])
    assert not list(card.iterdir())


def test_adam_plays_documented_single_discs_and_copies_to_exact_initialized_folders(tmp_path):
    root, roms = tmp_path / 'library', tmp_path / 'card/roms'
    put(root, 'psx/Game.chd', b'PS')
    put(root, 'psx/Game.m3u', b'Game.chd\n')
    put(root, 'segacd/Disc.chd', b'Sega')
    put(root, 'pcecd/Disc.chd', b'NEC')
    put(root, 'nes/Cart.7z', b'cart archive')
    put(root, 'nes/firmware.7z', b'not a game')
    put(root, 'arcade/nes/Machine.7z', b'arcade set')
    put(root, 'psp/Other.chd', b'PSP')
    assert [r['path'] for r in handheld.entries(root, 'adam')] == ['psx/Game.chd']
    players = ['pcsx4all', 'genesis-plus-gx', 'beetle-pce-fast', 'adam-carts']
    selected = [r['path'] for r in handheld.entries(root, 'adam', players=players)]
    assert selected == ['nes/Cart.7z', 'pcecd/Disc.chd', 'psx/Game.chd', 'segacd/Disc.chd']
    for folder in ('PS', 'SEGACD', 'PCECD'):
        (roms / folder).mkdir(parents=True)
    with pytest.raises(ValueError, match='initialized'):
        handheld.export(root, selected, roms, 'adam', players=players)
    assert tree(roms) == {}
    (roms / 'FC').mkdir()
    handheld.export(root, selected, roms, 'adam', players=players)
    assert tree(roms) == {'FC/Cart.7z': b'cart archive', 'PS/Game.chd': b'PS',
                          'PCECD/Disc.chd': b'NEC', 'SEGACD/Disc.chd': b'Sega'}


def test_frontend_filter_narrows_core_playlist_extensions(tmp_path):
    root, roms = tmp_path / 'library', tmp_path / 'card/share/roms'
    for system in ('segacd', 'pcenginecd'):
        put(root, system + '/.chd_discs/Disc.chd', b'one')
        put(root, system + '/Set.m3u', b'.chd_discs/Disc.chd\n')
        (roms / system).mkdir(parents=True)
    players = ['genesis-plus-gx', 'beetle-pce-fast']
    assert handheld.entries(root, 'arkos', players=players) == []
    assert handheld.entries(root, 'darkos', players=players) == []
    assert {r['system'] for r in handheld.entries(root, 'rocknix', players=players)} == {'segacd', 'pcenginecd'}
    assert [r['path'] for r in handheld.entries(root, 'knulli', players=players)] == ['segacd/Set.m3u']
    copied = handheld.export(root, 'segacd/Set.m3u', roms, 'knulli', players=players)['copied']
    assert copied == ['segacd/.chd_discs/Disc.chd', 'segacd/Set.m3u']


def test_minui_and_regional_folder_aliases_require_unambiguous_existing_destination(tmp_path):
    root, roms = tmp_path / 'library', tmp_path / 'card/Roms'
    put(root, 'ps1/Game.chd', b'PS')
    put(root, 'pcecd/Game.chd', b'PCE')
    for name in ('My Console (PS)', 'pcenginecd', 'tg16cd'):
        (roms / name).mkdir(parents=True)
    assert handheld.export(root, 'ps1/Game.chd', roms, 'minui')['copied'] == ['My Console (PS)/Game.chd']
    with pytest.raises(ValueError, match='one of'):
        handheld.export(root, 'pcecd/Game.chd', roms, 'rocknix')
    assert handheld.export(root, 'pcecd/Game.chd', roms / 'tg16cd', 'rocknix')['copied'] == ['Game.chd']
    with pytest.raises(ValueError, match='layout'):
        handheld.export(root, 'ps1/Game.chd', roms)
    assert not (roms / 'psx').exists()


def test_dolphin_only_copies_to_assigned_platform_not_card_or_wrong_system(tmp_path):
    root, card = tmp_path / 'library', tmp_path / 'card'
    folder = card / 'ROMS' / 'Nintendo - My Cube'
    folder.mkdir(parents=True)
    for name in ('gamecube/Game.rvz', 'wii/Other.rvz', 'psp/Wrong.rvz', '3ds/Cart.trim.cci', 'gc/Raw.iso'):
        put(root, name, name.encode())
    wanted = 'gamecube/Game.rvz'
    selected = [r['path'] for r in handheld.entries(root, 'android-t820', players=['dolphin-rvz'])]
    assert selected == [wanted, 'wii/Other.rvz']
    for dest in (card, folder.parent, card / 'nds', card / 'wii'):
        dest.mkdir(exist_ok=True)
        with pytest.raises(ValueError, match='existing game folder'):
            handheld.export(root, wanted, dest, 'android-t820', players=['dolphin-rvz'])
    with pytest.raises(ValueError, match='one system'):
        handheld.export(root, selected, folder, 'android-t820', players=['dolphin-rvz'])
    assert handheld.export(root, wanted, folder, 'android-t820', players=['dolphin-rvz'])['copied'] == ['Game.rvz']
    assert tree(card) == {'ROMS/Nintendo - My Cube/Game.rvz': wanted.encode()}


def test_folder_identity_from_confirmation_rechecked_before_writes(tmp_path):
    root, folder = tmp_path / 'library', tmp_path / 'card/Old favorites'
    put(root, 'psx/Game.chd', b'keep')
    folder.mkdir(parents=True)
    st = folder.stat()
    expected = (st.st_dev, st.st_ino)
    folder.rename(folder.with_name('old'))
    folder.mkdir()
    with pytest.raises(ValueError, match='destination changed'):
        handheld.export(root, 'psx/Game.chd', folder, 'muos', expected_destination=expected)
    assert tree(folder.parent) == {}
    new = folder.stat()
    handheld.export(root, 'psx/Game.chd', folder, 'muos', expected_destination=(new.st_dev, new.st_ino))
    assert tree(folder) == {'Game.chd': b'keep'}
