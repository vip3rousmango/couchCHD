"""Exercise the worker-backed library workflows using only controller row actions."""
import os
from pathlib import Path
from threading import Event, get_ident
import time
from zipfile import ZipFile

import pytest

pytest.importorskip('PySide6')
pytest.importorskip('pygame')
pytest.importorskip('vdf')

from PySide6.QtCore import QCoreApplication, QSettings

from couchchd import cso, handheld_profiles
from couchchd.ui import bridge
from test_cso import _iso


def _until(condition):
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.005)
    assert condition()


@pytest.fixture
def controller(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    prefs = QSettings(str(tmp_path / 'preferences.ini'), QSettings.Format.IniFormat)
    prefs.setValue('pspFormat', 'cso')
    prefs.setValue('cleanupPolicy', 'immediate')
    monkeypatch.setattr(bridge, '_settings', lambda: prefs)
    state = bridge.Bridge(None)

    def settle():
        deadline = time.monotonic() + 15
        while state.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert not state.busy, state.status

    def choose(key, arg=None):
        rows = state.modalRows if state.modal['kind'] else state.rows
        index = next((i for i, r in enumerate(rows) if r['key'] == key and (arg is None or str(arg) == r['arg'])), None)
        assert index is not None, (key, arg, state.modal, rows)
        state.activate(index, False)
        settle()

    try:
        yield state, choose, settle
    finally:
        state.close()


def test_plan_marks_and_reclaims_only_reviewed_disc(controller, tmp_path, monkeypatch):
    state, choose, settle = controller
    monkeypatch.setattr(bridge.hardware, 'free_bytes', lambda _: 12 * 1024**3)
    root = tmp_path / 'roms'
    (root / 'psp').mkdir(parents=True)
    originals = [root / 'psp' / name for name in ('A.iso', 'B.iso')]
    for p in originals:
        p.write_bytes(_iso())
        os.utime(p, (1600000000, 1600000000))
    state._library(root)
    settle()
    choose('tool_plan')
    choose('tool_systems')
    choose('tool_all_systems')
    choose('tool_system', 'psp')
    choose('back')
    choose('tool_goal')
    choose('tool_set_goal', 0)
    choose('tool_preview')
    assert state.modal['kind'] == 'plan_preview', state.modal
    assert set(state.tools.plan['sources']) == set(map(str, originals))
    assert 'staging' in state.modal['body'] and 'backups' in state.modal['body']
    choose('tool_run_plan')
    choose('back')
    assert state.modal['kind'] == 'plan_preview' and all(p.exists() for p in originals)
    choose('tool_run_plan')
    choose('accept')
    assert state.modal['title'] == 'Batch complete', state.modal
    assert state.queueModel.verified == 2 and state.policy == 'immediate'
    assert 'Compression difference:' in state.modal['body'] and 'Library free now:' in state.modal['body']
    outputs = [p.with_suffix('.cso') for p in originals]
    assert all(p.is_file() for p in outputs)
    choose('tool_review')
    discs = [r for r in state.modalRows if r['key'] == 'tool_disc']
    assert {r['detail'] for r in discs} == set(map(str, outputs))
    assert all(r['value'] == 'Not tested' for r in discs)
    chosen = next(r for r in discs if r['detail'] == str(outputs[0]))
    choose('tool_disc', chosen['arg'])
    choose('tool_mark')
    assert '1 marked play-tested' in state.modal['body']
    choose('tool_reclaim')
    preview = state.tools.reclaim
    assert preview['count'] == 1 and preview['batches']
    backup, = map(Path, preview['files'])
    assert backup.is_file() and backup.name == originals[0].name
    assert any(r['detail'] == str(backup) for r in state.modalRows)
    choose('tool_reclaim_confirm')
    assert state.modalRows[0]['key'] == 'back' and 'cannot be undone' in state.modal['body']
    choose('back')
    assert state.modal['kind'] == 'reclaim_preview' and backup.exists()
    choose('tool_reclaim_confirm')
    choose('accept')
    assert state.modal['title'] == 'Reclaim complete', state.modal
    assert not backup.exists() and all(p.exists() for p in outputs)
    assert list(root.rglob(originals[1].name)), 'Untested original must remain in backup'


def test_browse_external_source_check_and_copy_without_opening_working_library(controller, tmp_path):
    state, choose, _ = controller
    root, card = tmp_path / 'windows-roms', tmp_path / 'RG405M'
    (root / 'psp').mkdir(parents=True)
    (root / 'psx').mkdir()
    (root / 'psx' / 'Broken.cue').write_text('FILE "missing.bin" BINARY\n TRACK 01 MODE2/2352\n INDEX 01 00:00:00\n')
    iso = tmp_path / 'Pocket.iso'
    iso.write_bytes(_iso())
    source = root / 'psp' / 'Pocket.cso'
    cso.convert(iso, source, threads=1, cancel=Event())
    (card / 'psx').mkdir(parents=True)
    existing = card / 'psx' / 'Favorite.chd'
    existing.write_bytes(b'already on the handheld')
    before = set(root.rglob('*'))
    state.candidates = [root, card]
    state._draw(True)

    def pick(path):
        choose('browse', path)
        choose('use_dir')

    choose('tool_health')
    choose('tool_health_browse')
    pick(root)
    assert state.modal['kind'] == 'health_report' and 'No files changed' in state.modal['body']
    assert any('Next:' in r['detail'] and 'Broken.cue' in r['detail'] for r in state.modalRows)
    state.back(); state.back()
    for repeat in (False, True):
        choose('tool_export')
        if repeat:
            choose('tool_pack_repeat')
            assert state.tools.selected == {'psp/Pocket.cso'}
        else:
            choose('tool_export_browse')
            pick(root)
            choose('tool_pack_toggle', 'psp/Pocket.cso')
        choose('tool_pack_dest')
        if repeat:
            choose('tool_last_folder')
        else:
            pick(card)
        assert state.modal['kind'] == 'confirm' and str(card) in state.modal['body']
        assert repeat or not (card / 'psp' / 'Pocket.cso').exists()
        choose('accept')
        assert state.modal['title'] == 'Handheld export complete', state.modal
        assert ('Identical files skipped: 1' if repeat else 'Files copied and verified: 1') in state.modal['body']
        state.back()
        state.tools = type(state.tools)(state)  # Reopen using only the persisted preferences.
    assert (card / 'psp' / 'Pocket.cso').read_bytes() == source.read_bytes()
    assert existing.read_bytes() == b'already on the handheld'
    assert set(root.rglob('*')) == before and state.backend is None

    # A different directory at the old mount path is not the previous destination.
    card.rename(tmp_path / 'original-card')
    card.mkdir()
    choose('tool_export')
    choose('tool_pack_repeat')
    choose('tool_pack_dest')
    choose('tool_last_folder')
    assert state.modal['kind'] == 'pack_changed' and not list(card.iterdir())
    choose('tool_pack_dest')
    pick(card)
    assert state.modal['kind'] == 'confirm'
    # Replacement *after* confirmation opens is also rejected by the copy worker.
    card.rename(tmp_path / 'second-card')
    card.mkdir()
    choose('accept')
    assert state.modal['title'] == 'Operation stopped', state.modal
    assert not list(card.iterdir())


def test_saved_wrapped_home_library_can_send_to_default_rg405m(controller, tmp_path, monkeypatch):
    from couchchd.catalog import Catalog

    state, choose, _ = controller
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'app-data'))
    root = tmp_path / 'NTFS'
    key = 'Emulation/roms/PS1/Disc.chd'
    (root / key).parent.mkdir(parents=True)
    (root / key).write_bytes(b'disc')
    Catalog().add('Windows library', root)
    state._draw(True)
    choose('tool_export')
    choose('tool_saved')
    choose('tool_saved_source', root)
    choose('tool_pack_toggle', key)
    assert any(r['key'] == 'tool_wifi' for r in state.modalRows)
    assert state.backend is None


