import sqlite3
from concurrent.futures import ThreadPoolExecutor
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
    discover = catalog.catalog_transport.discover

    def slow_list(location, **options):
        games = discover(location, **options)
        started.set()
        if not release.wait(10):
            raise RuntimeError('refresh thread timed out')
        return games

    monkeypatch.setattr(catalog.catalog_transport, 'discover', slow_list)
    thread = Thread(target=shelf.refresh, args=(source.id,))
    thread.start()
    try:
        assert started.wait(10)
        old.rename(old.with_name('New.chd'))
        monkeypatch.setattr(catalog.catalog_transport, 'discover', discover)
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
    scanned, = shelf.refresh(source.id)
    assert (scanned.invalid_playlists, scanned.unsupported) == (1, 0)
    assert {e.relative for e in shelf.entries() if e.kind == 'output'} == singles | {'psx/RPG.m3u'}
    assert [(e.relative, e.system) for e in shelf.entries() if e.kind == 'original'] == [('psp/Original.iso', 'psp')]
    assert shelf.entries('psx/RPG.m3u')[0].size == len(manifest) + 8


def test_nested_platforms_keep_distinct_sources_and_never_promote_originals(tmp_path):
    root, dest = tmp_path / 'master', tmp_path / 'device'
    dest.mkdir()
    data = {
        'NTFS/Games/PS1/US/Rally.chd': b'first',
        'Backup/psx/US/Rally.chd': b'other',
        'NTFS/Games/psp/Title.iso': b'original',
        'NTFS/Games/dreamcast/Disc.gdi': b'sheet',
        'NTFS/Games/PS1/Disc.cue': b'sheet',
        'NTFS/Games/PS1/Disc.zip': b'disc zip',
        'NTFS/Games/n64/Demo.z64': b'cartridge',
        'Unknown/Unknown.iso': b'unknown',
        'Collections/psx/wii/Wrong.rvz': b'ambiguous',
        'Old/psx/Archive.rar': b'unsupported',
        'Old/psx/Firmware.chd': b'firmware',
        'Old/bios/psx/Bios.chd': b'firmware',
        '.cache/psp/Ghost.iso': b'hidden',
    }
    for path, content in data.items():
        disc(root, path, content)
    (root / 'linked').symlink_to(root / 'NTFS', target_is_directory=True)
    shelf = catalog.Catalog(tmp_path / 'index.sqlite3')
    source = shelf.add('Master', root)
    scanned, = shelf.refresh(source.id)
    assert scanned.directories > 1 and (scanned.unplaced, scanned.unsupported) == (2, 1)
    entries = {(e.relative, e.kind, e.system) for e in shelf.entries()}
    assert entries == {
        ('NTFS/Games/PS1/US/Rally.chd', 'output', 'psx'), ('Backup/psx/US/Rally.chd', 'output', 'psx'),
        ('NTFS/Games/psp/Title.iso', 'original', 'psp'), ('NTFS/Games/dreamcast/Disc.gdi', 'original', 'dreamcast'),
        ('NTFS/Games/PS1/Disc.cue', 'original', 'psx'), ('NTFS/Games/PS1/Disc.zip', 'original', 'psx'),
        ('NTFS/Games/n64/Demo.z64', 'original', 'n64'),
    }
    for key in ('NTFS/Games/psp/Title.iso', 'NTFS/Games/PS1/Disc.zip', 'Collections/psx/wii/Wrong.rvz'):
        with pytest.raises(ValueError):
            shelf.pull(source.id, key, dest)
    output, = shelf.pull(source.id, 'NTFS/Games/PS1/US/Rally.chd', dest)
    assert output == dest / 'psx/US/Rally.chd'
    with pytest.raises(ValueError, match='Different file'):
        shelf.pull(source.id, 'Backup/psx/US/Rally.chd', dest)
    assert output.read_bytes() == b'first'
    assert {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()} == data


