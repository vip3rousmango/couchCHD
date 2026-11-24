#!/usr/bin/env bash
# Build on the target Linux architecture, not on a Deck/Pi with a nearly full ROM drive.
# Prerequisites: GCC/g++, GNU make, git, python3, pkg-config, binutils, libc static archives.
# JOBS limits memory usage. TMPDIR must be a build volume, not the user's ROM directory.
set -euo pipefail
export LC_ALL=C
root=$(cd "$(dirname "$0")/.." && pwd)
arch=$(uname -m)
case $(uname -s):$arch in Linux:x86_64) config=release64;; Linux:aarch64) config=release;; *) echo 'Requires native x86_64 or aarch64 Linux.' >&2; exit 2;; esac
jobs=${JOBS:-2}
work=$(mktemp -d "${TMPDIR:-/tmp}/couchchd-build.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
cd "$work"
mame_tag=mame0270
mame_rev=ef032a31e50e861d4f593fa4d5e9a8c4cc06b0e1
sdl_tag=release-2.30.9
sdl_rev=c98c4fbff6d8f3016a3ce6685bf8f43433c3efcc
checkout() {
    git -c advice.detachedHead=false clone --depth=1 --branch "$2" "https://github.com/$1.git" "$4"
    [[ $(git -C "$4" rev-parse HEAD) == "$3" ]] || { echo "Unexpected $1 tag revision" >&2; exit 1; }
}
checkout mamedev/mame "$mame_tag" "$mame_rev" mame
checkout libsdl-org/SDL "$sdl_tag" "$sdl_rev" sdl

# MAME's Unix OSD support library references SDL clipboard functions even in the
# command-line tools. Use a separate minimal static SDL, never the host GUI's SDL.
(cd sdl && ./configure --prefix="$work/sdl-static" --disable-shared --enable-static \
    --disable-audio --disable-joystick --disable-haptic --disable-hidapi --disable-sensor \
    --disable-video-x11 --disable-video-wayland --disable-video-kmsdrm --disable-video-rpi \
    --disable-video-vulkan --disable-video-opengl --disable-video-opengles --disable-video-offscreen \
    --disable-render --disable-ime --disable-ibus --disable-fcitx --disable-dbus --disable-libudev &&
    make -j"$jobs" && make install)
cd mame
project=build/projects/sdl/mame/gmake-linux
# SDL's static-only sdl2-config emits an absolute archive path. MAME treats
# those paths as early linker flags; pkg-config emits -lSDL2 in the library group.
export PKG_CONFIG_PATH="$work/sdl-static/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
# Invoke GENie's chdman target, not the whole MAME release or the emulator.
make -j"$jobs" TOOLS=1 EMULATOR=0 PRECOMPILE=0 NO_OPENGL=1 NO_X11=1 NO_USE_XINPUT=1 \
    NO_USE_MIDI=1 NO_USE_PORTAUDIO=1 NO_USE_PULSEAUDIO=1 USE_QTDEBUG=0 NOWERROR=1 \
    ARCHOPTS="-I$work/sdl-static/include" LDOPTS=-static \
    build/generated/version.cpp "$project/Makefile"
make -j"$jobs" -C "$project" config="$config" chdman
strip chdman
case $arch in
    aarch64) machine='AArch64';;
    x86_64) machine='Advanced Micro Devices X86-64';;
esac
readelf -hW chdman > "$work/elf-header"
grep -Eq "Machine:[[:space:]]+$machine$" "$work/elf-header" || { echo "Wrong ELF machine; expected $arch" >&2; exit 1; }
# Reject dynamic loaders and dynamic dependencies (including static PIE ambiguity).
readelf -lW chdman > "$work/segments"
readelf -dW chdman > "$work/dynamic"
if grep -q INTERP "$work/segments" || grep -q '(NEEDED)' "$work/dynamic"; then
    echo 'Refusing non-static chdman' >&2; exit 1
fi
# Exercise the exact program and hunk size used for PSP/PS2 before installing it.
dd if=/dev/zero of="$work/probe.iso" bs=2048 count=128 status=none
./chdman createdvd -i "$work/probe.iso" -o "$work/probe.chd" -hs 2048
./chdman verify -i "$work/probe.chd"

out="$root/bin/$arch"
mkdir -p "$out/licenses"
cp COPYING "$out/licenses/MAME-COPYING"
cp -rT docs/legal "$out/licenses/mame-legal"
cp "$work/sdl/LICENSE.txt" "$out/licenses/static-SDL.txt"
if command -v dpkg-query >/dev/null; then
    for pkg in libc6 libgcc-s1 libstdc++6; do
        cp -L "/usr/share/doc/$pkg/copyright" "$out/licenses/$pkg-copyright"
        dpkg-query -W -f='${Package}: https://snapshot.debian.org/package/${source:Package}/${source:Version}/\n' "$pkg"
    done > "$out/licenses/system-sources.txt"
    cp -rL /usr/share/common-licenses "$out/licenses/"
fi
# Preserve bundled codec licenses, including notices outside docs/legal.
find 3rdparty -type f \( -iname 'copying*' -o -iname 'license*' -o -iname 'copyright*' \) -exec cp --parents -t "$out/licenses" -- {} +
install -m 755 chdman "$out/chdman.new"
mv -f "$out/chdman.new" "$out/chdman"
{
    echo "architecture=$arch"
    echo "mame=https://github.com/mamedev/mame/tree/$mame_rev ($mame_tag)"
    echo "static_sdl=https://github.com/libsdl-org/SDL/tree/$sdl_rev ($sdl_tag)"
    cc --version; ld --version; getconf GNU_LIBC_VERSION
} > "$out/PROVENANCE.txt"
(cd "$out" && sha256sum chdman > SHA256SUMS)
# A release job supplies a fresh output directory; these are unmodified complete sources.
if [[ -n ${CHDMAN_SOURCES_DIR:-} ]]; then
    mkdir -p "$CHDMAN_SOURCES_DIR"
    git archive --format=tar.gz --prefix="$mame_tag/" -o "$CHDMAN_SOURCES_DIR/$mame_tag.tar.gz" HEAD
    git -C "$work/sdl" archive --format=tar.gz --prefix="SDL-$sdl_tag/" -o "$CHDMAN_SOURCES_DIR/SDL-$sdl_tag.tar.gz" HEAD
fi
printf 'Installed verified static chdman: %s\n' "$out/chdman"