@pytest.mark.parametrize('leave', ['back', 'exit', 'close'])
def test_wifi_selection_and_explicit_approval(controller, tmp_path, monkeypatch, leave):
    from couchchd import wifi_sender

    state, choose, _ = controller
    library = tmp_path / 'roms'
    (library / 'psx').mkdir(parents=True)
    for name in ('Selected', 'Other'):
        (library / 'psx' / f'{name}.chd').write_bytes(name.encode())
    gui_thread = get_ident()
    calls = []
    approved, stopped = Event(), Event()
    firewall_allowed, firewall_closed = Event(), Event()

    class Firewall:
        network = '192.168.1.0/24'

        def allow(self, stop):
            assert get_ident() != gui_thread
            firewall_allowed.set()
            return True

        def close(self):
            firewall_closed.set()

    monkeypatch.setattr('couchchd.ui.library_tools.HomeFirewall.discover', lambda _: Firewall())

    class Session:
        address = '192.168.1.11:42000'
        pair_code = ''
        status = 'Waiting for receiver'
        closed = False
        transfer = {'phase': 'waiting'}

        def approve(self):
            assert get_ident() != gui_thread
            self.pair_code = ''
            self.transfer = dict(phase='connected', peer='192.168.1.25', device='RG405M')
            approved.set()

        def close(self):
            assert get_ident() != gui_thread
            stopped.set()

    session = Session()

    def start(root, selected, profile, *, players, cancel):
        assert cancel is state.tools._wifi_stop and not cancel.is_set()
        assert get_ident() != gui_thread
        calls.append((root, selected, profile, players))
        return session

    monkeypatch.setattr(wifi_sender, 'start_sender', start)
    state.candidates = [library]
    state._draw(True)
    choose('tool_export')
    choose('tool_profiles')
    choose('tool_profile_set', 'android-t618')
    choose('tool_players')
    choose('tool_player_toggle', 'pcsx-rearmed')
    choose('back')
    choose('tool_export_browse')
    choose('browse', library)
    choose('use_dir')
    assert not any(r['key'] == 'tool_wifi' for r in state.modalRows)
    choose('tool_pack_toggle', 'psx/Selected.chd')
    assert not calls
    choose('tool_wifi')
    _until(lambda: state.transferView.get('address') == session.address)
    assert calls == [(library, ('psx/Selected.chd',), 'android-t618', ('pcsx-rearmed',))]
    assert any('couchCHD/receive' in r['detail'] for r in state.modalRows)
    _until(lambda: any(r['key'] == 'tool_wifi_firewall' for r in state.modalRows))
    assert not firewall_allowed.is_set()
    if leave == 'back':
        choose('tool_wifi_firewall')
        _until(firewall_allowed.is_set)
    assert not any(r['key'] == 'tool_wifi_approve' for r in state.modalRows)
    session.pair_code = '1234-5678-9abc'
    session.transfer = dict(phase='pairing', device='RG405M', peer='192.168.1.25')
    _until(lambda: session.pair_code in state.modal['body'])
    assert not approved.is_set() and state.modalRows[0]['key'] == 'back'
    if leave != 'exit':
        choose('tool_wifi_approve', session.pair_code)
        _until(approved.is_set)
        session.transfer = dict(phase='sending', device='RG405M', file='Selected.chd', sent=128, total=512, rate=64)
        _until(lambda: state.transferView.get('fraction') == .25)
        assert state.transferView['device'] == 'RG405M' and state.transferView['speed'].endswith('/s')
    if leave == 'back':
        choose('back')
        assert state.modal['kind'] == 'pack_games' and state.tools.selected == {'psx/Selected.chd'}
    elif leave == 'exit':
        state.exitPrompt()
        assert state.modal['kind'] == 'confirm' and not approved.is_set()
    else:
        state.close()
    _until(stopped.is_set)
    _until(firewall_closed.is_set)
    assert firewall_allowed.is_set() == (leave == 'back')