def test_named_system_folders_and_catalog_only_games_keep_exact_source_paths(tmp_path):
    root = tmp_path / 'Master'
    expected = {
        'No-Intro/Nintendo - Game Boy Advance/USA/One.gba': ('gba', 'original'),
        'No-Intro/Nintendo - Game Boy Advance/USA/Two.zip': ('gba', 'catalog_only'),
        'Redump/Sony - PlayStation Portable/Game.iso': ('psp', 'original'),
        'Sega Saturn/Japan/Game.cue': ('saturn', 'original'),
        'Nintendo - Game Boy (GB)/Demo.gb': ('gb', 'original'),
        'Sega_32/Example.bin': ('sega32x', 'catalog_only'),
        'Atari/2600/Game.zip': ('atari2600', 'catalog_only'),
        'Atari/Unsorted/Game.a52': ('atari5200', 'catalog_only'),
        'Atari/Unsorted/Game.a78': ('atari7800', 'catalog_only'),
        'Atari/Unsorted/Game.atr': ('atari800', 'catalog_only'),
        'Atari/Unsorted/Game.stx': ('atarist', 'catalog_only'),
        'Atari/Unsorted/Game.lnx': ('atarilynx', 'catalog_only'),
        'Atari/Unsorted/Game.j64': ('atarijaguar', 'catalog_only'),
        'Atari - Jaguar CD/Game.cue': ('atarijaguarcd', 'catalog_only'),
        'Atari_7800/Player.7z': ('atari7800', 'catalog_only'),
    }
    unclear = {'Atari/Mixed/Game' + ext for ext in ('.zip', '.iso', '.rom', '.bin', '.car')} | {
        'Sega 32 games/Unsorted.zip', 'Mystery/Game.iso', 'Nintendo - Game Boy (PS2)/Demo.zip',
        'Sony - PlayStation Portable/Nintendo - Game Boy Advance/Conflict.zip'}
    for path in (*expected, *unclear, 'bios/Atari/Game.a78', 'Atari/.hidden/Game.a26'):
        disc(root, path, b'read-only inventory')
    shelf = catalog.Catalog(tmp_path / 'index.sqlite3')
    source = shelf.add('Home', root)
    scanned, = shelf.refresh(source.id)
    assert {e.relative: (e.system, e.kind) for e in shelf.entries()} == expected
    assert scanned.unplaced == len(unclear) and scanned.unsupported == 0
    assert set(scanned.unplaced_examples.splitlines()) <= unclear and scanned.unplaced_examples
    dest = tmp_path / 'dest'
    dest.mkdir()
    for path, (_, kind) in expected.items():
        if kind == 'catalog_only':
            with pytest.raises(ValueError, match='Not a selectable'):
                shelf.pull(source.id, path, dest)
    assert not list(dest.iterdir())


def test_direct_atari_root_reports_ambiguous_archive_without_reading_it(tmp_path):
    root = tmp_path / 'Atari'
    disc(root, 'A.a26', b'cart')
    disc(root, 'Unsorted/A.zip', b'not opened')
    shelf = catalog.Catalog(tmp_path / 'index.sqlite3')
    source = shelf.add('Atari shelf', root)
    scanned, = shelf.refresh(source.id)
    entry, = shelf.entries()
    assert (entry.relative, entry.system, entry.kind) == ('Atari/A.a26', 'atari2600', 'catalog_only')
    assert (scanned.unplaced, scanned.platform_folders, scanned.unplaced_examples) == (1, 1, 'Atari/Unsorted/A.zip')


def test_offline_remote_retains_inventory_without_storing_command_errors(tmp_path, monkeypatch):
    shelf = catalog.Catalog(tmp_path / 'catalog.sqlite3')
    monkeypatch.setattr(catalog.cloud_ingest, 'remotes', lambda: ['pi:'])
    source = shelf.add('Pi', 'pi:/recalbox/share/roms')
    game = catalog.catalog_transport.Game('psx/RPG.chd', 55, ('psx/RPG.chd',))
    snapshot = catalog.catalog_transport.Discovery([game], 2, 1, 0, 0)
    monkeypatch.setattr(catalog.catalog_transport, 'discover', lambda *a, **kw: snapshot)
    first, = shelf.refresh()

    def unavailable(*args, **kwargs):
        raise RuntimeError('provider failed: secret=not-for-display')

    monkeypatch.setattr(catalog.catalog_transport, 'discover', unavailable)
    second, = shelf.refresh()
    assert first.location == second.location == 'pi:/recalbox/share/roms'
    assert second.id == source.id and second.status == 'offline'
    assert second.last_seen == first.last_seen and 'secret' not in second.error
    assert [(e.relative, e.status) for e in shelf.entries()] == [('psx/RPG.chd', 'offline')]
    assert (second.directories, second.platform_folders) == (2, 1)


