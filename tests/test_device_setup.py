"""Run all install transports against isolated storage and local release fixtures."""
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Linux device installer')


def executable(path, body):
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(0o755)


@pytest.fixture
def target(tmp_path):
    tools = tmp_path / 'tools'
    tools.mkdir()
    executable(tools / 'uname', 'case $1 in -s) echo Linux;; -m) echo x86_64;; esac')
    executable(tools / 'od', 'echo 2')  # ELF class byte of a 64-bit target shell
    executable(tools / 'ssh', '[ "$1" != -n ] || shift; shift; exec sh -c "$1"')
    executable(tools / 'curl', '''
        dest=/dev/stdout
        while [ "$#" -gt 1 ]; do
            if [ "$1" = -o ]; then dest=$2; shift; fi
            shift
        done
        case $1 in https://github.com/vip3rousmango/couchCHD/releases/download/v*/?*) ;; *) exit 22;; esac
        cat "artifacts/x86_64/${1##*/}" > "$dest"
    ''')
    if not shutil.which('sha256sum'):
        executable(tools / 'sha256sum', 'exec shasum -a 256 "$@"')
    data = tmp_path / 'installed builds'
    env = dict(os.environ, PATH=f'{tools}:{os.environ["PATH"]}', XDG_DATA_HOME=str(data), DISPLAY=':test')
    out = tmp_path / 'artifacts/x86_64'
    out.mkdir(parents=True)
    image = out / 'CouchCHD-x86_64.AppImage'
    executable(image, 'case "$*" in --appimage-extract-and-run) echo GUI;; "--appimage-extract-and-run --version") echo version;; "--appimage-extract-and-run --register-frontend desktop "*) echo "Menu: $4";; *) exit 6;; esac')
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (out / 'SHA256SUMS-x86_64').write_text(f'{digest}  {image.name}\n')
    return tmp_path, env, data / 'couchchd', image


@pytest.mark.parametrize('transport', ['github', 'push', 'pull'])
def test_install_launch_and_reject_damaged_update(target, transport):
    tmp, env, base, image = target
    cmd = {
        'github': ['sh', SCRIPTS / 'install-device.sh'],
        'push': [sys.executable, SCRIPTS / 'send-device.py', '--builds', 'artifacts', 'device'],
        'pull': ['sh', SCRIPTS / 'install-device.sh', 'host', 'artifacts'],
    }[transport]
    def install():
        return subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True)
    for _ in range(2):
        result = install()
        assert result.returncode == 0 and 'GUI' in result.stdout, result.stderr
    assert len(list((base / 'builds').iterdir())) == 1
    launcher = base / 'run-couchchd'
    original = launcher.read_bytes()
    image.write_bytes(b'truncated update')
    result = install()
    assert result.returncode != 0 and 'SHA-256 verification' in result.stderr
    assert launcher.read_bytes() == original
    assert subprocess.check_output([launcher], env=env, text=True).strip() == 'GUI'


@pytest.mark.parametrize('graphical', [True, False])
def test_menu_failure_does_not_hide_installed_gui(target, graphical):
    tmp, env, _, image = target
    executable(image, 'case "$2" in --version) echo version;; --register-frontend) echo "menu rejected" >&2; exit 1;; "") echo GUI;; esac')
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (image.parent / 'SHA256SUMS-x86_64').write_text(f'{digest}  {image.name}\n')
    env.pop('WAYLAND_DISPLAY', None)
    if not graphical:
        env.pop('DISPLAY', None)
    result = subprocess.run(['sh', SCRIPTS / 'install-device.sh'], cwd=tmp, env=env, capture_output=True, text=True)
    assert 'menu rejected' in result.stderr
    assert ('GUI' in result.stdout) == graphical
    assert (result.returncode == 0) == graphical