@pytest.fixture
def nearby(monkeypatch):
    """Only this explicitly opened protocol listener can report a peer; no LAN dependency."""
    from couchchd import wifi_sender

    gui_thread = get_ident()
    sessions = []

    class ReceiverSession:
        address = '192.168.1.10:42001'
        pair_code = ''
        status = 'Listening'
        closed = False

        def __init__(self, cancel):
            self.cancel = cancel
            self.transfer = {'phase': 'waiting'}
            self.offers = []
            self.stopped = Event()
            self.adverts = ()
            self.discovery_error = False
            self.invitations = []
            self.discovery_closed = Event()

        def candidate(self):
            self.pair_code = '1111-2222-3333'
            self.transfer = dict(phase='pairing', device='My handheld', peer='192.168.1.55')

        def approve(self):
            assert get_ident() != gui_thread
            self.pair_code = ''
            self.transfer = dict(self.transfer, phase='approved')

        def offer(self, root, selected, profile, *, players):
            assert get_ident() != gui_thread and not self.cancel.is_set()
            assert self.transfer['phase'] == 'connected'
            self.offers.append((root, selected, profile, players))
            self.transfer = dict(self.transfer, phase='offered')

        def close(self):
            assert get_ident() != gui_thread
            self.closed = True
            self.stopped.set()

    class Browser:
        def __init__(self, address):
            self.session = sessions[-1]
            assert address == self.session.address and get_ident() != gui_thread

        def poll(self):
            assert get_ident() != gui_thread
            if self.session.discovery_error:
                raise OSError('Broadcast unavailable')
            return self.session.adverts

        def invite(self, ip):
            assert get_ident() != gui_thread
            if ip not in dict(self.session.adverts):
                return False
            self.session.invitations.append(ip)
            return True

        def close(self):
            self.session.discovery_closed.set()

    def start(*, cancel):
        assert get_ident() != gui_thread
        assert not sessions or sessions[-1].stopped.is_set(), 'listener lifetimes must not overlap'
        session = ReceiverSession(cancel)
        sessions.append(session)
        return session

    monkeypatch.setattr(wifi_sender, 'start_pairing', start)
    monkeypatch.setattr('couchchd.receiver_discovery.NearbyReceivers', Browser)
    monkeypatch.setattr('couchchd.ui.library_tools.HomeFirewall.discover', lambda _: None)
    return sessions


