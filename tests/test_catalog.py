from pathlib import Path
from threading import Event, Thread

import pytest

from couchchd import catalog


def disc(root, relative, data):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def test_named_sources_keep_independent_observations_across_restart(tmp_path):
    db = tmp_path / 'state' / 'catalog.sqlite3'
    shelf = catalog.Catalog(db)
    name = 'psx/Rally.chd'
    original = disc(tmp_path / 'archive', name, b'AAA')
    second = disc(tmp_path / 'deck', name, b'BBB')
    home = shelf.add('Home', tmp_path / 'archive')
    deck = shelf.add('Deck', tmp_path / 'deck')
    assert shelf.add('Home', tmp_path / 'archive') == home
    assert shelf.entries() == []
    assert [s.status for s in shelf.refresh()] == ['online', 'online']
    shelf = catalog.Catalog(db)
    assert [(e.source_name, e.size) for e in shelf.entries(name)] == [('Deck', 3), ('Home', 3)]
    assert shelf.entries(name.swapcase()) == []
    (tmp_path / 'deck').rename(tmp_path / 'disconnected')
    old_time = shelf.entries(name)[0].last_seen
    offline, = shelf.refresh(deck.id)
    assert offline.status == 'offline' and offline.last_seen == old_time and offline.error
    assert [(e.source_name, e.status) for e in shelf.entries(name)] == [('Deck', 'offline'), ('Home', 'online')]
    (tmp_path / 'disconnected').rename(tmp_path / 'deck')
    second.rename(second.with_name('New title.chd'))
    assert shelf.refresh(deck.id)[0].status == 'online'
    assert [e.source_id for e in shelf.entries(name)] == [home.id]
    shelf.remove(home.id)
    assert original.read_bytes() == b'AAA' and shelf.entries(name) == []
    assert [e.relative for e in shelf.entries()] == ['psx/New title.chd']


def test_live_pull_does_not_take_size_or_contents_from_cache(tmp_path):
    shelf = catalog.Catalog(tmp_path / 'catalog.sqlite3')
    root = tmp_path / 'archive'
    key = 'psx/Track.chd'
    original = disc(root, key, b'first')
    source = shelf.add('Bazzite NTFS', root)
    shelf.refresh(source.id)
    original.write_bytes(b'second edition')
    destination = tmp_path / 'device'
    destination.mkdir()
    assert shelf.pull(source.id, key, destination) == (destination / key,)
    assert (destination / key).read_bytes() == original.read_bytes() == b'second edition'
    original.unlink()
    with pytest.raises((OSError, RuntimeError, ValueError)):
        shelf.pull(source.id, key, destination)
    assert (destination / key).read_bytes() == b'second edition'


def test_detached_mountpoint_cannot_erase_inventory_or_serve_a_different_file(tmp_path, monkeypatch):
    shelf = catalog.Catalog(tmp_path / 'catalog.sqlite3')
    mount = tmp_path / 'mnt'
    key = 'psx/Adventure.chd'
    source_file = disc(mount, key, b'original')
    monkeypatch.setattr(catalog, '_mountpoint', lambda path: mount)
    source = shelf.add('SD', mount)
    shelf.refresh(source.id)
    # A plain directory is exposed underneath the now-unmounted volume.
    monkeypatch.setattr(catalog, '_mountpoint', lambda path: Path('/'))
    source_file.unlink()
    assert shelf.refresh(source.id)[0].status == 'offline'
    assert shelf.entries()[0].relative == key
    source_file.write_bytes(b'not on the SD')
    destination = tmp_path / 'dest'
    destination.mkdir()
    with pytest.raises(OSError, match='mount'):
        shelf.pull(source.id, key, destination)
    assert list(destination.iterdir()) == []


def test_outdated_refresh_cannot_replace_a_newer_snapshot(tmp_path, monkeypatch):
    db = tmp_path / 'catalog.sqlite3'
    shelf = catalog.Catalog(db)
    root = tmp_path / 'archive'
    old = disc(root, 'psx/Old.chd', b'1')
    source = shelf.add('Library', root)
    shelf.refresh()
    started, release = Event(), Event()
    list_games = catalog.catalog_transport.list_games

    def slow_list(location, **options):
        games = list_games(location, **options)
        started.set()
        if not release.wait(10):
            raise RuntimeError('refresh thread timed out')
        return games

    monkeypatch.setattr(catalog.catalog_transport, 'list_games', slow_list)
    thread = Thread(target=shelf.refresh, args=(source.id,))
    thread.start()
    try:
        assert started.wait(10)
        old.rename(old.with_name('New.chd'))
        monkeypatch.setattr(catalog.catalog_transport, 'list_games', list_games)
        assert catalog.Catalog(db).refresh()[0].status == 'online'
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert [e.relative for e in shelf.entries()] == ['psx/New.chd']


