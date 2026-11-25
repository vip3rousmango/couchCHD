"""Exercise the bundled updater through the same POSIX shell interface the UI uses."""
import hashlib
import os
import re
import subprocess
import tarfile

import pytest

from test_device_setup import SCRIPTS, card_target, executable, recalbox_target, target

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Linux device installer')
INSTALLER = SCRIPTS / 'install-device.sh'
RELEASES = 'https://github.com/vip3rousmango/couchCHD/releases/download'


def invoke(tmp, env, *args):
    return subprocess.run(['/bin/sh', INSTALLER, *args], env=env, cwd=tmp,
                          capture_output=True, text=True, timeout=15)


def success(result):
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def publish(image, arch):
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (image.parent / f'SHA256SUMS-{arch}').write_text(f'{digest}  {image.name}\n')


def test_channel_is_only_a_tag_without_platform_or_network(tmp_path):
    for name in ['uname', 'curl']:
        executable(tmp_path / name, 'exit 1')
    pin = success(invoke(tmp_path, {'PATH': str(tmp_path)}, '--channel'))
    # Deliberately independent of the release currently being cut.
    assert re.fullmatch(r'v\d+\.\d+\.\d+(?:-preview\.\d+)?', pin)


@pytest.mark.parametrize('arch', ['x86_64', 'aarch64'])
def test_apply_switches_only_the_given_launcher_for_next_start(target, arch):
    tmp, env, base, image = target
    tools = tmp / 'tools'
    executable(tools / 'uname', f'case $1 in -s) echo Linux;; -m) echo {arch};; esac')
    executable(tools / 'curl', f'''
        [ "$1" = -q ] || exit 1
        for url do :; done
        case "$url" in {RELEASES}/v*/?*) ;; *) exit 22;; esac
        echo "$url" >> requests
        dest=/dev/stdout
        while [ "$#" -gt 1 ]; do
            if [ "$1" = -o ]; then dest=$2; shift; fi
            shift
        done
        cat "artifacts/{arch}/${{url##*/}}" > "$dest"
    ''')
    if arch == 'aarch64':
        image = tmp / 'artifacts' / arch / 'CouchCHD-Recalbox-aarch64.tar.gz'
        image.parent.mkdir()
        app = tmp / 'couchchd'
        app.mkdir()
        (app / 'launch-recalbox.sh').write_bytes((SCRIPTS.parent / 'packaging/launch-recalbox.sh').read_bytes())
        (app / 'launch-recalbox.sh').chmod(0o755)

    def upload(label):
        # No registration or GUI start allowed for the replacement bundle.
        body = f'''case "${{1:-}}" in --appimage-extract-and-run) shift;; esac
        case "${{1:-}}" in
          --version) echo 'version';;
          probe) printf '%s %s\\n' '{label}' "$COUCHCHD_RELEASE_TAG";;
          *) echo invoked >> "$CALLS";;
        esac'''
        if arch == 'x86_64':
            executable(image, body)
        else:
            executable(app / 'couchchd', body)
            with tarfile.open(image, 'w:gz') as archive:
                archive.add(app, arcname='couchchd')
        publish(image, arch)

    upload('first')
    env = dict(env, CALLS=str(tmp / 'calls'))
    old = success(invoke(tmp, env, '--channel'))
    success(invoke(tmp, env))
    launcher = (base / 'run-couchchd').resolve()
    assert subprocess.check_output([launcher, 'probe'], env=env, text=True).strip() == f'first {old}'
    calls_before = (tmp / 'calls').read_bytes()
    upload('second')
    new = 'v999.0.0-preview.12'
    child_env = dict(env, XDG_DATA_HOME=str(base / 'data'))  # as set by launch-recalbox.sh
    (tmp / 'requests').write_text('')
    success(invoke(tmp, child_env, '--apply-update', new, old, launcher))
    assert (tmp / 'requests').read_text().splitlines() == [
        f'{RELEASES}/{new}/SHA256SUMS-{arch}', f'{RELEASES}/{new}/{image.name}']
    assert (tmp / 'calls').read_bytes() == calls_before
    assert not (base / 'data/couchchd/run-couchchd').exists()
    assert subprocess.check_output([launcher, 'probe'], env=env, text=True).strip() == f'second {new}'

    # Numeric preview ordering and the subsequent stable promotion.
    assert invoke(tmp, env, '--apply-update', 'v999.0.0-preview.9', new, launcher).returncode != 0
    success(invoke(tmp, env, '--apply-update', 'v999.0.0', new, launcher))
    assert invoke(tmp, env, '--apply-update', 'v999.0.0-preview.99', 'v999.0.0', launcher).returncode != 0