def _connect(state, choose, sessions):
    choose('tool_devices')
    _until(lambda: state.transferView.get('phase') == 'waiting')
    session = sessions[-1]
    assert not state.transferView['peer'] and not session.offers
    assert any('Enter host IP' in r['detail'] and '192.168.1.10' in r['detail'] for r in state.modalRows)
    session.candidate()
    _until(lambda: session.pair_code in state.modal['body'])
    assert 'My handheld (192.168.1.55)' in state.modal['body']
    assert state.modalRows[0]['key'] == 'back'  # Repeated A cannot approve a newly arrived peer.
    choose('tool_wifi_approve', session.pair_code)
    _until(lambda: state.transferView['phase'] == 'approved')
    assert not any(r['key'] == 'tool_wifi_choose' for r in state.modalRows)
    assert session.discovery_closed.is_set()
    session.transfer = dict(session.transfer, phase='connected')
    _until(lambda: state.transferView['phase'] == 'connected')
    assert 'No games offered' in state.modal['body']
    return session


def test_nearby_invitation_is_not_pairing_and_discovery_is_optional(controller, nearby):
    state, choose, _ = controller
    state.switchTab(3)
    choose('tool_devices')
    _until(lambda: state.transferView.get('phase') == 'waiting')
    session, = nearby
    session.adverts = (('192.168.1.55', 'RG405M'), ('192.168.1.56', 'Second handheld'))
    scene_resets = []
    state.sceneChanged.connect(lambda reset: scene_resets.append(reset))

    def found():
        return [r for r in state.modalRows if r['key'] == 'tool_nearby_receiver']

    _until(lambda: len(found()) == 2)
    assert [r['label'] for r in found()] == ['RG405M', 'Second handheld']
    assert all(r['value'] == 'Found · Not paired' for r in found()) and scene_resets[-1]
    choose('tool_nearby_receiver', '192.168.1.56')
    _until(lambda: 'Invitation sent' in state.modal['body'])
    assert session.invitations == ['192.168.1.56']
    assert state.transferView['phase'] == 'waiting' and not state.transferView['peer'] and not state.transferView['device']
    assert not any(r['key'] in ('tool_wifi_approve', 'tool_wifi_choose') for r in state.modalRows)
    # An expired entry on the last rendered frame cannot invite the wrong device.
    session.adverts = (('192.168.1.56', 'Different receiver'),)
    choose('tool_nearby_receiver', '192.168.1.55')
    _until(lambda: len(found()) == 1)
    assert found()[0]['label'] == 'Different receiver' and scene_resets[-1]
    assert session.invitations == ['192.168.1.56']
    session.discovery_error = True
    _until(lambda: not found() and 'discovery unavailable' in state.modal['body'])
    assert session.discovery_closed.is_set() and any('Enter host IP' in r['detail'] for r in state.modalRows)
    session.candidate()  # The listener still accepts a real receiver reaching this host directly.
    _until(lambda: state.transferView['phase'] == 'pairing')
    assert 'My handheld (192.168.1.55)' in state.modal['body'] and '1111-2222-3333' in state.modal['body']
    choose('back')
    _until(session.stopped.is_set)