def test_exec_mask_stops_arm_setup_before_download(target):
    tmp, env, base, _ = target
    tools = tmp / 'tools'
    executable(tools / 'uname', 'case $1 in -s) echo Linux;; -m) echo aarch64;; esac')
    # A permission-synthesizing filesystem can accept chmod but return no x bits.
    executable(tools / 'chmod', 'exec /bin/chmod 600 "$2"')
    executable(tools / 'curl', 'echo unexpected-download >&2; exit 22')
    result = subprocess.run(['sh', SCRIPTS / 'install-device.sh'], cwd=tmp, env=env, capture_output=True, text=True)
    assert result.returncode and 'execute permission' in result.stderr
    assert str(base) in result.stderr and 'unexpected-download' not in result.stderr
    assert not (base / 'run-couchchd').exists() and not list((base / 'builds').iterdir())


@pytest.fixture
def recalbox_target(target):
    tmp, env, _, image = target
    tmp = tmp.resolve()
    root, media, mounts = tmp / 'recalbox', tmp / 'media', tmp / 'mountinfo'
    system = root / 'share/system'
    system.mkdir(parents=True)
    media.mkdir()
    # Substitute only kernel/device paths in a test copy. The root installer
    # must not allow an environment override to authorize arbitrary volumes.
    script = (SCRIPTS / 'install-device.sh').read_text()
    for name, path, replacement in [('recalbox', '/recalbox', root), ('media', '/media', media),
                                    ('mountinfo', '/proc/self/mountinfo', mounts)]:
        script = script.replace(f'{name}={path}\n', f'{name}={shlex.quote(str(replacement))}\n')
    installer = tmp / 'installer'
    installer.write_text(script)
    mounts.write_text('1 0 8:1 / / rw - ext4 /dev/root rw\n'
                      '2 1 8:2 / /boot rw - ext4 /dev/boot rw\n')

    def mount(path, *, device='8:3', fs='ext4', source='/dev/sda1', flags='rw', subtree='/'):
        escaped = str(path).replace(' ', r'\040')
        with mounts.open('a') as file:
            file.write(f'10 1 {device} {subtree} {escaped} {flags} - {fs} {source} rw\n')

    executable(tmp / 'tools/df', '''
        case $2 in */nearly-full) available=786431;; *) available=786432;; esac
        echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'
        echo "/dev/sda1 2000000 1000000 $available 50% $2"
    ''')
    executable(tmp / 'tools/stat', 'echo "stat: invalid option" >&2; exit 1')
    executable(tmp / 'tools/chmod', '''
        case $2 in "$SYSTEM"/couchchd/builds/.setup.*/run)
            [ -e allow-default ] || exec /bin/chmod 600 "$2";;
            */masked/couchchd/builds/.setup.*/run) exec /bin/chmod 600 "$2";;
        esac
        exec /bin/chmod "$@"
    ''')
    executable(image, '''
        case $2 in
            --version) echo version;;
            --register-frontend) [ "$3" = recalbox ] && echo "$4" >> registration;;
            *) exit 1;;
        esac
    ''')
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (image.parent / 'SHA256SUMS-x86_64').write_text(f'{digest}  {image.name}\n')

    def install(*args, stdin=None):
        return subprocess.run(['sh', installer, *args], env=dict(env, SYSTEM=str(system)),
                              cwd=tmp, stdin=stdin, capture_output=True, text=True, timeout=15)

    return tmp, system, media, mount, install