def test_source_configuration_rejects_ambiguous_labels_inline_config_and_urls(tmp_path, monkeypatch):
    shelf = catalog.Catalog(tmp_path / 'catalog.sqlite3')
    monkeypatch.setattr(catalog.cloud_ingest, 'remotes', lambda: ['pi:', 'home:'])
    source = shelf.add('Pi', 'pi:/recalbox/share/roms')
    with pytest.raises(ValueError):
        shelf.add('pi', 'home:roms')
    for unsafe in (':sftp,host=foo:roms', 'pi,pass=secret:roms', 'sftp://user:pass@host/roms',
                   'unconfigured:roms', 'pi:../outside', 'relative/directory', ''):
        with pytest.raises(ValueError):
            shelf.add('Bad', unsafe)
    assert shelf.sources() == [source]


def test_catalog_inventories_every_published_format_and_only_complete_sets(tmp_path):
    shelf = catalog.Catalog(tmp_path / 'catalog.sqlite3')
    root = tmp_path / 'library'
    singles = {'psp/Racing.cso', 'dreamcast/A.chd', 'gc/Island.rvz', 'wii/Sports.rvz',
               'nes/Demo.7z', '3ds/Demo.trim.cci', 'xbox/Demo.xiso.iso'}
    for key in singles:
        disc(root, key, b'12345')
    members = ['.chd_discs/RPG/Disc 1.chd', '.chd_discs/RPG/Disc 2.chd']
    for member in members:
        disc(root, 'psx/' + member, b'disc')
    manifest = ('\n'.join(members) + '\n').encode()
    disc(root, 'psx/RPG.m3u', manifest)
    disc(root, 'psx/Incomplete.m3u', b'Missing.chd\n')
    disc(root, 'psp/Original.iso', b'not compressed')
    source = shelf.add('Archive', root)
    shelf.refresh(source.id)
    assert {e.relative for e in shelf.entries()} == singles | {'psx/RPG.m3u'}
    assert shelf.entries('psx/RPG.m3u')[0].size == len(manifest) + 8


def test_offline_remote_retains_inventory_without_storing_command_errors(tmp_path, monkeypatch):
    shelf = catalog.Catalog(tmp_path / 'catalog.sqlite3')
    monkeypatch.setattr(catalog.cloud_ingest, 'remotes', lambda: ['pi:'])
    source = shelf.add('Pi', 'pi:/recalbox/share/roms')
    game = catalog.catalog_transport.Game('psx/RPG.chd', 55, ('psx/RPG.chd',))
    monkeypatch.setattr(catalog.catalog_transport, 'list_games', lambda *a, **kw: [game])
    first, = shelf.refresh()

    def unavailable(*args, **kwargs):
        raise RuntimeError('provider failed: secret=not-for-display')

    monkeypatch.setattr(catalog.catalog_transport, 'list_games', unavailable)
    second, = shelf.refresh()
    assert first.location == second.location == 'pi:/recalbox/share/roms'
    assert second.id == source.id and second.status == 'offline'
    assert second.last_seen == first.last_seen and 'secret' not in second.error
    assert [(e.relative, e.status) for e in shelf.entries()] == [('psx/RPG.chd', 'offline')]


def test_default_catalog_lives_in_xdg_or_persistent_recalbox_data(tmp_path, monkeypatch):
    system, data = tmp_path / 'share/system', tmp_path / 'user-data'
    monkeypatch.setattr(catalog, '_RECALBOX_SYSTEM', system)
    monkeypatch.setenv('XDG_DATA_HOME', str(data))
    assert catalog.Catalog().path == data / 'couchchd/catalog.sqlite3'
    system.mkdir(parents=True)
    assert catalog.Catalog().path == system / 'couchchd/catalog.sqlite3'
