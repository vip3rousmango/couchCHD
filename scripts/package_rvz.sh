#!/usr/bin/env bash
# Build Bookworm's real DolphinTool from authenticated corresponding source.
# The distro binary hardcodes /usr/share/games/dolphin-emu/sys, sizes workers
# by *host* CPUs and pulls in FFmpeg. Use upstream's relocatable CLI target.
# Run only inside the disposable native Bookworm build container, as with
# install_build_deps.sh. Arguments: frozen app directory and release assets.
set -euo pipefail
export LC_ALL=C
arch=$(uname -m)
. /etc/os-release
[[ $ID == debian && $VERSION_ID == 12 && $EUID == 0 ]] || { echo 'Use the native Bookworm release builder.' >&2; exit 2; }
app=$(realpath "$1"); out=$(realpath "$2"); script=$(realpath "$0")
work=$(mktemp -d "${TMPDIR:-/tmp}/couchchd-rvz.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/sources" "$work/stage/lib" "$work/notices"
cp "$script" "$work/sources/package_rvz.sh"
# Use signed source indexes for the pinned Dolphin version and for the exact
# versions of libraries installed in the native builder, including security fixes.
for entry in 'debian bookworm' 'debian bookworm-updates' 'debian-security bookworm-security'; do
  read -r archive suite <<<"$entry"
  for kind in deb deb-src; do
    echo "$kind [check-valid-until=no signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://snapshot.debian.org/archive/$archive/20261123T000000Z/ $suite main"
  done
  echo "deb-src [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/$archive $suite main"
done > "$work/sources.list"
printf 'Dir::Etc::sourcelist "%s";\n' "$work/sources.list" > "$work/apt.conf"
export APT_CONFIG="$work/apt.conf" DEBIAN_FRONTEND=noninteractive SOURCE_DATE_EPOCH=1795392000
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH LD_LIBRARY_PATH VIRTUAL_ENV
version=5.0-17995-1
apt-get -o APT::Sandbox::User=root update
apt-get -o APT::Sandbox::User=root install -y --no-install-recommends build-essential python3 ninja-build
apt-get -o APT::Sandbox::User=root build-dep -y --no-install-recommends "dolphin-emu=$version"
cd "$work/sources"
apt-get -o APT::Sandbox::User=root source --download-only --only-source "dolphin-emu=$version"
cd "$work"
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
cmake -S . -B "$work/build" -GNinja -DCMAKE_BUILD_TYPE=Release \
  -DLINUX_LOCAL_DEV=ON -DENABLE_QT=OFF -DENABLE_TESTS=OFF -DENABLE_NOGUI=OFF \
  -DUSE_SHARED_ENET=ON -DUSE_DISCORD_PRESENCE=OFF -DUSE_MGBA=OFF \
  -DENCODE_FRAMEDUMPS=OFF -DENABLE_ANALYTICS=OFF -DENABLE_LLVM=OFF \
  -DDOLPHIN_WC_BRANCH=master -DDOLPHIN_WC_DESCRIBE=5.0-17995-g8bad821019 \
  -DDOLPHIN_WC_REVISION=8bad821019721b9b72701b495da95656ace5fea5 \
  "-DCMAKE_EXE_LINKER_FLAGS=-Wl,-Map=$work/link.map"
cmake --build "$work/build" --target dolphin-tool --parallel "${JOBS:-2}"
install -s -m 755 "$work/build/Binaries/dolphin-tool" "$work/stage/dolphin-tool.bin"
cp -aL Data/Sys "$work/stage/Sys"
cp -L debian/copyright "$work/notices/dolphin-emu-copyright"
cp -rL LICENSES "$work/notices/dolphin-licenses"
cp -L COPYING "$work/notices/dolphin-COPYING"
find Externals -type f \( -iname 'copying*' -o -iname 'license*' -o -iname 'copyright*' \) -exec cp --parents -t "$work/notices" -- {} +
cp -rL /usr/share/common-licenses "$work/notices/"
# Collect actual transitive shared libraries and every system archive participating
# in the final link, with the Debian source version and copyright for each.
cd "$work"
python3 - <<'PY'
from pathlib import Path
import re, shutil, subprocess

work = Path.cwd()
stage = work / 'stage'
exe = stage / 'dolphin-tool.bin'
text = subprocess.check_output(['ldd', exe], text=True)
if 'not found' in text:
    raise SystemExit(text)
interp = re.search(r'Requesting program interpreter: (.*?)\]', subprocess.check_output(['readelf', '-lW', exe], text=True))[1]
shutil.copy2(interp, stage / 'loader')
paths = {Path(interp).resolve()}
for name, path in re.findall(r'^\s*(\S+) => (/\S+) \(', text, re.M):
    shutil.copy2(path, stage / 'lib' / name)
    paths.add(Path(path).resolve())
# GNU ld's LOAD records give the complete archive name, relative to Ninja's
# build directory or absolute for system libraries. Member names elsewhere
# in the map are not paths (e.g. Source/Core/AudioCommon/libaudiocommon.a).
for lib in re.findall(r'^LOAD ([^\n]+\.a)$', Path('link.map').read_text(), re.M):
    p = (work / 'build' / lib).resolve(strict=True)
    if not p.is_relative_to(work):
        paths.add(p)
# A library named in the private loader's --list result may never fall back to host.
resolved = subprocess.check_output([stage / 'loader', '--inhibit-cache', '--library-path', stage / 'lib', '--list', exe], text=True)
for line in resolved.splitlines():
    if ' => ' in line and not any(s in line for s in (f' => {stage}/lib/', f' => {stage}/loader ')):
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
with open('notices/PROVENANCE.txt', 'w') as out:
    print('DolphinTool 5.0-17995-1 (source build; 128 KiB Zstandard, no scrubbing)', file=out)
    print('Native Debian Bookworm builder; package versions below. Recipe: package_rvz.sh in corresponding sources.', file=out)
    for pkg in sorted(packages):
        line = subprocess.check_output(['dpkg-query','-W','-f=${binary:Package} ${Version} ${source:Package}=${source:Version}',pkg],text=True)
        print(line, file=out)
        source_versions.add(line.split()[-1])
        name = pkg.split(':')[0]
        shutil.copyfile(Path('/usr/share/doc') / name / 'copyright', Path('notices') / (name + '-copyright'))
subprocess.run(['apt-get', '-o', 'APT::Sandbox::User=root', 'source', '--download-only', '--only-source', *sorted(source_versions)], cwd='sources', check=True)
PY
# Keep Sys beside the executable *and* loader: /proc/self/exe can denote the
# loader when invoked explicitly. Do not expose private glibc to the parent app.
cat > "$work/stage/dolphin-tool" <<'SH'
#!/bin/sh
set -eu
here=${0%/*}
unset LD_PRELOAD LD_LIBRARY_PATH
exec "$here/loader" --inhibit-cache --library-path "$here/lib" "$here/dolphin-tool.bin" "$@"
SH
chmod 755 "$work/stage/dolphin-tool"
test -x "$work/stage/loader"
test -x "$work/stage/dolphin-tool.bin"
(cd "$work/stage" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) > "$work/notices/SHA256SUMS"
mkdir -p "$app/_internal/bin/$arch" "$app/notices"
mv "$work/stage" "$app/_internal/bin/$arch/rvz"
mv "$work/notices" "$app/notices/rvz"
(cd "$work/sources" && sha256sum -- * > SHA256SUMS)
tar --sort=name --mtime=@1795392000 --owner=0 --group=0 --numeric-owner \
    -C "$work" -cf "$out/dolphin-corresponding-sources-$arch.tar" sources
