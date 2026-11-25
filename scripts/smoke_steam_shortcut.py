"""Release gate: launch the installed binary using the shortcut Steam would read.

Requires an isolated Steam profile, Xvfb/xdotool, and the pinned vdf reader.
This exercises the stored command, not the Steam client or gamescope.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys

import vdf


def main() -> None:
    launcher = Path(sys.argv[1])
    profile = Path.home() / '.local/share/Steam/userdata/123/config'
    with (profile / 'shortcuts.vdf').open('rb') as stream:
        entries = vdf.binary_load(stream)['shortcuts']
    (entry,) = (fields for fields in entries.values() if fields['AppName'] == 'CouchCHD')
    assert entry['Exe'] == f'"{launcher}"' and entry['StartDir'] == f'"{launcher.parent}"', entry
    assert not entry['LaunchOptions'], entry
    env = dict(os.environ, SteamAppId=str(entry['appid'] & 0xffffffff),
               XDG_SESSION_TYPE='wayland', WAYLAND_DISPLAY='unavailable', QT_XCB_GL_INTEGRATION='none')
    proc = subprocess.Popen([entry['Exe'][1:-1]], cwd=entry['StartDir'][1:-1], env=env,
                            start_new_session=True)
    try:
        subprocess.run(['xdotool', 'search', '--sync', '--onlyvisible', '--name', '^CouchCHD'],
                       timeout=60, check=True)
    finally:
        log = launcher.with_name('steam-launch.log')
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        print(log.read_text())


if __name__ == '__main__':
    main()