@pytest.mark.parametrize('disk', ['media', 'share', 'share_int'])
def test_recalbox_keeps_first_selected_executable_volume(recalbox_target, disk):
    tmp, system, media, mount, install = recalbox_target
    volume = {'media': media / 'Game Disk', 'share': system.parent,
              'share_int': system.parent.with_name('share_int')}[disk]
    volume.mkdir(exist_ok=True)
    mount(volume, source='/dev/loop0')
    mount(system, flags='rw,noexec')
    launcher = volume / 'couchchd/run-couchchd'
    result = install()
    assert result.returncode == 0, result.stderr
    assert launcher.is_file() and not (system / 'couchchd/run-couchchd').exists()
    # After reboot the default can run again, and another disk sorts first.
    (tmp / 'allow-default').touch()
    (media / 'AAA').mkdir()
    mount(media / 'AAA')
    result = install()
    assert result.returncode == 0, result.stderr
    assert (tmp / 'registration').read_text().splitlines() == [str(launcher)] * 2
    pin = install('--channel').stdout.strip()
    result = install('--apply-update', 'v999.0.0', pin, launcher)
    assert result.returncode == 0, result.stderr
    assert 'export COUCHCHD_RELEASE_TAG=v999.0.0' in launcher.read_text()
    # A disconnected install disk isn't an invitation to install elsewhere.
    (tmp / 'mountinfo').write_text('')
    result = install()
    assert result.returncode and 'missing or unsafe' in result.stderr
    assert 'Downloading' not in result.stderr
    assert not (media / 'AAA/couchchd').exists() and not (system / 'couchchd/run-couchchd').exists()


def test_recalbox_skips_unsafe_unmounted_and_unusable_volumes_before_download(recalbox_target):
    tmp, system, media, mount, install = recalbox_target
    # These are ordinary directories on the host; mount topology comes only
    # from the isolated fixture. Leave an unmounted internal SHARE candidate.
    system.parent.with_name('share_int').mkdir()
    for name, facts in [
        ('root-device', {'device': '8:1'}), ('boot-device', {'device': '8:2'}),
        ('tmp', {'fs': 'tmpfs'}), ('net', {'fs': 'nfs4', 'source': 'server:/share'}),
        ('ram', {'source': '/dev/zram0'}), ('subtree', {'subtree': '/var/lib'}),
        ('noexec', {'flags': 'rw,noexec'}), ('readonly', {'flags': 'ro'}),
        ('group-writable', {}), ('world-writable', {}), ('masked', {}), ('nearly-full', {}),
        ('preexisting', {}), ('redirect', {}), ('nested', {}), ('foreign-owner', {}),
    ]:
        path = media / name
        path.mkdir()
        mount(path, **facts)
    (media / 'group-writable').chmod(0o775)
    (media / 'world-writable').chmod(0o757)
    existing = media / 'preexisting/couchchd'
    existing.mkdir()
    (existing / 'run-couchchd').write_text('not ours')
    (media / 'redirect/couchchd').symlink_to(system)
    mount(media / 'nested/couchchd/builds', fs='tmpfs')
    (media / 'alias').symlink_to(tmp)
    mount(media / 'alias')
    # Never chown a real device or require root privileges in this test.
    executable(tmp / 'tools/ls', '''
        case "$2" in */foreign-owner) echo "drwxr-xr-x 1 999999 999999 0 Jan 1 1970 $2";;
            *) exec /bin/ls "$@";; esac
    ''')
    result = install()
    assert result.returncode and 'No safe executable persistent data volume' in result.stderr
    assert 'Downloading' not in result.stderr and not (tmp / 'registration').exists()
    assert (existing / 'run-couchchd').read_text() == 'not ours'
    assert not (media / 'masked/couchchd').exists()
    assert 'need at least 768 MiB' in result.stderr and not (media / 'nearly-full/couchchd').exists()
    assert not (system.parent.with_name('share_int') / 'couchchd').exists()
    # Failure of one candidate's actual exec probe must not hide a later disk.
    good = media / 'zz-good'
    good.mkdir()
    mount(good)
    result = install()
    assert result.returncode == 0, result.stderr
    assert (tmp / 'registration').read_text().strip() == str(good / 'couchchd/run-couchchd')


def test_recalbox_refuses_to_move_existing_default(recalbox_target):
    tmp, system, media, mount, install = recalbox_target
    (tmp / 'allow-default').touch()
    result = install()
    assert result.returncode == 0, result.stderr
    launcher = system / 'couchchd/run-couchchd'
    previous = launcher.read_bytes()
    (tmp / 'allow-default').unlink()
    alternate = media / 'disk'
    alternate.mkdir()
    mount(alternate)
    result = install()
    assert result.returncode and 'refusing to move' in result.stderr
    assert launcher.read_bytes() == previous and not (alternate / 'couchchd').exists()