def test_pair_first_offer_only_explicit_games_from_one_system(controller, nearby, tmp_path):
    state, choose, _ = controller
    library, empty = tmp_path / 'roms', tmp_path / 'empty'
    empty.mkdir()
    for path in ('psx/Keep.chd', 'psx/Other.chd', 'psp/Other system.chd'):
        game = library / path
        game.parent.mkdir(exist_ok=True, parents=True)
        game.write_bytes(path.encode())
    state.candidates = [library, empty]
    state._draw(True)
    session = _connect(state, choose, nearby)
    choose('tool_wifi_choose')
    choose('tool_profiles')
    assert 'does not pair' in state.modal['body']
    choose('tool_profile_set', 'android-t618')
    choose('tool_players')
    choose('tool_player_toggle', 'pcsx-rearmed')
    choose('tool_player_toggle', 'ppsspp-1.17.1')
    choose('back')
    choose('tool_export_browse')
    state.back()
    assert state.modal['kind'] == 'export_source'
    choose('tool_export_browse')
    choose('browse', empty)
    choose('use_dir')
    assert 'No eligible games' in state.modal['body'] and 'Paired with My handheld' in state.modal['body']
    state.back()
    choose('tool_export_browse')
    choose('browse', library)
    choose('use_dir')
    assert state.modal['kind'] == 'pack_system'
    choose('tool_filter_by', 'psp')
    choose('tool_pack_toggle', 'psp/Other system.chd')
    choose('tool_filter')
    choose('tool_filter_by', 'psx')
    assert not state.tools.selected and not any(r['key'] == 'tool_wifi' for r in state.modalRows)
    choose('tool_pack_toggle', 'psx/Keep.chd')
    assert not session.offers and not any(r['key'] == 'tool_pack_dest' for r in state.modalRows)
    choose('tool_wifi')
    _until(lambda: state.transferView['phase'] == 'offered')
    assert session.offers == [(library, ('psx/Keep.chd',), 'android-t618', ('pcsx-rearmed', 'ppsspp-1.17.1'))]
    assert len(nearby) == 1 and not session.cancel.is_set() and state.backend is None
    choose('back')
    _until(session.stopped.is_set)
    assert state.transferView == {} and not state.modal['kind']