def test_old_database_migrates_offline_without_discarding_observations(tmp_path):
    db = tmp_path / 'old.sqlite3'
    with sqlite3.connect(db) as old:
        old.executescript("""
            CREATE TABLE source (id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL,
                location TEXT NOT NULL, status TEXT DEFAULT 'new', last_seen REAL, error TEXT DEFAULT '',
                mountpoint TEXT, generation INTEGER DEFAULT 0);
            CREATE TABLE entry (source_id TEXT NOT NULL REFERENCES source(id) ON DELETE CASCADE,
                relative TEXT NOT NULL, size INTEGER NOT NULL, PRIMARY KEY(source_id,relative)) WITHOUT ROWID;
            """)
        old.execute('INSERT INTO source(id,name,name_key,location,status,last_seen) VALUES (?,?,?,?,?,?)',
                    ('old', 'Archive', 'archive', str(tmp_path / 'unmounted'), 'online', 42))
        old.execute("INSERT INTO entry VALUES('old','backup/PS1/Rally.chd',123)")
        old.execute("INSERT INTO entry VALUES('old','psx/firmware.chd',456)")
    shelf = catalog.Catalog(db)
    offline, = shelf.refresh('old')
    assert offline.status == 'offline' and offline.last_seen == 42 and offline.directories is None
    entry, = catalog.Catalog(db).entries()
    assert (entry.relative, entry.kind, entry.system, entry.size) == ('backup/PS1/Rally.chd', 'output', 'psx', 123)


def test_controller_save_scans_empty_parent_then_refresh_finds_readonly_original(tmp_path, monkeypatch):
    from couchchd.ui.catalog_controls import CatalogControls

    root = tmp_path / 'master'
    (root / 'Collections/Disc').mkdir(parents=True)
    monkeypatch.setattr(catalog, '_RECALBOX_SYSTEM', tmp_path / 'no-recalbox')
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'state'))

    class Screen:
        def _work(self, title, read, loaded):
            # Match the real bridge seam: source I/O runs on the worker, then
            # presentation resumes on the caller. No external GUI or SDL needed.
            with ThreadPoolExecutor(max_workers=1) as worker:
                result = worker.submit(read).result()
            loaded(result)

        def _show(self, kind, title, body, rows):
            self.kind, self.body, self.rows = kind, body, rows

        def _report(self, message):
            self.status = message

        def _hide(self):
            self.kind = None

        def switchTab(self, tab):
            self.tab = tab

    screen = Screen()
    controls = CatalogControls(screen)
    controls.chosen_directory(root)
    controls.activate('catalog_name', 'Home')
    controls.activate('catalog_save', '')
    assert screen.kind == 'catalog_source' and '3 folders scanned' in screen.body
    assert 'No supported platform folder found' in screen.body
    original = disc(root, 'Collections/Disc/psp/Untouched.iso', b'not content-verified')
    controls.activate('catalog_open', '')
    assert not controls.files  # Browsing the cache doesn't scan again.
    source, = controls.sources
    controls.activate('catalog_source', source)
    controls.activate('catalog_refresh_source', '')
    assert '0 ready to copy · 1 original' in screen.body
    controls.activate('catalog_systems', source)
    assert [r['label'] for r in screen.rows] == ['PlayStation Portable']
    controls.activate('catalog_system', 'psp')
    choice, = screen.rows
    assert choice['value'] == 'Needs conversion'
    controls.activate(choice['key'], choice['arg'])
    location, = screen.rows
    controls.activate(location['key'], location['arg'])
    assert str(original) in screen.body and 'catalog_pull' not in {r['key'] for r in screen.rows}
    controls.activate('catalog_ingest', '')
    assert screen.ingest == str(root) and screen.tab == 3 and screen.kind is None
    assert original.read_bytes() == b'not content-verified'
    archived = disc(root, 'Sega 32/Game.7z', b'not opened')
    ambiguous = disc(root, 'Atari/Game.zip', b'not opened')
    controls.activate('catalog_source', source)
    controls.activate('catalog_refresh_source', '')
    assert '1 inventory only' in screen.body
    controls.activate('catalog_unplaced', '')
    assert 'Atari' in screen.body and str(ambiguous) == screen.rows[0]['detail']
    assert controls.back('catalog_unplaced') and screen.kind == 'catalog_source'
    controls.activate('catalog_systems', source)
    assert {r['label'] for r in screen.rows} == {'PlayStation Portable', 'Sega 32X'}
    controls.activate('catalog_system', 'sega32x')
    choice, = screen.rows
    assert choice['value'] == 'Inventory only'
    controls.activate(choice['key'], choice['arg'])
    location, = screen.rows
    controls.activate(location['key'], location['arg'])
    assert str(archived) in screen.body and 'compatibility have not been checked' in screen.body
    assert [r['key'] for r in screen.rows] == ['back']


def test_default_catalog_lives_in_xdg_or_persistent_recalbox_data(tmp_path, monkeypatch):
    system, data = tmp_path / 'share/system', tmp_path / 'user-data'
    monkeypatch.setattr(catalog, '_RECALBOX_SYSTEM', system)
    monkeypatch.setenv('XDG_DATA_HOME', str(data))
    assert catalog.Catalog().path == data / 'couchchd/catalog.sqlite3'
    system.mkdir(parents=True)
    assert catalog.Catalog().path == system / 'couchchd/catalog.sqlite3'
