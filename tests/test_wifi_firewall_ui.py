"""Exercise firewall consent mechanics without touching the host firewall."""
import json
from subprocess import CompletedProcess
from threading import Event

import pytest

from couchchd.ui import wifi_firewall as fw


def _reply(stdout='', code=0, stderr=''):
    return CompletedProcess([], code, stdout, stderr)


@pytest.mark.parametrize(('prefix', 'interfaces', 'valid'), [(24, 'wlan0', True), (0, 'wlan0', False),
                                                            (24, 'wlan0 eth0', False), (24, '', False)])
def test_discovery_requires_exact_link_and_zone(monkeypatch, prefix, interfaces, valid):
    monkeypatch.setattr(fw.shutil, 'which', lambda name: '/bin/' + name)
    links = [{'ifname': 'wlan0', 'addr_info': [{'family': 'inet', 'local': '192.168.9.23', 'prefixlen': prefix}]}]
    responses = {('/bin/ip', '-j', '-4', 'addr', 'show', 'up'): _reply(json.dumps(links)),
                 ('/bin/firewall-cmd', '--get-zone-of-interface=wlan0'): _reply('home\n'),
                 ('/bin/firewall-cmd', '--zone=home', '--list-interfaces'): _reply(interfaces)}
    monkeypatch.setattr(fw, '_run', lambda *args: responses[args])
    assert fw.HomeFirewall.discover('192.168.9.99:45678') is None
    result = fw.HomeFirewall.discover('192.168.9.23:45678')
    assert (result is not None) == valid
    if valid:
        assert str(result.network) == '192.168.9.0/24' and result.zone == 'home'


@pytest.mark.parametrize('mode', ['both', 'existing', 'race', 'denied'])
def test_only_owns_successfully_added_runtime_rules(monkeypatch, mode):
    firewall = fw.HomeFirewall('firewall-cmd', 'home', '192.168.9.0/24', 45678)
    calls = []

    def run(*args, **kwargs):
        assert args[:2] == ('firewall-cmd', '--zone=home')
        calls.append(args[2:])
        if args[2].startswith('--query-'):
            return _reply('yes') if mode == 'existing' and 'udp' in args[2] else _reply('no', 1)
        if args[2].startswith('--add-'):
            assert args[3:] == ('--timeout=600',)
            if mode == 'race' and 'udp' in args[2]:
                return _reply('success', stderr='Warning: ALREADY_ENABLED')
            if mode == 'denied' and 'tcp' in args[2]:
                return _reply(code=1)
        return _reply('success')

    monkeypatch.setattr(fw, '_run', run)
    assert firewall.allow(Event()) == (mode != 'denied')
    firewall.close()
    udp, tcp = (f'rule family="ipv4" source address="192.168.9.0/24" port port="{port}" protocol="{proto}" accept'
                for port, proto in ((42846, 'udp'), (45678, 'tcp')))
    owned = {'both': [tcp, udp], 'existing': [tcp], 'race': [tcp], 'denied': [udp]}[mode]
    assert [args[0] for args in calls if args[0].startswith('--remove')] == ['--remove-rich-rule=' + r for r in owned]
    assert ('--query-rich-rule=' + udp,) in calls and ('--query-rich-rule=' + tcp,) in calls
    if mode == 'existing':
        assert not any(args[0] == '--add-rich-rule=' + udp for args in calls)