def test_devices_workflow_limited_to_explicit_foreground_screen(controller, nearby):
    state, choose, _ = controller
    state.switchTab(3)
    assert not nearby
    session = _connect(state, choose, nearby)
    state.switchTab(0)
    assert state.tab == 3
    choose('tool_wifi_choose')
    state.back()
    assert state.modal['kind'] == 'pack_wifi' and not session.cancel.is_set()
    choose('back')
    assert not state.modal['kind'] and state.tab == 3 and session.cancel.is_set()
    # Immediate re-open is serialized behind any pending network/firewall teardown.
    choose('tool_devices')
    _until(lambda: state.transferView.get('phase') == 'waiting')
    assert len(nearby) == 2 and not state.transferView['device']
    state.exitPrompt()
    assert state.modal['kind'] == 'confirm' and nearby[-1].cancel.is_set()
    choose('back')
    choose('tool_export')
    assert state.modal['kind'] == 'export_source' and state.transferView == {}
    _until(nearby[-1].stopped.is_set)


@pytest.mark.parametrize('leave', ['disconnect', 'exit', 'back'])
def test_pairing_teardown_during_source_read(controller, nearby, tmp_path, monkeypatch, leave):
    from couchchd.app import Backend

    state, choose, settle = controller
    state.candidates = [tmp_path]
    state._draw(True)
    session = _connect(state, choose, nearby)
    choose('tool_wifi_choose')
    choose('tool_export_browse')
    choose('browse', tmp_path)
    reading, finish = Event(), Event()

    def read_source(*args, **kwargs):
        reading.set()
        assert finish.wait(5)
        return []

    monkeypatch.setattr(Backend, 'export_entries', read_source)
    state.activate(next(i for i, r in enumerate(state.modalRows) if r['key'] == 'use_dir'), False)
    try:
        _until(reading.is_set)
        if leave == 'exit':
            state.exitPrompt()
            assert state.modal['kind'] == 'confirm'
        elif leave == 'disconnect':
            session.status = 'Receiver disconnected.'
            session.transfer = dict(session.transfer, phase='disconnected')
            session.closed = True
            _until(lambda: state.transferView.get('phase') == 'disconnected')
            assert state.modal['kind'] == 'pack_wifi' and not any(r['key'] == 'tool_wifi_choose' for r in state.modalRows)
        else:
            state.back()  # Abort browsing without discarding the connection.
            assert state.modal['kind'] == 'pack_wifi' and not session.cancel.is_set()
            state.back()  # Leave the whole workflow while the read is still running.
            assert not state.modal['kind']
        _until(session.stopped.is_set)
        assert session.cancel.is_set()
    finally:
        finish.set()
    settle()
    assert state.modal['kind'] == {'back': '', 'exit': 'confirm', 'disconnect': 'pack_wifi'}[leave]


def test_reported_model_suggests_profiles_without_selecting_one(controller, nearby):
    state, choose, _ = controller
    state._draw(True)
    session = _connect(state, choose, nearby)
    choose('tool_wifi_choose')
    choose('tool_profiles')
    assert [r['arg'] for r in state.modalRows] == list(state.tools.profiles)
    assert not any('Model suggestion' in r['detail'] for r in state.modalRows)
    choose('tool_profile_set', 'manual')
    original_players = state.tools.players.copy()
    session.transfer = dict(session.transfer, device='rG405m')
    _until(lambda: state.transferView['device'] == 'rG405m')
    choose('tool_profiles')
    rows = state.modalRows
    matches = [r['arg'] for r in rows if 'Model suggestion' in r['detail']]
    assert set(matches) == {'rg405m', 'android-t618'} and [r['arg'] for r in rows[:len(matches)]] == matches
    assert {r['arg'] for r in rows} == set(state.tools.profiles)
    assert [r['arg'] for r in rows if r['value'] == 'Current'] == ['manual']
    assert state.tools.players == original_players and state.prefs.value('handheld/profile') == 'manual'
    choose('tool_profile_set', 'android-t618')
    assert state.tools.profile['id'] == 'android-t618' and state.transferView['phase'] == 'connected'


