#!/bin/sh
# Pull from a trusted SSH build host, or receive a verified stream from send-device.sh.
set -eu
umask 077
fail() { printf 'CouchCHD: %s\n' "$*" >&2; exit 1; }
platform() {
    [ "$(uname -s)" = Linux ] || fail 'A Linux target is required.'
    arch=$(uname -m)
    case $arch in
        x86_64) asset=CouchCHD-x86_64.AppImage; base=${XDG_DATA_HOME:-$HOME/.local/share}/couchchd ;;
        aarch64) [ -d /recalbox/share/system ] || fail 'The ARM bundle requires Recalbox.'
            asset=CouchCHD-Recalbox-aarch64.tar.gz; base=/recalbox/share/system/couchchd ;;
        *) fail "Unsupported architecture: $arch (64-bit userspace required)." ;;
    esac
    # A 64-bit kernel can run a 32-bit userspace. EI_CLASS of the running shell is authoritative.
    elf=$(od -An -tu1 -j4 -N1 /proc/$$/exe | tr -d '[:space:]')
    [ "$elf" = 2 ] || fail 'A 64-bit Linux userspace is required.'
}
install() {
    platform
    case ${1:-} in
        --probe) printf '%s\n' "$arch"; return ;;
        --receive) [ "$#" = 3 ] && [ "$arch" = "$2" ] || fail 'Build/target architecture mismatch.'; digest=$3; host='' ;;
        ''|-*|*[!a-zA-Z0-9.@_-]*) fail 'Usage: install-device.sh user@build-host [remote-build-directory]' ;;
        *) host=$1; dir=${2:-couchCHD/dist/release}; [ "$#" -le 2 ] || fail 'Unexpected arguments.'
            case $dir in ''|-*|*[!a-zA-Z0-9./_-]*) fail 'Unsupported build directory name.' ;; esac
            prefix=$dir/$arch
            manifest=$(ssh -n "$host" "cat '$prefix/SHA256SUMS-$arch'") || fail 'Cannot read the build manifest over SSH.'
            digest=$(printf '%s\n' "$manifest" | awk -v f="$asset" '$2==f {print $1}') ;;
    esac
    case $digest in ''|*[!0-9a-f]*) fail 'Invalid or missing SHA-256 digest.' ;; esac
    [ "${#digest}" = 64 ] || fail 'Invalid SHA-256 length.'
    mkdir -p "$base/builds"
    tmp=$(mktemp -d "$base/builds/.setup.XXXXXX")
    lock=$base/builds/.install-lock
    owns_lock=no
    cleanup() { rm -rf "$tmp"; if [ "$owns_lock" = yes ]; then rmdir "$lock"; fi; }
    trap cleanup EXIT
    trap 'exit 1' HUP INT TERM
    if [ -n "$host" ]; then ssh -n "$host" "cat '$prefix/$asset'" > "$tmp/download"; else cat > "$tmp/download"; fi
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
            printf '%s\n' '#!/bin/sh' 'exec "$(dirname "$0")/couchchd/launch-recalbox.sh" "$@"' > "$tmp/install/run"
        fi
        chmod 755 "$tmp/install/run"
        "$tmp/install/run" --version >&2 || fail 'Bundle cannot run here. Check glibc, mount permissions and architecture.'
        mv "$tmp/install" "$final"
    else
        "$final/run" --version >&2 || fail "Previously installed build at $final cannot start."
    fi
    printf '%s\n' '#!/bin/sh' '# CouchCHD managed launcher' "exec \"\$(dirname \"\$0\")/builds/${final##*/}/run\" \"\$@\"" > "$tmp/entry"
    chmod 755 "$tmp/entry"; mv -f "$tmp/entry" "$launcher"
    cleanup; trap - EXIT HUP INT TERM
    printf 'Installed. Relaunch with: %s\n' "$launcher" >&2
    if [ "$arch" = aarch64 ]; then
        # Never seize the KMS display from EmulationStation during SSH provisioning.
        exec "$launcher" --headless --root /recalbox/share/roms
    elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then exec "$launcher";
    else printf 'No desktop display in this session. Run the launcher from Desktop Mode.\n' >&2; fi
}
install "$@"