@pytest.mark.parametrize('problem', [
    'hash', 'preflight', 'switch', 'stale', 'downgrade', 'equal', 'invalid', 'private',
    'unmanaged', 'missing', 'locked', 'symlink', 'alias', 'builds-link', 'cached-link',
])
def test_refused_update_never_replaces_old_launcher(target, problem):
    tmp, env, base, image = target
    old = success(invoke(tmp, env, '--channel'))
    success(invoke(tmp, env, *(['host', 'artifacts'] if problem == 'private' else [])))
    launcher = (base / 'run-couchchd').resolve()
    new = 'v999.0.0'
    old_builds = set((base / 'builds').iterdir())
    if problem == 'hash':
        image.write_bytes(b'truncated update')
    elif problem == 'preflight':
        executable(image, 'exit 1')
        publish(image, 'x86_64')
    elif problem == 'switch':
        executable(image, 'echo version')
        publish(image, 'x86_64')
        executable(tmp / 'tools/mv', '''
            for dest do :; done
            case "$dest" in */run-couchchd) exit 1;; esac
            exec /bin/mv "$@"
        ''')
    elif problem == 'stale':
        old = 'v0.0.1'
    elif problem == 'downgrade':
        new = 'v0.0.0'
    elif problem == 'equal':
        new = old
    elif problem == 'invalid':
        new += '\n--help'
    elif problem == 'unmanaged':
        launcher.write_text(launcher.read_text().replace('# CouchCHD managed launcher', '# custom'))
    elif problem == 'missing':
        launcher = tmp / 'absent/couchchd/run-couchchd'
    elif problem == 'locked':
        (base / 'builds/.install-lock').mkdir()
    elif problem == 'symlink':
        launcher.rename(base / 'real')
        launcher.symlink_to(base / 'real')
    elif problem == 'alias':
        (tmp / 'alias').symlink_to(base.parent, target_is_directory=True)
        launcher = tmp / 'alias/couchchd/run-couchchd'
    elif problem == 'builds-link':
        (base / 'builds').rename(base / 'real')
        (base / 'builds').symlink_to(base / 'real', target_is_directory=True)
    elif problem == 'cached-link':
        exe = next((base / 'builds').glob('*/run'))
        exe.rename(exe.with_name('real'))
        exe.symlink_to(exe.with_name('real'))
    before = launcher.read_bytes() if launcher.exists() else None
    result = invoke(tmp, env, '--apply-update', new, old, launcher)
    assert result.returncode != 0, result.stdout
    if before is None:
        assert not launcher.parent.exists()
    else:
        assert launcher.read_bytes() == before
        assert subprocess.check_output([launcher], env=env, text=True).strip() == 'GUI'
    if problem == 'switch':
        assert 'Could not switch the launcher' in result.stderr
        assert set((base / 'builds').iterdir()) == old_builds


def test_card_update_switches_public_pointer_without_reregistering(card_target):
    tmp, base, install, launch, publish_card = card_target
    old = success(install('--channel'))
    success(install())
    launcher = base / 'run-couchchd'
    initial_registration = (tmp / 'registration').read_bytes()
    publish_card('second')
    new = 'v999.0.0'
    success(install('--apply-update', new, old, launcher))
    assert success(launch('--version')) == 'second'
    assert (tmp / 'registration').read_bytes() == initial_registration
    assert f'export COUCHCHD_RELEASE_TAG={new}\n' in launcher.read_text()
    assert not list((tmp / 'run').iterdir())


@pytest.mark.parametrize('failure', ['corrupt-old', 'bad-download', 'mount', 'switch'])
def test_card_update_failure_is_atomic(card_target, failure):
    tmp, base, install, launch, publish_card = card_target
    old = success(install('--channel'))
    success(install())
    launcher = base / 'run-couchchd'
    before = launcher.read_bytes()
    builds = set((base / 'builds').iterdir())
    publish_card('second')
    if failure == 'corrupt-old':
        next((base / 'builds').glob('*/bundle.squashfs')).write_bytes(b'changed')
    elif failure == 'bad-download':
        next((tmp / 'artifacts/aarch64').glob('*.squashfs')).write_bytes(b'changed')
    elif failure == 'mount':
        (tmp / 'reject-mount').touch()
    else:
        executable(tmp / 'tools/mv', '''
            for dest do :; done
            case "$dest" in */run-couchchd) exit 1;; esac
            exec /bin/mv "$@"
        ''')
    requests = (tmp / 'requests').read_bytes()
    result = install('--apply-update', 'v999.0.0', old, launcher)
    assert result.returncode != 0
    assert launcher.read_bytes() == before and set((base / 'builds').iterdir()) == builds
    assert not list((tmp / 'run').iterdir())
    if failure == 'corrupt-old':
        assert 'SHA-256 verification' in result.stderr and (tmp / 'requests').read_bytes() == requests
    else:
        (tmp / 'reject-mount').unlink(missing_ok=True)
        assert success(launch('--version')) == 'first'