def test_dismissing_while_listener_opens_cannot_replace_new_session(controller, nearby, monkeypatch):
    from couchchd import wifi_sender

    state, choose, _ = controller
    start = wifi_sender.start_pairing
    starting, finish_start = Event(), Event()

    def delayed(*, cancel):
        session = start(cancel=cancel)
        if len(nearby) == 1:
            starting.set()
            assert finish_start.wait(5)
        return session

    monkeypatch.setattr(wifi_sender, 'start_pairing', delayed)
    state._draw(True)
    choose('tool_devices')
    try:
        _until(starting.is_set)
        choose('back')
        choose('tool_devices')
        assert state.transferView['phase'] == 'starting'
        assert len(nearby) == 1 and nearby[0].cancel.is_set()
    finally:
        finish_start.set()
    _until(lambda: state.transferView.get('phase') == 'waiting')
    assert len(nearby) == 2 and nearby[0].stopped.is_set() and not nearby[1].cancel.is_set()


def test_cancel_while_checking_offered_files(controller, nearby, tmp_path, monkeypatch):
    state, choose, _ = controller
    library = tmp_path / 'psx'
    library.mkdir()
    (library / 'Game.chd').write_bytes(b'disc')
    state.candidates = [library]
    state._draw(True)
    session = _connect(state, choose, nearby)
    checking = Event()

    def offer(*args, **kwargs):
        checking.set()
        assert session.cancel.wait(5)
        raise ValueError('Cancelled during read')

    monkeypatch.setattr(session, 'offer', offer)
    choose('tool_wifi_choose')
    choose('tool_export_browse')
    choose('browse', library)
    choose('use_dir')
    choose('tool_pack_toggle', 'Game.chd')
    choose('tool_wifi')
    _until(checking.is_set)
    assert state.transferView['phase'] == 'preparing'
    choose('back')
    _until(session.stopped.is_set)
    assert not state.modal['kind'] and not state.transferView and not session.offers


def test_platform_profile_limits_players_formats_and_destination(controller, tmp_path):
    state, choose, _ = controller
    profile, psx = 'android-t618', 'pcsx-rearmed'
    library, card = tmp_path / 'roms', tmp_path / 'My PSP games'
    (library / 'psp').mkdir(parents=True)
    (library / 'psx').mkdir()
    card.mkdir()
    (library / 'psx' / 'Rally.chd').write_bytes(b'PS1 disc')
    (library / 'psp' / 'Rally.chd').write_bytes(b'PSP disc')
    iso = tmp_path / 'Tiny.iso'
    iso.write_bytes(_iso())
    source = library / 'psp' / 'Tiny.cso'
    cso.convert(iso, source, threads=1, cancel=Event())
    state.candidates = [library, card]
    state._draw(True)

    choose('tool_export')
    choose('tool_profiles')
    choose('tool_profile_set', profile)
    assert not any(r['key'] == 'tool_export_browse' for r in state.modalRows)
    assert 'No player selected' in state.modal['body']
    choose('tool_profile_info')
    assert any('SHA-256' in r['detail'] for r in state.modalRows)
    choose('back')
    choose('tool_players')
    choose('tool_player_toggle', 'ppsspp-1.17.1')
    choose('tool_player_toggle', 'ppsspp')
    choose('tool_player_toggle', psx)
    assert state.tools.players == {'ppsspp', psx}
    choose('back')
    state.tools = type(state.tools)(state)
    assert state.tools.profile['id'] == profile and state.tools.players == {'ppsspp', psx}
    choose('tool_export_browse')
    choose('browse', library)
    choose('use_dir')
    assert state.modal['kind'] == 'pack_system'
    assert {r['arg'] for r in state.modalRows} == {'psx', 'psp'}
    choose('tool_filter_by', 'psx')
    choose('tool_pack_bulk')
    choose('tool_filter')
    choose('tool_filter_by', 'psp')
    assert not state.tools.selected
    assert [r['arg'] for r in state.modalRows if r['key'] == 'tool_pack_toggle'] == ['psp/Tiny.cso']
    choose('tool_pack_bulk')
    choose('tool_pack_dest')
    assert 'not the card root' in state.modal['body']
    choose('browse', card)
    choose('use_dir')
    choose('accept')
    assert (card / source.name).read_bytes() == source.read_bytes() and not (card / 'psp').exists()

    # A vanished title is never silently replaced in the repeat selection.
    source.unlink()
    state.back()
    choose('tool_export')
    choose('tool_pack_repeat')
    assert '1 previously selected' in state.modal['body']
    assert not state.tools.selected and not any(r['key'] == 'tool_pack_dest' for r in state.modalRows)
    state.back()
    choose('tool_players')
    choose('tool_player_toggle', psx)
    choose('back')
    choose('tool_pack_repeat')
    assert 'No eligible games' in state.modal['body']
    assert any('CSO' in r['detail'] for r in state.modalRows)


