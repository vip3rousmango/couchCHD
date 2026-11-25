#!/usr/bin/env bash
# Use in a clean, native Debian 12 build environment after install_build_deps.sh.
# Builds the x86_64 AppImage or ARM64 tarball INCLUDING chdman; no cross-compilation.
# RELEASE_DIR is output-only; use a fresh directory. Full corresponding sources for
# the release are written by the x86_64 job. See the release workflow for the CI gate.
set -euo pipefail
export LC_ALL=C.UTF-8 PYGAME_HIDE_SUPPORT_PROMPT=1
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
arch=$(uname -m)
case $(uname -s):$arch in Linux:x86_64|Linux:aarch64) ;; *) echo 'Unsupported build host.' >&2; exit 2;; esac
out=$(realpath -m "${RELEASE_DIR:-dist/release/$arch}")
mkdir -p "$out"
[[ -z $(ls -A "$out") ]] || { echo "Release directory must be empty: $out" >&2; exit 2; }
work=$(mktemp -d "${TMPDIR:-/tmp}/couchchd-freeze.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
if [[ $arch == x86_64 ]]; then export CHDMAN_SOURCES_DIR="$out"; fi
bash scripts/fetch_chdman.sh
(cd "bin/$arch" && sha256sum --check SHA256SUMS)
export COUCHCHD_CHDMAN="$root/bin/$arch/chdman" PATH="$root/bin/$arch:$PATH"
python3.11 -m venv "$work/env"
source "$work/env/bin/activate"
python -m pip install --disable-pip-version-check --only-binary=:all: --require-hashes \
    --report "$work/wheels.json" -r packaging/requirements-linux.txt
python -m pip install --no-build-isolation --no-deps '.[gui]'
python -m pip check
if [[ ${COUCHCHD_RUN_TESTS:-0} == 1 ]]; then
    # Keep test-only hooks and modules out of the frozen app.
    python -m pip install --target "$work/test-deps" 'pytest>=8,<10'
    PYTHONPATH="$work/test-deps" QT_QPA_PLATFORM=offscreen python -m pytest
    # Exercise the real converter and controller-to-frame path on both native
    # Linux architectures, not just a scripted progress model.
    SteamDeck=1 SDL_AUDIODRIVER=dummy PYTHONPATH="$work/test-deps" \
        python tests/test_queue_ui.py --native-latency --input dpad
fi
qt=$(python -c 'import pathlib, PySide6; print(pathlib.Path(PySide6.__file__).parent / "Qt")')
# PySide6 Essentials 6.7.3 ships the EGLFS/KMS plugin but omits one of its
# private shared libraries. Use the matching upstream Qt build, not distro Qt.
case $arch in
    aarch64) repo=linux_arm64; kit=gcc_arm64; platform=Linux-Debian_11_6-GCC-Linux-Debian_11_6-AARCH64
        qtbase_digest=850f7d2b73c958ce80ef47a81c18f64d03d3be606637278d14acb8959e6dfe8c ;;
    x86_64) repo=linux_x64; kit=gcc_64; platform=Linux-RHEL_8_8-GCC-Linux-RHEL_8_8-X86_64
        qtbase_digest=f37a29c373ad5c2f4b31657fdefe415015c7298f957c49572227950774385f83 ;;
esac
qtbase_url="https://download.qt.io/online/qtsdkrepository/$repo/desktop/qt6_673/qt.qt6.673.linux_$kit/6.7.3-0-202409200836qtbase-$platform.7z"
curl --proto '=https' --proto-redir '=https' -fsSL "$qtbase_url" -o "$work/qtbase.7z"
printf '%s  %s\n' "$qtbase_digest" "$work/qtbase.7z" | sha256sum --check
gbm=libQt6EglFsKmsGbmSupport.so
bsdtar -xOf "$work/qtbase.7z" "6.7.3/$kit/lib/$gbm.6.7.3" > "$qt/lib/$gbm.6"
sdl=$(python -c 'import sysconfig; print(sysconfig.get_path("platlib") + "/pygame_ce.libs")')
python -m PyInstaller --clean --noconfirm --onedir --name couchchd --noupx \
    --specpath "$work" --workpath "$work/build" --distpath "$work/frozen" \
    --collect-data couchchd.ui --copy-metadata couchchd \
    --add-data "$root/scripts/install-device.sh:installer" \
    --hidden-import PySide6.QtQuick --hidden-import PySide6.QtQuickControls2 --hidden-import PySide6.QtSvg \
    --hidden-import pygame._sdl2.controller --hidden-import pygame.mixer \
    --add-binary "$sdl:pygame_ce.libs" \
    --add-binary "$qt/plugins/egldeviceintegrations:PySide6/Qt/plugins/egldeviceintegrations" \
    --add-binary "$qt/lib/$gbm.6:PySide6/Qt/lib" \
    --add-binary "$COUCHCHD_CHDMAN:bin/$arch" "$root/packaging/launcher.py"
