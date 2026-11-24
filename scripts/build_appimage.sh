#!/usr/bin/env bash
# Usage: build_appimage.sh FROZEN_APP_DIRECTORY OUTPUT.AppImage (native x86_64 Linux).
set -euo pipefail
[[ $(uname -sm) == 'Linux x86_64' && $# == 2 ]] || exit 2
root=$(cd "$(dirname "$0")/.." && pwd)
app=$(realpath "$1")
out=$(realpath -m "$2")
[[ -x $app/couchchd && ! -e $out ]] || exit 2
work=$(mktemp -d "${TMPDIR:-/tmp}/couchchd-appimage.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
cd "$work"
# Neither the builder nor its embedded runtime is fetched from a rolling URL.
curl --proto '=https' --proto-redir '=https' -fsSL https://github.com/AppImage/appimagetool/releases/download/1.9.0/appimagetool-x86_64.AppImage -o appimagetool
curl --proto '=https' --proto-redir '=https' -fsSL https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64 -o runtime
sha256sum --check <<'HASHES'
46fdd785094c7f6e545b61afcfb0f3d98d8eab243f644b4b17698c01d06083d1  appimagetool
2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d  runtime
HASHES
chmod +x appimagetool
install -m 644 "$root/packaging/couchchd.desktop" "$root/packaging/couchchd.svg" "$app/"
ln -s couchchd "$app/AppRun"
# Generation and smoke testing require no FUSE privilege in CI.
./appimagetool --appimage-extract >/dev/null
ARCH=x86_64 ./squashfs-root/AppRun --runtime-file "$work/runtime" "$app" "$out"
