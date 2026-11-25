"""Optional, runtime-only firewalld access for the sender's directly attached LAN."""
import ipaddress
import json
import os
import re
import shutil
import subprocess
import time


_TIMEOUT = 600
_PRIVATE_LANS = tuple(map(ipaddress.IPv4Network, ('192.168.0.0/16', '172.16.0.0/12', '10.0.0.0/8')))


def _run(*args, timeout=4):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, 'LC_ALL': 'C'}, check=False)


class HomeFirewall:
    def __init__(self, command, zone, network, port):
        self.command = command
        self.zone = zone
        self.network = network
        self.rules = tuple(f'rule family="ipv4" source address="{network}" port port="{p}" protocol="{proto}" accept'
                           for p, proto in ((42846, 'udp'), (port, 'tcp')))
        self._owned = []

    @classmethod
    def discover(cls, address):
        """Never guess a subnet, zone, interface, or executable when discovery fails."""
        cmd, ip = shutil.which('firewall-cmd'), shutil.which('ip')
        if not cmd or not ip:
            return None
        try:
            host, raw_port = address.split(':')
            addr, port = ipaddress.IPv4Address(host), int(raw_port)
            if addr.is_loopback or addr.is_multicast or addr.is_unspecified or not 0 < port <= 65535:
                return None
            links = _run(ip, '-j', '-4', 'addr', 'show', 'up')
            if links.returncode:
                return None
            matches = [(link['ifname'], ipaddress.IPv4Interface(f"{host}/{info['prefixlen']}").network)
                       for link in json.loads(links.stdout) for info in link.get('addr_info', [])
                       if info.get('family') == 'inet' and info.get('local') == host]
            if len(matches) != 1:
                return None
            iface, network = matches[0]
            if network.prefixlen == 32 or not any(network.subnet_of(lan) for lan in _PRIVATE_LANS):
                return None
            response = _run(cmd, '--get-zone-of-interface=' + iface)
            zone = response.stdout.strip()
            if response.returncode or not re.fullmatch(r'[A-Za-z0-9_-]+', zone):
                return None
            bindings = _run(cmd, '--zone=' + zone, '--list-interfaces')
            if bindings.returncode or bindings.stdout.split() != [iface]:
                return None
            return cls(cmd, zone, network, port)
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
            return None

    def allow(self, stop):
        try:
            for rule in self.rules:
                if stop.is_set():
                    raise ValueError('Session stopped')
                query = _run(self.command, '--zone=' + self.zone, '--query-rich-rule=' + rule)
                if query.returncode == 0:
                    continue  # The rule predates us; never adopt it.
                if query.returncode != 1 or query.stdout.strip() != 'no':
                    raise ValueError('Cannot query firewall')
                expires = time.monotonic() + _TIMEOUT
                added = _run(self.command, '--zone=' + self.zone, '--add-rich-rule=' + rule,
                             f'--timeout={_TIMEOUT}', timeout=20)
                if added.returncode:
                    raise ValueError('Firewall denied change')
                if 'ALREADY_ENABLED' not in added.stderr + added.stdout:
                    self._owned.append((rule, expires))
            return True
        except (ValueError, OSError, subprocess.SubprocessError):
            self.close()
            return False

    def close(self):
        while self._owned:
            rule, expires = self._owned.pop()
            if time.monotonic() >= expires:
                continue
            try:
                _run(self.command, '--zone=' + self.zone, '--remove-rich-rule=' + rule)
            except (OSError, subprocess.SubprocessError):
                pass  # Runtime timeout also covers crashes and revoked authorization.
