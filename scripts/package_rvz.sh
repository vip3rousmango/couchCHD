#!/usr/bin/env bash
# Build Bookworm's real DolphinTool from authenticated corresponding source.
# The distro binary hardcodes /usr/share/games/dolphin-emu/sys, sizes workers
# by *host* CPUs and pulls in FFmpeg. Use upstream's relocatable CLI target.
# Native Linux, proot, curl, tar, coreutils; no mounts, Docker-in-Docker or
# changes to the outer builder's apt configuration. Arguments: frozen app, assets.
set -euo pipefail
export LC_ALL=C
arch=$(uname -m)
case $(uname -s):$arch in
  Linux:x86_64) rev=865e49bb5782df1fa4b4c4d8d474e3583b67bfda; layer=b1dd3d113aab1041fd80cc61d9db7ffbc058f756bf46fc149a0d9aa8b85935f4;;
  Linux:aarch64) rev=657d5664bd8e7d6833fa48225b8110774cc17666; layer=7f1aa0b5c351d0542b2066749ed47608e5873bcdd1fd14f4df6f0a05cac02f8e;;
  *) echo 'RVZ requires native x86_64 or aarch64 Linux.' >&2; exit 2;;
esac
app=$(realpath "$1"); out=$(realpath "$2"); script=$(realpath "$0")
command -v proot >/dev/null || { echo 'Install proot in the build environment.' >&2; exit 2; }
work=$(mktemp -d "${TMPDIR:-/tmp}/couchchd-rvz.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
fs=$work/root
mkdir -p "$fs"
curl --proto '=https' --proto-redir '=https' -fsSL \
  "https://raw.githubusercontent.com/debuerreotype/docker-debian-artifacts/$rev/bookworm/slim/oci/blobs/rootfs.tar.gz" -o "$work/rootfs.tar.gz"
printf '%s  %s\n' "$layer" "$work/rootfs.tar.gz" | sha256sum --check
tar -xzf "$work/rootfs.tar.gz" -C "$fs"
mkdir -p "$fs/etc/ssl/certs" "$fs/work/sources" "$fs/stage/lib" "$fs/notices"
cp -L /etc/ssl/certs/ca-certificates.crt "$fs/etc/ssl/certs/"
cp -L /etc/resolv.conf "$fs/etc/resolv.conf"
cp "$script" "$fs/work/sources/package_rvz.sh"
# Same dated archive as both base layers. Old Valid-Until dates are expected;
# signature and package digest checks stay enabled. Include security sources.
rm -f "$fs/etc/apt/sources.list.d/debian.sources"
for entry in 'debian bookworm' 'debian bookworm-updates' 'debian-security bookworm-security'; do
  read -r archive suite <<<"$entry"
  for kind in deb deb-src; do
    echo "$kind [check-valid-until=no signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://snapshot.debian.org/archive/$archive/20261123T000000Z/ $suite main"
  done
done > "$fs/etc/apt/sources.list"
# Keep compiler/libs independent of the app's Python/Qt build environment.
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C LC_ALL=C PROOT_NO_SECCOMP=1 \
  proot -0 -r "$fs" -b /dev -b /proc -b /sys -w /work /bin/bash -seu -o pipefail -- "${JOBS:-2}" <<'BUILD'
export DEBIAN_FRONTEND=noninteractive SOURCE_DATE_EPOCH=1795392000
version=5.0-17995-1
apt-get -o APT::Sandbox::User=root update
apt-get -o APT::Sandbox::User=root install -y --no-install-recommends build-essential python3 ninja-build
apt-get -o APT::Sandbox::User=root build-dep -y --no-install-recommends "dolphin-emu=$version"
cd /work/sources
apt-get -o APT::Sandbox::User=root source --download-only --only-source "dolphin-emu=$version"
cd /work
dpkg-source -x sources/dolphin-emu_${version}.dsc source
cd source
# Keep the codec and verification implementation unchanged. Stock DolphinTool's
# CLI supplies no progress callback or worker limit. Apply only these UI/worker
# adjustments to the pinned source and include the recipe in the source offer.
python3 - <<'PY'
from pathlib import Path

def replace(path, old, new):
    p = Path('Source/Core') / path
    text = p.read_text()
    if text.count(old) != 1:
        raise SystemExit(f'Unexpected pinned source: {p}')
    p.write_text(text.replace(old, new))

for p in ('DiscIO/MultithreadedCompressor.h', 'DiscIO/VolumeWii.cpp'):
    text = (Path('Source/Core') / p).read_text()
    first_include = next(x for x in text.splitlines() if x.startswith('#include '))
    replace(p, first_include, '#include <algorithm>\n#include <cstdlib>\n' + first_include)
    replace(p, 'std::thread::hardware_concurrency()',
            '(std::getenv("COUCHCHD_DOLPHIN_THREADS") ? '
            'static_cast<unsigned int>(std::clamp(std::atoi(std::getenv("COUCHCHD_DOLPHIN_THREADS")), 1, 4)) : 1u)')
conv = 'DolphinTool/ConvertCommand.cpp'
replace(conv, 'const auto NOOP_STATUS_CALLBACK = [](const std::string& text, float percent) { return true; };',
        '''int last_percent = -1;
  const auto NOOP_STATUS_CALLBACK = [&last_percent](const std::string&, float fraction) {
    int pct = static_cast<int>(fraction * 100);
    if (pct != last_percent) std::cerr << "RVZ progress " << (last_percent = pct) << std::endl;
    return true;
  };''')
verify = 'DolphinTool/VerifyCommand.cpp'
replace(verify, '  verifier.Start();', '  int last_percent = -1;\n  verifier.Start();')
replace(verify, '    verifier.Process();', '''    verifier.Process();
    int pct = static_cast<int>(verifier.GetBytesProcessed() * 100 / verifier.GetTotalBytes());
    if (pct != last_percent) std::cerr << "RVZ progress " << (last_percent = pct) << std::endl;''')
PY
cmake -S . -B /work/build -GNinja -DCMAKE_BUILD_TYPE=Release \
  -DLINUX_LOCAL_DEV=ON -DENABLE_QT=OFF -DENABLE_TESTS=OFF -DENABLE_NOGUI=OFF \
  -DUSE_SHARED_ENET=ON -DUSE_DISCORD_PRESENCE=OFF -DUSE_MGBA=OFF \
  -DENCODE_FRAMEDUMPS=OFF -DENABLE_ANALYTICS=OFF -DENABLE_LLVM=OFF \
  -DDOLPHIN_WC_BRANCH=master -DDOLPHIN_WC_DESCRIBE=5.0-17995-g8bad821019 \
  -DDOLPHIN_WC_REVISION=8bad821019721b9b72701b495da95656ace5fea5 \
  -DCMAKE_EXE_LINKER_FLAGS=-Wl,-Map=/work/link.map
cmake --build /work/build --target dolphin-tool --parallel "$1"
install -s -m 755 /work/build/Binaries/dolphin-tool /stage/dolphin-tool.bin
cp -aL Data/Sys /stage/Sys
cp -L debian/copyright /notices/dolphin-emu-copyright
cp -rL LICENSES /notices/dolphin-licenses
cp -L COPYING /notices/dolphin-COPYING
find Externals -type f \( -iname 'copying*' -o -iname 'license*' -o -iname 'copyright*' \) -exec cp --parents -t /notices -- {} +
cp -rL /usr/share/common-licenses /notices/
# Collect actual transitive shared libraries and every system archive participating
# in the final link, with the Debian source version and copyright for each.
python3 - <<'PY'
from pathlib import Path
import re, shutil, subprocess

exe = '/stage/dolphin-tool.bin'
text = subprocess.check_output(['ldd', exe], text=True)
if 'not found' in text:
    raise SystemExit(text)
interp = re.search(r'Requesting program interpreter: (.*?)\]', subprocess.check_output(['readelf', '-lW', exe], text=True))[1]
shutil.copy2(interp, '/stage/loader')
paths = {Path(interp).resolve()}
for name, path in re.findall(r'^\s*(\S+) => (/\S+) \(', text, re.M):
    shutil.copy2(path, Path('/stage/lib') / name)
    paths.add(Path(path).resolve())
for lib in re.findall(r'(/[^\s()]+\.a)(?:\(|\s|$)', Path('/work/link.map').read_text(), re.M):
    p = Path(lib).resolve()
    if not p.is_relative_to('/work'):
        paths.add(p)
# A library named in the private loader's --list result may never fall back to host.
resolved = subprocess.check_output(['/stage/loader', '--inhibit-cache', '--library-path', '/stage/lib', '--list', exe], text=True)
for line in resolved.splitlines():
    if ' => ' in line and not any(s in line for s in (' => /stage/lib/', ' => /stage/loader ')):
        raise SystemExit(f'Missing private dependency: {line}')
packages = set()
for p in sorted(paths):
    candidates = (str(p), str(p).removeprefix('/usr')) if str(p).startswith('/usr/') else (str(p),)
    for candidate in candidates:
        query = subprocess.run(['dpkg-query', '-S', candidate], text=True, capture_output=True)
        if not query.returncode:
            packages.add(query.stdout.split(': ', 1)[0].split(', ', 1)[0])
            break
    else:
        raise SystemExit(f'No Debian owner for {p}')
source_versions = set()
with open('/notices/PROVENANCE.txt', 'w') as out:
    print('DolphinTool 5.0-17995-1 (source build; 128 KiB Zstandard, no scrubbing)', file=out)
    print('Builder/dependencies: signed Debian bookworm snapshot 20261123T000000Z. Recipe: package_rvz.sh in corresponding sources.', file=out)
    for pkg in sorted(packages):
        line = subprocess.check_output(['dpkg-query','-W','-f=${binary:Package} ${Version} ${source:Package}=${source:Version}',pkg],text=True)
        print(line, file=out)
        source_versions.add(line.split()[-1])
        name = pkg.split(':')[0]
        shutil.copyfile(Path('/usr/share/doc') / name / 'copyright', '/notices/' + name + '-copyright')
subprocess.run(['apt-get', '-o', 'APT::Sandbox::User=root', 'source', '--download-only', '--only-source', *sorted(source_versions)], cwd='/work/sources', check=True)
PY
BUILD
# Keep Sys beside the executable *and* loader: /proc/self/exe can denote the
# loader when invoked explicitly. Do not expose private glibc to the parent app.
cat > "$fs/stage/dolphin-tool" <<'SH'
#!/bin/sh
set -eu
here=${0%/*}
unset LD_PRELOAD LD_LIBRARY_PATH
exec "$here/loader" --inhibit-cache --library-path "$here/lib" "$here/dolphin-tool.bin" "$@"
SH
chmod 755 "$fs/stage/dolphin-tool"
(cd "$fs/stage" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) > "$fs/notices/SHA256SUMS"
mkdir -p "$app/_internal/bin/$arch" "$app/notices"
mv "$fs/stage" "$app/_internal/bin/$arch/rvz"
mv "$fs/notices" "$app/notices/rvz"
(cd "$fs/work/sources" && sha256sum -- * > SHA256SUMS)
tar --sort=name --mtime=@1795392000 --owner=0 --group=0 --numeric-owner \
    -C "$fs/work" -cf "$out/dolphin-corresponding-sources-$arch.tar" sources
