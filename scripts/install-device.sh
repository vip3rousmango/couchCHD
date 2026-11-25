#!/bin/sh
# No arguments: install the pinned GitHub release. SSH transports are optional.
set -eu
umask 077
channel=v0.1.0-preview.5
fail() { printf 'CouchCHD: %s\n' "$*" >&2; exit 1; }
fetch() {
    curl -q --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --speed-time 60 --speed-limit 1 "$@"
}
# Validate the complete argument, not individual lines (a tag is part of a URL).
valid_tag() {
    case $1 in ''|*[!v0123456789.preview-]*) return 1 ;; esac
    [ "${#1}" -le 64 ] && printf '%s\n' "$1" |
        LC_ALL=C grep -Eq '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-preview\.(0|[1-9][0-9]*))?$'
}
newer_tag() {
    # Compare canonical decimal fields as strings: BusyBox awk integers lose
    # precision for sufficiently long tags. A final release follows all previews.
    LC_ALL=C awk -v new="$1" -v old="$2" '
        function fields(tag, parts) {
            sub(/^v/, "", tag); sub(/-preview/, "", tag); return split(tag, parts, /[.]/)
        }
        BEGIN {
            n = fields(new, a); m = fields(old, b)
            for (i = 1; i <= 4; ++i) {
                if (i > n || i > m) exit !(n < m)
                if (length(a[i]) != length(b[i])) exit !(length(a[i]) > length(b[i]))
                if ("v" a[i] != "v" b[i]) exit !(("v" a[i]) > ("v" b[i]))
            }
            exit 1
        }'
}
valid_digest() {
    case $1 in ''|*[!0-9a-f]*) return 1 ;; esac
    [ "${#1}" = 64 ]
}
regular_executable() { [ ! -L "$1" ] && [ -f "$1" ] && [ -x "$1" ]; }
check_build() {
    [ ! -L "$1" ] && [ -d "$1" ] && regular_executable "$1/run" ||
        fail "Missing or unsafe build at $1."
    if [ "$arch" = x86_64 ]; then
        regular_executable "$1/$asset" || fail 'Missing or unsafe AppImage.'
    else
        [ ! -L "$1/couchchd" ] && regular_executable "$1/couchchd/launch-recalbox.sh" &&
            regular_executable "$1/couchchd/couchchd" || fail 'Missing or unsafe ARM bundle.'
    fi
}
check_update_source() {
    regular_executable "$launcher" && grep -Fqx '# CouchCHD managed launcher' "$launcher" ||
        fail 'Updates require an existing managed launcher.'
    installed=$(sed -n 's/^export COUCHCHD_RELEASE_TAG=//p' "$launcher")
    [ -n "$installed" ] || fail 'This is not a managed public release.'
    [ "$installed" = "$expected" ] || fail 'The installed release changed. Close and reopen CouchCHD.'
    previous=$(sed -n 's|^exec "\$here/builds/\([0-9a-f]*\)/run" "\$@"$|\1|p' "$launcher")
    valid_digest "$previous" || fail 'The managed launcher has no valid build target.'
    check_build "$base/builds/$previous"
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
    # Identify the frontend independently of the install base. ARM launchers
    # may have changed XDG_DATA_HOME since initial registration.
    if [ -d /recalbox/share/system ]; then
        frontend=recalbox
    elif [ -d /userdata/system ] && [ -d /userdata/roms ] && { [ -f /boot/boot/batocera.board ] || command -v batocera-version >/dev/null 2>&1; }; then
        frontend=batocera
    elif [ -d /opt/retropie/supplementary/emulationstation ]; then
        [ "$(id -u)" != 0 ] || fail 'Install as your RetroPie frontend user, not root.'
        frontend=retropie
    elif [ -d /storage/roms ] && grep -Eiq '^(ID|NAME|OS_NAME)="?rocknix"?$' /etc/os-release; then
        frontend=rocknix
    else
        frontend=desktop
    fi
}
install() {
    platform
    transport=ssh
    update=no
    case ${1:-} in
        '') [ "$#" = 0 ] || fail 'Empty SSH host.'
            transport=github; release_tag=$channel ;;
        --apply-update)
            [ "$#" = 4 ] || fail 'Expected new tag, current tag and stable launcher path.'
            release_tag=$2; expected=$3; launcher=$4
            valid_tag "$release_tag" && valid_tag "$expected" || fail 'Invalid release tag.'
            newer_tag "$release_tag" "$expected" || fail 'The requested release is not newer.'
            case $launcher in /*/couchchd/run-couchchd|/couchchd/run-couchchd) ;;
                *) fail 'Updates require the absolute managed launcher path.' ;; esac
            base=${launcher%/*}
            [ -d "$base/builds" ] && [ "$(CDPATH='' cd -P "$base" && pwd -P)" = "$base" ] ||
                fail 'Missing or redirected installation directory.'
            transport=github; update=yes ;;
        --probe) printf '%s\n' "$arch"; return ;;
        --receive) [ "$#" = 3 ] && [ "$arch" = "$2" ] || fail 'Build/target architecture mismatch.'; digest=$3; transport=stream ;;
        -*|*[!a-zA-Z0-9.@_-]*) fail 'Usage: install-device.sh [user@build-host [remote-build-directory]]' ;;
        *) host=$1; dir=${2:-couchCHD/dist/release}; [ "$#" -le 2 ] || fail 'Unexpected arguments.'
            case $dir in ''|-*|*[!a-zA-Z0-9./_-]*) fail 'Unsupported build directory name.' ;; esac
            prefix=$dir/$arch ;;
    esac
    if [ "$update" = no ]; then
        case $frontend in
            recalbox) base=/recalbox/share/system/couchchd ;;
            batocera) base=/userdata/system/couchchd ;;
            rocknix) base=/storage/.local/share/couchchd ;;
            *) base=${XDG_DATA_HOME:-$HOME/.local/share}/couchchd ;;
        esac
        launcher=$base/run-couchchd
    fi
    [ ! -L "$base" ] && [ ! -L "$base/builds" ] || fail 'Refusing symlinked installation directory.'
    if [ "$update" = no ]; then mkdir -p "$base/builds"; fi
    tmp=; entry=; pending=; owns_lock=no
    lock=$base/builds/.install-lock
    cleanup() {
        # If the final rename failed or was interrupted, the staged launcher
        # still exists. Undo ONLY this install's promotion under the lock.
        if [ -n "$pending" ] && [ -f "$entry" ] && [ ! -e "$tmp/install" ] && [ -d "$pending" ]; then
            mv "$pending" "$tmp/install"
        fi
        if [ -n "$entry" ]; then rm -f "$entry"; fi
        if [ -n "$tmp" ]; then rm -rf "$tmp"; fi
        if [ "$owns_lock" = yes ]; then rmdir "$lock"; fi
    }
    trap cleanup 0
    trap 'exit 1' HUP INT TERM
    mkdir "$lock" || fail "Another installer holds $lock. If it crashed, remove the empty lock directory after checking no setup is running."
    owns_lock=yes
    if [ "$update" = yes ]; then
        check_update_source
    elif [ -L "$launcher" ] || { [ -e "$launcher" ] && { [ ! -f "$launcher" ] || ! grep -Fqx '# CouchCHD managed launcher' "$launcher"; }; }; then
        fail "Will not replace unmanaged $launcher"
    fi
    case $transport in
        github)
            release=https://github.com/vip3rousmango/couchCHD/releases/download/$release_tag
            command -v curl >/dev/null 2>&1 || fail 'The GitHub installer requires curl with HTTPS support.'
            printf 'CouchCHD: Downloading %s for %s.\n' "$asset" "$release_tag" >&2
            manifest=$(fetch "$release/SHA256SUMS-$arch") || fail "Cannot download release manifest. Check your connection and whether $release is available." ;;
        ssh) manifest=$(ssh -n "$host" "cat '$prefix/SHA256SUMS-$arch'") || fail 'Cannot read the build manifest over SSH.' ;;
    esac
    if [ "$transport" != stream ]; then
        digest=$(printf '%s\n' "$manifest" | awk -v f="$asset" 'NF == 2 && $2 == f {print $1}')
    fi
    valid_digest "$digest" || fail 'Invalid or missing SHA-256 digest.'
    tmp=$(mktemp -d "$base/builds/.setup.XXXXXX")
    case $transport in
        github) fetch -o "$tmp/download" "$release/$asset" || fail 'Release download failed; previous installation unchanged.' ;;
        ssh) ssh -n "$host" "cat '$prefix/$asset'" > "$tmp/download" ;;
        stream) cat > "$tmp/download" ;;
    esac
    printf 'CouchCHD: Verifying SHA-256 checksum.\n' >&2
    (cd "$tmp" && printf '%s  download\n' "$digest" | sha256sum -c -) >&2 || fail 'Download failed SHA-256 verification; previous installation unchanged.'
    printf 'CouchCHD: Checking that the bundle can run on this device.\n' >&2
    final=$base/builds/$digest
    [ ! -L "$final" ] || fail 'Refusing symlinked build directory.'
    if [ ! -e "$final" ]; then
        mkdir "$tmp/install"
        if [ "$arch" = x86_64 ]; then
            mv "$tmp/download" "$tmp/install/$asset"; chmod 755 "$tmp/install/$asset"
            printf '%s\n' '#!/bin/sh' 'exec "$(dirname "$0")/CouchCHD-x86_64.AppImage" --appimage-extract-and-run "$@"' > "$tmp/install/run"
        else
            # Extract only after verification against the selected transport's manifest.
            tar -xzf "$tmp/download" -C "$tmp/install"
            printf '%s\n' '#!/bin/sh' 'export COUCHCHD_HOME="${COUCHCHD_HOME:-$(CDPATH="" cd "$(dirname "$0")/../.." && pwd)}"' 'exec "$(dirname "$0")/couchchd/launch-recalbox.sh" "$@"' > "$tmp/install/run"
        fi
        chmod 755 "$tmp/install/run"
        check_build "$tmp/install"
        COUCHCHD_HOME="$base" "$tmp/install/run" --version >&2 || fail 'Bundle cannot run here. Check glibc (2.36+), mount permissions and architecture.'
    else
        check_build "$final"
        COUCHCHD_HOME="$base" "$final/run" --version >&2 || fail "Previously installed build at $final cannot start."
    fi
    # Stage the pointer on the launcher's own filesystem. Even if builds/ is
    # separately mounted, switching versions is one atomic rename.
    entry=$(mktemp "$base/.run-couchchd.XXXXXX")
    {
        printf '%s\n' '#!/bin/sh' '# CouchCHD managed launcher'
        if [ "$transport" = github ]; then
            printf 'export COUCHCHD_RELEASE_TAG=%s\n' "$release_tag"
        else
            printf '%s\n' 'unset COUCHCHD_RELEASE_TAG'
        fi
        cat <<'LOCATION'
here=$(CDPATH='' cd -P "$(dirname "$0")" && pwd) || exit 1
export COUCHCHD_LAUNCHER="$here/run-couchchd"
LOCATION
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
        if [ "$frontend" = desktop ]; then
            cat <<'STEAM_LOG'
if [ -n "${SteamGameId:-}${SteamAppId:-}" ]; then
    # Steam hides stderr in Gaming Mode. Replace the last launch log without
    # following an existing symlink or changing permissions on other files.
    if log=$(mktemp "$here/.steam-launch.XXXXXX"); then
        exec 2>"$log"
        mv -fT "$log" "$here/steam-launch.log"
        printf 'CouchCHD Steam launch %s; Qt platform=%s renderer=%s\n' \
            "$(date -u)" "${QT_QPA_PLATFORM:-auto}" "${QT_QUICK_BACKEND:-auto}" >&2
    fi
fi
STEAM_LOG
        fi
        printf '%s\n' "exec \"\$here/builds/${final##*/}/run\" \"\$@\""
    } > "$entry"
    chmod 755 "$entry"
    if [ -d "$tmp/install" ]; then
        pending=$final
        mv "$tmp/install" "$final"
    fi
    printf 'CouchCHD: Switching the launcher to the verified build.\n' >&2
    mv -f "$entry" "$launcher" || fail 'Could not switch the launcher; previous installation unchanged.'
    if [ "$update" = yes ]; then return 0; fi
    cleanup; trap - 0 HUP INT TERM
    printf 'Installed. Relaunch with: %s\n' "$launcher" >&2
    "$launcher" --register-frontend "$frontend" "$launcher"
    if [ "$frontend" = desktop ] && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        exec "$launcher"
    fi
    printf 'Setup complete. Start CouchCHD from your frontend or run the printed launcher in a graphical session.\n' >&2
}
if [ "${1:-}" = --channel ]; then
    [ "$#" = 1 ] || fail 'Unexpected channel arguments.'
    printf '%s\n' "$channel"
    exit 0
fi
install "$@"