@pytest.fixture
def card_target(recalbox_target, target):
    tmp, system, _, mount, install = recalbox_target
    _, env, _, _ = target
    runtime = tmp / 'run'
    runtime.mkdir(mode=0o700)
    mount(runtime, device='0:2', source='tmpfs', fs='tmpfs')
    mount(system.parent, source='/dev/mmcblk0p3', fs='exfat', flags='rw,fmask=0133,dmask=0022')
    for name, value in [('meminfo', 'MemAvailable: 500000 kB\n'), ('filesystems', '\tsquashfs\n'),
                        ('status', 'CapEff:\t0000000000200000\n')]:
        (tmp / name).write_text(value)
    script = tmp / 'installer'
    body = script.read_text()
    for old, new in [("temp_roots='/run /tmp'", 'temp_roots=' + shlex.quote(str(runtime))),
                     ('meminfo=/proc/meminfo', 'meminfo=' + shlex.quote(str(tmp / 'meminfo'))),
                     ('filesystems=/proc/filesystems', 'filesystems=' + shlex.quote(str(tmp / 'filesystems'))),
                     ('cap_status=/proc/self/status', 'cap_status=' + shlex.quote(str(tmp / 'status'))),
                     ('loop_control=/dev/loop-control', 'loop_control=/dev/null')]:
        body = body.replace(old + '\n', new + '\n')
    if sys.platform != 'linux':  # Only Linux has the procfs open-file interface.
        body = body.replace('source=/proc/$$/fd/3\n', 'source=$image\n')
    script.write_text(body)
    tools = tmp / 'tools'
    executable(tools / 'uname', 'case $1 in -s) echo Linux;; -m) echo aarch64;; esac')
    executable(tools / 'curl', '''
        dest=/dev/stdout
        while [ "$#" -gt 1 ]; do
            if [ "$1" = -o ]; then dest=$2; shift; fi
            shift
        done
        echo "$1" >> requests
        cat "artifacts/aarch64/${1##*/}" > "$dest"
    ''')
    # The native ARM workflow mounts the actual SquashFS. Here model only
    # the privileged kernel boundary, not the checksum, pointer or runner.
    executable(tools / 'mount', '''
        set -eu
        [ "$#" = 6 ] && [ "$1 $2 $3 $4" = '-t squashfs -o loop,ro,nodev,nosuid' ] || exit 1
        [ "$(ls -nd "${6%/*}" | cut -c1-10)" = drwx------ ] || exit 1
        [ ! -e reject-mount ] || exit 1
        cat "$5" > "$6/launch-recalbox.sh"
        cp "$6/launch-recalbox.sh" "$6/couchchd"
        chmod 755 "$6/launch-recalbox.sh" "$6/couchchd"
        echo "99 10 7:0 / $6 ro,nodev,nosuid - squashfs /dev/loop0 ro" >> mountinfo
        echo mounted >> mounts-log
    ''')
    executable(tools / 'umount', '''
        set -eu
        rm "$1/launch-recalbox.sh" "$1/couchchd"
        awk -v p="$1" '$5 != p' mountinfo > mountinfo.new
        mv mountinfo.new mountinfo
    ''')
    image = tmp / 'artifacts/aarch64/CouchCHD-Recalbox-aarch64.squashfs'
    image.parent.mkdir()
    def publish(label):
        executable(image, f'''
            case "$1" in
              --version) echo {label};;
              --register-frontend) [ "$2" = recalbox ] && printf '%s\\n' "$3" >> registration;;
              --probe) printf '%s\\n' {label} "$COUCHCHD_HOME" "$COUCHCHD_LAUNCHER" "$2";;
              --fail) exit 37;;
            esac
        ''')
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        (image.parent / 'SHA256SUMS-aarch64').write_text(f'{digest}  {image.name}\n')
        return digest
    publish('first')
    base = system / 'couchchd'
    def launch(*args):
        return subprocess.run(['/bin/sh', base / 'run-couchchd', *args], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=15)
    return tmp, base, install, launch, publish