def test_stale_profile_and_player_preferences_require_new_choices(controller):
    state, choose, _ = controller
    state.prefs.setValue('handheld/profile', 'retired-profile')
    state.prefs.setValue(f'handheld/{handheld_profiles.DEFAULT_PROFILE}/players', 'corrupt preference')
    state.tools = type(state.tools)(state)
    state._draw(True)
    choose('tool_export')
    assert state.modal['kind'] == 'pack_profiles' and 'unavailable' in state.modal['body']
    state.back()
    assert not state.modal['kind']
    choose('tool_export')
    choose('tool_profile_set', handheld_profiles.DEFAULT_PROFILE)
    assert not state.tools.players and not any(r['key'] == 'tool_export_browse' for r in state.modalRows)


def test_archive_inspect_and_optional_workspace(controller, tmp_path):
    state, choose, settle = controller
    root, scratch = tmp_path / 'roms', tmp_path / 'scratch'
    scratch.mkdir()
    psp = root / 'psp'
    psp.mkdir(parents=True)
    archive = psp / 'Pocket.zip'
    with ZipFile(archive, 'w') as zf:
        zf.writestr('Pocket.iso', _iso())
    os.utime(archive, (1600000000, 1600000000))
    state._library(root)
    settle()
    state.candidates = [scratch]
    state.settings()
    choose('scratch_settings')
    choose('scratch_browse')
    choose('browse', scratch)
    choose('use_dir')
    assert state.prefs.value('scratchDir') == str(scratch.resolve()) and state.root == root.resolve()
    state.back()

    def inspect_archive():
        index = next(i for i, r in enumerate(state.rows) if r['arg'] == str(archive))
        state.activate(index, True)
        return state.modal['body']

    streamed = inspect_archive()
    assert 'no unpacked ISO' in streamed and 'Original ZIP always kept' in streamed
    state.back()
    state.primary()
    assert 'Original ZIPs always stay' in state.modal['body']
    choose('back')
    state.settings()
    choose('psp_formats')
    choose('psp_format', 'chd')
    state.back()
    staged = inspect_archive()
    assert 'ZIP → CHD' in staged and 'packed /' in staged and 'scratch' in staged
    assert str(scratch.resolve()) in staged and 'staged output' in staged
    state.back()
    state.settings()
    choose('scratch_settings')
    choose('scratch_auto')
    assert state.scratch_dir == '' and state.prefs.value('scratchDir') == ''
    assert archive.is_file() and not list(scratch.iterdir())