app="$work/frozen/couchchd"
cmp scripts/install-device.sh "$app/_internal/installer/install-device.sh"
# Load the delivered KMS plugin and its complete link closure without a display.
# Xvfb alone only exercises xcb; this still does not test an actual KMS device.
LD_LIBRARY_PATH="$app/_internal:$app/_internal/PySide6/Qt/lib" /usr/bin/python3 -I -c \
    'import ctypes, sys; ctypes.CDLL(sys.argv[1])' "$app/_internal/PySide6/Qt/plugins/egldeviceintegrations/libqeglfs-kms-integration.so"
install -m 644 LICENSE packaging/INSTALL.txt "$app/"
mkdir -p "$app/fonts"
install -m 644 /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf "$app/fonts/"
python packaging/collect_notices.py "$app/notices"
printf '\nQt EGLFS/GBM binary: %s (SHA256 %s); source: Qt 6.7.3 archive above.\n' \
    "$qtbase_url" "$qtbase_digest" >> "$app/notices/SOURCES.txt"
cp -r "bin/$arch/licenses" "$app/notices/converter"
cp "bin/$arch/"{PROVENANCE.txt,SHA256SUMS} "$app/notices/converter/"
bash scripts/collect_upstream.sh "$work/sources" "$app/notices" "$out"
cp "$work/wheels.json" packaging/requirements-linux.txt "$app/notices/"
{
    echo "CouchCHD $(python -c 'from importlib.metadata import version; print(version("couchchd"))')"
    echo "source_commit=$(git rev-parse HEAD)"
    echo "target=$arch glibc=$(getconf GNU_LIBC_VERSION)"
    echo "workflow=${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-local}/actions/runs/${GITHUB_RUN_ID:-local}"
    python -VV
} > "$app/BUILD.txt"
cp "$app/BUILD.txt" "$out/BUILD-$arch.txt"
cp "$app/notices/SOURCES.txt" "$out/SOURCES-$arch.txt"
cp "bin/$arch/SHA256SUMS" "$out/chdman-$arch.sha256"
# Include notices with the separately downloadable native converter.
tar -C bin -czf "$out/chdman-$arch.tar.gz" "$arch"
if [[ $arch == x86_64 ]]; then
    # Publish the installer from this exact tag with the checksummed binaries.
    install -m 755 scripts/install-device.sh "$out/install-device.sh"
    git archive --format=tar.gz --prefix=couchchd-source/ HEAD > "$out/CouchCHD-source.tar.gz"
    bash scripts/build_appimage.sh "$app" "$out/CouchCHD-x86_64.AppImage"
    launch=("$out/CouchCHD-x86_64.AppImage" --appimage-extract-and-run)
else
    install -m 755 packaging/launch-recalbox.sh "$app/"
    archive="$out/CouchCHD-Recalbox-aarch64.tar.gz"
    # PyInstaller creates symlinks; exFAT data partitions cannot represent them.
    tar --dereference --hard-dereference -C "$work/frozen" -czf "$archive" couchchd
    mkdir "$work/unpacked"
    tar -C "$work/unpacked" -xzf "$archive"
    launch=("$work/unpacked/couchchd/launch-recalbox.sh")
fi
# Prove the delivered bundle can resolve and run its own converter. No source
# checkout, Python environment or previously built converter may satisfy this.
cd "$work"
unset COUCHCHD_CHDMAN PYTHONPATH
export PATH=/usr/bin:/bin COUCHCHD_HOME="$work/smoke-home" HOME="$work/smoke-home"
mkdir -p "$HOME" roms/psx
dd if=/dev/zero of=roms/psx/track.bin bs=2352 count=300 status=none
printf 'FILE "track.bin" BINARY\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n' >roms/psx/Probe.cue
touch -d '1 minute ago' roms/psx/{track.bin,Probe.cue}
"${launch[@]}" --headless --root "$work/roms" --auto --system psx --keep-originals --preset silent
test -s roms/psx/Probe.chd
test -s roms/psx/track.bin
"$root/bin/$arch/chdman" verify -i roms/psx/Probe.chd
# Confirm that the distributed Qt app actually maps a visible root window.
# The release must still be tested with a physical controller/graphics stack.
QT_QPA_PLATFORM=xcb QT_QUICK_BACKEND=software SDL_AUDIODRIVER=dummy \
    xvfb-run -a bash -eu -c '"$@" >"$HOME/window.log" 2>&1 & pid=$!; trap '\''kill "$pid" 2>/dev/null || :; cat "$HOME/window.log"'\'' EXIT; timeout 45 xdotool search --sync --onlyvisible --name CouchCHD' \
    -- "${launch[@]}" --root "$work/roms"
(cd "$out" && sha256sum -- * > "SHA256SUMS-$arch")