@pytest.mark.parametrize('transport', ['github', 'pull', 'receive'])
def test_masked_card_registers_and_relaunches_offline(card_target, transport):
    tmp, base, install, launch, _ = card_target
    assert install('--probe-asset').stdout.split() == ['aarch64', 'CouchCHD-Recalbox-aarch64.squashfs']
    if transport == 'receive':
        image = next((tmp / 'artifacts/aarch64').glob('*.squashfs'))
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        with image.open('rb') as payload:
            result = install('--receive', 'aarch64', digest, stdin=payload)
    else:
        result = install(*(['host', 'artifacts'] if transport == 'pull' else []))
    assert result.returncode == 0, result.stderr
    launcher = base / 'run-couchchd'
    assert not os.access(launcher, os.X_OK)
    assert (tmp / 'registration').read_text().splitlines() == [str(launcher)]
    assert not (base.parent / '.couchchd-install-volume').exists()
    assert not list((base / 'builds').rglob('couchchd'))
    (tmp / 'artifacts').rename(tmp / 'offline')
    result = launch('--probe', 'path with space')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['first', str(base), str(launcher), 'path with space']
    assert launch('--fail').returncode == 37
    assert not list((tmp / 'run').iterdir())


@pytest.mark.parametrize('unsafe', ['checksum', 'image-link', 'build-link', 'noexec', 'nested',
                                    'writable-system', 'writable-file', 'no-privilege', 'low-memory', 'runtime-noexec'])
def test_card_rechecks_every_launch_before_mount(card_target, unsafe):
    tmp, base, install, launch, _ = card_target
    result = install()
    assert result.returncode == 0, result.stderr
    before = (tmp / 'mounts-log').read_bytes()
    image = next((base / 'builds').glob('*/bundle.squashfs'))
    mounts = tmp / 'mountinfo'
    if unsafe == 'checksum':
        image.write_bytes(b'changed')
    elif unsafe in ('image-link', 'build-link'):
        path = image if unsafe == 'image-link' else image.parent
        real = path.rename(path.with_name('real'))
        path.symlink_to(real)
    elif unsafe == 'noexec':
        mounts.write_text(mounts.read_text().replace('rw,fmask=', 'rw,noexec,fmask='))
    elif unsafe == 'nested':
        with mounts.open('a') as f:
            f.write(f'77 10 3:1 / {base}/builds rw - ext4 /dev/other rw\n')
    elif unsafe == 'writable-system':
        base.parent.chmod(0o777)
    elif unsafe == 'writable-file':
        image.chmod(0o666)
    elif unsafe == 'no-privilege':
        (tmp / 'status').write_text('CapEff:\t0000000000000000\n')
    elif unsafe == 'runtime-noexec':
        mounts.write_text(mounts.read_text().replace('rw - tmpfs', 'rw,noexec - tmpfs'))
    else:
        (tmp / 'meminfo').write_text('MemAvailable: 4096 kB\n')
    assert launch('--version').returncode != 0
    assert (tmp / 'mounts-log').read_bytes() == before
    assert not list((tmp / 'run').iterdir())


@pytest.mark.parametrize('problem', ['no-space', 'noexec', 'no-squashfs'])
def test_card_preflight_rejects_unsupported_setup_before_download(card_target, problem):
    tmp, base, install, _, _ = card_target
    if problem == 'no-space':
        executable(tmp / 'tools/df', 'echo header; echo "/dev/mmcblk0p3 2000000 1990000 10000 99% SHARE"')
    elif problem == 'noexec':
        info = tmp / 'mountinfo'
        info.write_text(info.read_text().replace('rw,fmask=', 'rw,noexec,fmask='))
    else:
        (tmp / 'filesystems').write_text('\text4\n')
    result = install()
    assert result.returncode != 0 and not (tmp / 'requests').exists()
    assert not (base / 'run-couchchd').exists() and not list((tmp / 'run').iterdir())
