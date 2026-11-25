#!/bin/sh
# No arguments: install the pinned GitHub release. SSH transports are optional.
set -eu
umask 077
fail() { printf 'CouchCHD: %s\n' "$*" >&2; exit 1; }
fetch() {
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' "$@"
}
platform() {
    [ "$(uname -s)" = Linux ] || fail 'A Linux target is required.'
    arch=$(uname -m)
    case $arch in
        x86_64) asset=CouchCHD-x86_64.AppImage ;;
        aarch64) asset=CouchCHD-Recalbox-aarch64.tar.gz ;; # historical asset name, portable Linux bundle
        *) fail "Unsupported architecture: $arch (64-bit userspace required)." ;;
    esac
    # A 64-bit kernel can run a 32-bit userspace. EI_CLASS of the running shell is authoritative.
    elf=$(od -An -tu1 -j4 -N1 /proc/$$/exe | tr -d '[:space:]')
    [ "$elf" = 2 ] || fail 'A 64-bit Linux userspace is required.'
    base=${XDG_DATA_HOME:-$HOME/.local/share}/couchchd
    if [ -d /recalbox/share/system ]; then
        frontend=recalbox; base=/recalbox/share/system/couchchd
    elif [ -d /userdata/system ] && [ -d /userdata/roms ] && { [ -f /boot/boot/batocera.board ] || command -v batocera-version >/dev/null 2>&1; }; then
        frontend=batocera; base=/userdata/system/couchchd
    elif [ -d /opt/retropie/supplementary/emulationstation ]; then
        [ "$(id -u)" != 0 ] || fail 'Install as your RetroPie frontend user, not root.'
        frontend=retropie
    elif [ -d /storage/roms ] && grep -Eiq '^(ID|NAME|OS_NAME)="?rocknix"?$' /etc/os-release; then
        frontend=rocknix; base=/storage/.local/share/couchchd
    else
        frontend=desktop
    fi
}
install() {
    platform
    transport=ssh
    case ${1:-} in
        '') [ "$#" = 0 ] || fail 'Empty SSH host.'
            transport=github
            release=https://github.com/vip3rousmango/couchCHD/releases/download/v0.1.0-preview.2
            command -v curl >/dev/null 2>&1 || fail 'The GitHub installer requires curl with HTTPS support.'
            printf 'Installing %s from %s\n' "$asset" "$release" >&2
            manifest=$(fetch "$release/SHA256SUMS-$arch") || fail "Cannot download release manifest. Check your connection and whether $release is available."
            ;;
        --probe) printf '%s\n' "$arch"; return ;;
        --receive) [ "$#" = 3 ] && [ "$arch" = "$2" ] || fail 'Build/target architecture mismatch.'; digest=$3; transport=stream ;;
        -*|*[!a-zA-Z0-9.@_-]*) fail 'Usage: install-device.sh [user@build-host [remote-build-directory]]' ;;
        *) host=$1; dir=${2:-couchCHD/dist/release}; [ "$#" -le 2 ] || fail 'Unexpected arguments.'
            case $dir in ''|-*|*[!a-zA-Z0-9./_-]*) fail 'Unsupported build directory name.' ;; esac
            prefix=$dir/$arch
            manifest=$(ssh -n "$host" "cat '$prefix/SHA256SUMS-$arch'") || fail 'Cannot read the build manifest over SSH.' ;;
    esac
    if [ "$transport" != stream ]; then
        digest=$(printf '%s\n' "$manifest" | awk -v f="$asset" 'NF == 2 && $2 == f {print $1}')
    fi
    case $digest in ''|*[!0-9a-f]*) fail 'Invalid or missing SHA-256 digest.' ;; esac
    [ "${#digest}" = 64 ] || fail 'Invalid SHA-256 length.'
    mkdir -p "$base/builds"
    tmp=$(mktemp -d "$base/builds/.setup.XXXXXX")
    lock=$base/builds/.install-lock
    owns_lock=no
    cleanup() { rm -rf "$tmp"; if [ "$owns_lock" = yes ]; then rmdir "$lock"; fi; }
    trap cleanup EXIT
    trap 'exit 1' HUP INT TERM
    case $transport in
        github) fetch -o "$tmp/download" "$release/$asset" || fail 'Release download failed; previous installation unchanged.' ;;
        ssh) ssh -n "$host" "cat '$prefix/$asset'" > "$tmp/download" ;;
        stream) cat > "$tmp/download" ;;
    esac
    (cd "$tmp" && printf '%s  download\n' "$digest" | sha256sum -c -) >&2 || fail 'Download failed SHA-256 verification; previous installation unchanged.'
    mkdir "$lock" || fail "Another installer holds $lock. If it crashed, remove the empty lock directory after checking no setup is running."
    owns_lock=yes
    launcher=$base/run-couchchd
    if [ -L "$launcher" ] || { [ -e "$launcher" ] && { [ ! -f "$launcher" ] || ! grep -qx '# CouchCHD managed launcher' "$launcher"; }; }; then
        fail "Will not replace unmanaged $launcher"
    fi
    final=$base/builds/$digest
    if [ ! -d "$final" ]; then
        mkdir "$tmp/install"
        if [ "$arch" = x86_64 ]; then
            mv "$tmp/download" "$tmp/install/$asset"; chmod 755 "$tmp/install/$asset"
            printf '%s\n' '#!/bin/sh' 'exec "$(dirname "$0")/CouchCHD-x86_64.AppImage" --appimage-extract-and-run "$@"' > "$tmp/install/run"
        else
            # Only extract the tarball authenticated by the trusted build host's manifest.
            tar -xzf "$tmp/download" -C "$tmp/install"
            [ -x "$tmp/install/couchchd/launch-recalbox.sh" ] || fail 'Incomplete Recalbox bundle.'
            printf '%s\n' '#!/bin/sh' 'export COUCHCHD_HOME="${COUCHCHD_HOME:-$(CDPATH="" cd "$(dirname "$0")/../.." && pwd)}"' 'exec "$(dirname "$0")/couchchd/launch-recalbox.sh" "$@"' > "$tmp/install/run"
        fi
        chmod 755 "$tmp/install/run"
        COUCHCHD_HOME="$base" "$tmp/install/run" --version >&2 || fail 'Bundle cannot run here. Check glibc (2.36+), mount permissions and architecture.'
        mv "$tmp/install" "$final"
    else
        "$final/run" --version >&2 || fail "Previously installed build at $final cannot start."
    fi
    {
        printf '%s\n' '#!/bin/sh' '# CouchCHD managed launcher'
        if [ "$arch:$frontend" = x86_64:desktop ]; then
            cat <<'GRAPHICS'
# This Qt Quick UI does not need a GPU. Avoid requiring a working EGL/GLX
# driver in the AppImage just to open the library window on SteamOS.
if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && [ -z "${QT_QUICK_BACKEND:-}${QSG_RHI_BACKEND:-}" ]; then
    export QT_QUICK_BACKEND=software
    if [ -n "${DISPLAY:-}" ] && [ -z "${QT_QPA_PLATFORM:-}" ]; then
        export QT_QPA_PLATFORM=xcb
    fi
fi
GRAPHICS
        fi
        printf '%s\n' "exec \"\$(dirname \"\$0\")/builds/${final##*/}/run\" \"\$@\""
    } > "$tmp/entry"
    chmod 755 "$tmp/entry"; mv -f "$tmp/entry" "$launcher"
    cleanup; trap - EXIT HUP INT TERM
    printf 'Installed. Relaunch with: %s\n' "$launcher" >&2
    "$launcher" --register-frontend "$frontend" "$launcher"
    if [ "$frontend" = desktop ] && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        exec "$launcher"
    fi
    printf 'Setup complete. Start CouchCHD from your frontend or run the printed launcher in a graphical session.\n' >&2
}
install "$@"
