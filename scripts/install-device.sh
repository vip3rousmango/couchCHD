#!/bin/sh
# No arguments: install the pinned GitHub release. SSH transports are optional.
set -eu
umask 077
channel=v0.1.0-preview.13
recalbox=/recalbox
media=/media
mountinfo=/proc/self/mountinfo
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
check_executable() {
    [ ! -L "$1" ] || fail "Refusing linked executable: $1"
    [ -f "$1" ] || fail "Missing regular file: $1"
    [ -x "$1" ] || fail "No execute permission on $1. Check the install volume's mount options and file mode."
}
check_exec_volume() {
    # Probe on builds/ itself. A root login or successful chmod does not prove
    # execution is allowed (e.g. noexec, or a filesystem masking execute bits).
    printf '%s\n' '#!/bin/sh' 'exit 0' > "$tmp/run" || return 1
    if ! chmod 755 "$tmp/run" || [ ! -x "$tmp/run" ] || ! "$tmp/run"; then
        if [ -r "$mountinfo" ]; then
            # Report only the deepest covering mount and permission flags,
            # not remote names, credentials or unrelated mount options.
            awk -v path="$(CDPATH='' cd -P "$tmp" && pwd -P)" '
              {
                mp = $5; gsub(/\\040/, " ", mp)
                if ((mp == "/" || path == mp || index(path, mp "/") == 1) && length(mp) >= depth) {
                    for (i=7; i<=NF && $i!="-"; i++) {}
                    depth=length(mp); mount=mp; fs=$(i+1); flags=""
                    n=split($6 "," $(i+3), opt, ",")
                    for (j=1; j<=n; j++) if (opt[j] ~ /^(noexec|showexec|ro|(fmask|umask)=[0-7]+)$/)
                        flags=flags " " opt[j]
                }
              }
              END { if (depth) printf "CouchCHD: Install mount: %s (%s)%s\n", mount, fs, flags }
            ' "$mountinfo" >&2 || :
        fi
        printf 'CouchCHD: No usable execute permission under %s/builds. Check for noexec or masked execute bits on the install volume.\n' "$base" >&2
        return 1
    fi
}
stage_on_volume() {
    tmp=$(mktemp -d "$base/builds/.setup.XXXXXX") || return 1
    if check_exec_volume; then return 0; fi
    rm -rf "$tmp"; tmp=
    return 1
}
trusted_directory() {
    [ -d "$1" ] && [ ! -L "$1" ] || return 1
    # Some Recalbox BusyBox builds omit stat -c. Numeric POSIX ls gives
    # the mode and UID before the pathname, including when it has spaces.
    LC_ALL=C ls -nd "$1" | awk -v uid="$install_uid" '
        NR == 1 && ($3 == "0" || $3 == uid) && substr($1, 1, 1) == "d" &&
            substr($1, 6, 1) == "-" && substr($1, 9, 1) == "-" { ok=1 }
        END { exit !ok }'
}
# Emit the same policy and runner for bootstrap preflight, updates and the
# persistent launcher. The bootstrap may itself be running from curl | sh;
# it cannot depend on an adjacent file or reopen $0 to copy its own code.
card_script() {
    cat <<'CARD'
set -eu
umask 077
recalbox=/recalbox
mountinfo=/proc/self/mountinfo
temp_roots='/run /tmp'
meminfo=/proc/meminfo
filesystems=/proc/filesystems
cap_status=/proc/self/status
loop_control=/dev/loop-control
loop_zero=/dev/loop0
share=$recalbox/share
base=$share/system/couchchd
die() { printf 'CouchCHD: %s\n' "$*" >&2; exit 1; }
uid=$(id -u)
protected() {
    # $2 is d (directory), - (plain file), or t (system temp root where
    # sticky world-writable directories such as /tmp are also acceptable).
    [ ! -L "$1" ] && LC_ALL=C ls -nd "$1" | awk -v kind="$2" -v uid="$uid" '
        NR == 1 && ($3 == "0" || $3 == uid) {
            type=substr($1,1,1); sticky=(substr($1,10,1) ~ /^[tT]$/)
            ok=(type == (kind == "t" ? "d" : kind) &&
                ((substr($1,6,1) == "-" && substr($1,9,1) == "-") || (kind == "t" && sticky)))
        } END { exit !ok }'
}
card_policy() {
    [ "$(CDPATH='' cd -P "$share/system" && pwd -P)" = "$share/system" ] &&
        protected "$recalbox" d && protected "$share" d && protected "$share/system" d || return 1
    for item in "$base" "$base/builds" "$base/run-couchchd"; do
        if [ -e "$item" ] || [ -L "$item" ]; then
            case $item in */run-couchchd) protected "$item" - && [ ! -x "$item" ] ;; *) protected "$item" d ;; esac || return 1
        fi
    done
    LC_ALL=C awk -v share="$share" -v base="$base" -v boot="$recalbox/boot" '
        {
            mp=$5; gsub(/\\040/, " ", mp)
            if (mp == "/" || mp == "/boot" || mp == boot) systemdev[$3]=1
            if (mp == share "/system" || mp == base || index(mp,base "/")==1) nested=1
            if (mp != share) next
            count++; dev=$3
            for (i=7; i<=NF && $i!="-"; i++) {}
            flags="," $6 "," $(i+3) ","
            n=split(flags,options,","); fm=um=0
            for (j=1; j<=n; j++) {
                if (index(options[j],"fmask=")==1) { fm++; filemask=substr(options[j],7) }
                if (index(options[j],"umask=")==1) { um++; usermask=substr(options[j],7) }
            }
            mask=(fm ? filemask : usermask)
            ok=($4 == "/" && $(i+1) == "exfat" && $(i+2) ~ /^\/dev\// &&
                $(i+2) !~ /^\/dev\/(z?ram|nbd)[0-9]/ && flags !~ /,(noexec|ro),/ &&
                (fm ? fm : um)==1 && mask ~ /^[0-7]?[1357][1357][1357]$/)
        } END { exit !(ok && count == 1 && !nested && !(dev in systemdev)) }' "$mountinfo"
}
temporary_root() {
    for dir in $temp_roots; do
        [ -d "$dir" ] && [ "$(CDPATH='' cd -P "$dir" && pwd -P)" = "$dir" ] && protected "$dir" t || continue
        LC_ALL=C awk -v path="$dir" '
            {
                mp=$5; gsub(/\\040/," ",mp)
                if ((mp=="/" || path==mp || index(path,mp "/")==1) && length(mp)>=depth) {
                    depth=length(mp)
                    for (i=7; i<=NF && $i!="-"; i++) {}
                    ok=($(i+1) ~ /^(tmpfs|ramfs|rootfs|overlay|ext[234]|xfs|btrfs|f2fs)$/ &&
                        ("," $6 "," $(i+3) ",") !~ /,(noexec|ro),/)
                }
            } END { exit !ok }' "$mountinfo" || continue
        printf '%s\n' "$dir"; return 0
    done
    return 1
}
card_policy || die 'Single-card mode requires a protected local exFAT SHARE masking file execute bits, without noexec or nested mounts. No mount policy changed.'
action=$1; shift
if [ "$action" = eligible ]; then exit 0; fi
root=$(temporary_root) || die 'Cannot find a protected writable /run or /tmp without noexec for the private mountpoint.'
command -v mount >/dev/null && command -v umount >/dev/null &&
    grep -Eq '(^|[[:space:]])squashfs$' "$filesystems" &&
    { [ -c "$loop_control" ] || [ -b "$loop_zero" ]; } &&
    awk '/^CapEff:/ {ok=(substr($2,length($2)-5,1) ~ /^[2367abefABEF]$/)} END {exit !ok}' "$cap_status" ||
    die 'Single-card setup needs kernel SquashFS, a loop device and mount privileges (use the Recalbox root SSH login).'
awk '$1=="MemAvailable:" && $2>=65536 {ok=1} END {exit !ok}' "$meminfo" ||
    die 'Less than 64 MiB of available memory for startup. Close other apps and try again.'
if [ "$action" = setup ]; then
    LC_ALL=C df -Pk "$base" | awk 'NR==2 && $4 ~ /^[0-9]+$/ && $4>=196608 {ok=1} END {exit !ok}' ||
        die 'Need at least 192 MiB free on SHARE for the release image.'
    exit 0
fi
digest=$1; image=$2; shift 2
case $digest in ''|*[!0-9a-f]*) die 'Invalid image checksum.' ;; esac
[ "${#digest}" = 64 ] || die 'Invalid image checksum length.'
case $image in
    "$base/builds/$digest/bundle.squashfs") ;;
    "$base"/builds/.setup.*/install/bundle.squashfs) [ "$action" = preflight ] || die 'Unexpected staged image.' ;;
    *) die 'Unexpected single-card image location.' ;;
esac
parent=${image%/*}
[ "$(CDPATH='' cd -P "$parent" && pwd -P)" = "$parent" ] && protected "$parent" d &&
    protected "${parent%/*}" d && protected "$image" - || die 'Missing, redirected or unsafe single-card image.'
# Keep the file open across hashing and loop setup; the image remains on SD
# and the kernel pages it in on demand rather than expanding it in RAM.
exec 3< "$image"
source=/proc/$$/fd/3
actual=$(sha256sum < "$source") || die 'Cannot checksum the persistent image.'
[ "${actual%% *}" = "$digest" ] || die 'Persistent image failed SHA-256 verification; refusing to mount or launch.'
[ "$action" != verify ] || exit 0
private=$(mktemp -d "$root/couchchd.XXXXXX") || die 'Cannot create a private temporary mountpoint.'
point=$private/app; child=
cleanup_card() {
    if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null || :; wait "$child" 2>/dev/null || :; fi
    if awk -v path="$point" '{mp=$5; gsub(/\\040/," ",mp); if(mp==path) found=1} END {exit !found}' "$mountinfo"; then
        umount "$point" || { printf 'CouchCHD: Could not unmount %s; close any processes using it.\n' "$point" >&2; return 1; }
    fi
    if [ -d "$point" ]; then rmdir "$point" || return 1; fi
    rmdir "$private"
}
trap 'rc=$?; trap - 0; cleanup_card || exit 1; exit "$rc"' 0
trap 'exit 1' HUP INT TERM
# Mount beneath a private directory: mounting directly on a mode-0700
# directory would expose the image root permissions to other local users.
mkdir "$point"
if ! mount -t squashfs -o loop,ro,nodev,nosuid "$source" "$point"; then
    die 'Cannot mount the verified SquashFS image read-only. Check loop device and SquashFS support; no SHARE remount was attempted.'
fi
LC_ALL=C awk -v path="$point" '
    {
        mp=$5; gsub(/\\040/," ",mp); if (mp!=path) next
        count++; for(i=7; i<=NF && $i!="-"; i++) {}
        o="," $6 ","
        ok=($(i+1)=="squashfs" && o ~ /,ro,/ && o ~ /,nodev,/ && o ~ /,nosuid,/ && o !~ /,noexec,/)
    } END {exit !(count==1 && ok)}' "$mountinfo" || die 'Image mount did not have the required read-only, nodev, nosuid and executable flags.'
for exe in "$point/launch-recalbox.sh" "$point/couchchd"; do
    [ ! -L "$exe" ] && [ -f "$exe" ] && [ -x "$exe" ] || die "Missing or unsafe executable: $exe"
done
export COUCHCHD_HOME="$base" COUCHCHD_LAUNCHER="$base/run-couchchd"
"$point/launch-recalbox.sh" "$@" 3<&- <&0 &
child=$!
status=0; wait "$child" || status=$?
child=
if [ "$action" = preflight ] && [ "$status" != 0 ]; then
    die "Verified image could not run --version (status $status). No build activated."
fi
exit "$status"
CARD
}
card_call() { card_script | /bin/sh -s -- "$@"; }
approved_mount() {
    case ${1%/*}:${1##*/} in
        "$media":?*|"$recalbox":share*) ;;
        *) return 1 ;;
    esac
    case ${1##*/} in .|..|*[!a-zA-Z0-9._+\ -]*) return 1 ;; esac
    trusted_directory "${1%/*}" && trusted_directory "$1" &&
        [ "$(CDPATH='' cd -P "$1" && pwd -P)" = "$1" ] || return 1
    # An ordinary directory under /media is not a disk. Reject bind aliases
    # of system devices, stacked mounts, and any submount in the code tree.
    LC_ALL=C awk -v path="$1" -v boot="$recalbox/boot" '
        {
            mp=$5; gsub(/\\040/, " ", mp)
            if (mp == "/" || mp == "/boot" || mp == boot) systemdev[$3]=1
            if (mp == path "/couchchd" || index(mp, path "/couchchd/") == 1) nested=1
            if (mp != path) next
            count++; dev=$3
            for (i=7; i<=NF && $i!="-"; i++) {}
            ok=($4 == "/" && $(i+1) ~ /^(ext2|ext3|ext4|btrfs|xfs|f2fs)$/ &&
                $(i+2) ~ /^\/dev\// && $(i+2) !~ /^\/dev\/(z?ram|nbd)[0-9]/ &&
                ("," $6 "," $(i+3) ",") !~ /,(noexec|ro),/)
        }
        END { exit !(count == 1 && ok && !nested && !(dev in systemdev)) }
    ' "$mountinfo"
}
card_asset=CouchCHD-Recalbox-aarch64.squashfs
card_candidate() {
    [ "$frontend:$arch" = recalbox:aarch64 ] && card_call eligible 2>/dev/null
}
card_marker() {
    [ ! -L "$1" ] && [ -f "$1" ] && grep -Fqx '# CouchCHD Recalbox card launcher' "$1"
}
recalbox_placement() {
    default=$recalbox/share/system/couchchd
    binding=$recalbox/share/system/.couchchd-install-volume
    install_uid=$(id -u)
    [ "$(CDPATH='' cd -P "$recalbox/share/system" && pwd -P)" = "$recalbox/share/system" ] ||
        fail 'Refusing a redirected Recalbox SHARE.'
    if [ "$update" = yes ] && [ "$base" = "$default" ] && ! card_marker "$base/run-couchchd"; then return; fi
    if [ "$update" = no ]; then
        mkdir "$binding.lock" || fail "Cannot reserve $binding.lock. Check SHARE is writable and another setup is not running."
        placement_lock=$binding.lock
    fi
    if [ -L "$binding" ] || [ -e "$binding" ]; then
        [ ! -L "$binding" ] && [ -f "$binding" ] && IFS= read -r volume < "$binding" ||
            fail "Invalid Recalbox installation location in $binding."
        approved_mount "$volume" && trusted_directory "$volume/couchchd" &&
            trusted_directory "$volume/couchchd/builds" &&
            regular_executable "$volume/couchchd/run-couchchd" &&
            grep -Fqx '# CouchCHD managed launcher' "$volume/couchchd/run-couchchd" ||
            fail "The recorded installation volume is missing or unsafe: $volume. Reconnect it; no build moved."
        if [ "$update" = yes ]; then
            [ "$base" = "$volume/couchchd" ] || fail 'Update launcher does not match the recorded Recalbox installation.'
        fi
        base=$volume/couchchd
        return
    fi
    if [ "$update" = yes ]; then [ "$base" = "$default" ] || fail 'Missing Recalbox installation location for this launcher.'; fi
    base=$default
    if card_marker "$base/run-couchchd" || card_candidate; then
        [ "$arch" = aarch64 ] || fail 'The single-card image requires ARM64.'
        card_call eligible
        if [ -e "$base/run-couchchd" ] && ! card_marker "$base/run-couchchd"; then
            fail 'Refusing to convert an existing launcher to single-card mode.'
        fi
        card=yes; asset=$card_asset
        printf 'CouchCHD: Using single-card Recalbox storage with a verified read-only SquashFS image.\n' >&2
        return
    fi
    [ ! -L "$base" ] && [ ! -L "$base/builds" ] || fail 'Refusing symlinked installation directory.'
    if mkdir -p "$base/builds" && stage_on_volume; then return; fi
    if [ -e "$base/run-couchchd" ] || [ -L "$base/run-couchchd" ]; then
        fail "Existing installation at $base cannot execute; refusing to move it."
    fi
    [ -r "$mountinfo" ] || fail 'Cannot identify an executable persistent data volume; no download attempted.'
    for volume in "$recalbox"/share* "$media"/*; do
        approved_mount "$volume" || continue
        # The archive and extracted bundle coexist until the launcher switches.
        if ! LC_ALL=C df -Pk "$volume" | awk '
            NR == 2 && $4 ~ /^[0-9]+$/ { enough=($4 + 0 >= 768*1024) }
            END { exit !enough }'; then
            printf 'CouchCHD: Skipping %s: need at least 768 MiB available for setup.\n' "$volume" >&2
            continue
        fi
        base=$volume/couchchd
        # Never adopt an existing tree on a newly discovered volume, even
        # if it contains files with plausible launcher names or checksums.
        mkdir "$base" 2>/dev/null || continue
        fresh_base=$base
        if mkdir "$base/builds" && stage_on_volume; then
            printf 'CouchCHD: Using executable data volume: %s\n' "$volume" >&2
            return
        fi
        rmdir "$base/builds" 2>/dev/null || :
        rmdir "$base" 2>/dev/null || :
        fresh_base=
    done
    fail 'No safe executable persistent data volume found. Mount a writable local Linux data partition under /media/<name> with execute permission, a protected mount root, and at least 768 MiB free, then retry. No download attempted.'
}
check_build() {
    [ ! -L "$1" ] && [ -d "$1" ] || fail "Missing or linked build directory: $1"
    check_executable "$1/run"
    if [ "$arch" = x86_64 ]; then
        check_executable "$1/$asset"
    else
        [ ! -L "$1/couchchd" ] && [ -d "$1/couchchd" ] || fail "Missing or linked ARM bundle directory: $1/couchchd"
        check_executable "$1/couchchd/launch-recalbox.sh"
        check_executable "$1/couchchd/couchchd"
    fi
}
check_update_source() {
    if [ "$card" = yes ]; then card_marker "$launcher"; else regular_executable "$launcher"; fi &&
        grep -Fqx '# CouchCHD managed launcher' "$launcher" ||
        fail 'Updates require an existing managed launcher.'
    installed=$(sed -n 's/^export COUCHCHD_RELEASE_TAG=//p' "$launcher")
    [ -n "$installed" ] || fail 'This is not a managed public release.'
    [ "$installed" = "$expected" ] || fail 'The installed release changed. Close and reopen CouchCHD.'
    if [ "$card" = yes ]; then
        previous=$(sed -n 's/^card_digest=//p' "$launcher")
        valid_digest "$previous" || fail 'The managed card launcher has no valid image target.'
        card_call verify "$previous" "$base/builds/$previous/bundle.squashfs"
        return
    fi
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
    if [ -d "$recalbox/share/system" ]; then
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
    card=no
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
        --probe-asset)
            if [ ! -e "$recalbox/share/system/.couchchd-install-volume" ] && card_candidate; then asset=$card_asset; fi
            printf '%s %s\n' "$arch" "$asset"; return ;;
        --receive) [ "$#" = 3 ] && [ "$arch" = "$2" ] || fail 'Build/target architecture mismatch.'; digest=$3; transport=stream ;;
        -*|*[!a-zA-Z0-9.@_-]*) fail 'Usage: install-device.sh [user@build-host [remote-build-directory]]' ;;
        *) host=$1; dir=${2:-couchCHD/dist/release}; [ "$#" -le 2 ] || fail 'Unexpected arguments.'
            case $dir in ''|-*|*[!a-zA-Z0-9./_-]*) fail 'Unsupported build directory name.' ;; esac
            prefix=$dir/$arch ;;
    esac
    tmp=; entry=; pending=; owns_lock=no
    placement_lock=; fresh_base=; binding_tmp=
    cleanup() {
        # If the final rename failed or was interrupted, the staged launcher
        # still exists. Undo ONLY this install's promotion under the lock.
        if [ -n "$pending" ] && [ -f "$entry" ] && [ ! -e "$tmp/install" ] && [ -d "$pending" ]; then
            mv "$pending" "$tmp/install"
        fi
        if [ -n "$entry" ]; then rm -f "$entry"; fi
        if [ -n "$tmp" ]; then rm -rf "$tmp"; fi
        if [ "$owns_lock" = yes ]; then rmdir "$lock"; fi
        if [ -n "$binding_tmp" ]; then rm -f "$binding_tmp"; fi
        if [ -n "$fresh_base" ] && [ ! -e "$fresh_base/run-couchchd" ]; then
            rm -f "$binding"
            rmdir "$fresh_base/builds" "$fresh_base" 2>/dev/null || :
        fi
        if [ -n "$placement_lock" ]; then rmdir "$placement_lock"; fi
    }
    trap cleanup 0
    trap 'exit 1' HUP INT TERM
    if [ "$frontend" = recalbox ]; then
        recalbox_placement
    elif [ "$update" = no ]; then
        case $frontend in
            batocera) base=/userdata/system/couchchd ;;
            rocknix) base=/storage/.local/share/couchchd ;;
            *) base=${XDG_DATA_HOME:-$HOME/.local/share}/couchchd ;;
        esac
    fi
    launcher=$base/run-couchchd
    [ ! -L "$base" ] && [ ! -L "$base/builds" ] || fail 'Refusing symlinked installation directory.'
    if [ "$update" = no ]; then mkdir -p "$base/builds"; fi
    lock=$base/builds/.install-lock
    mkdir "$lock" || fail "Another installer holds $lock. If it crashed, remove the empty lock directory after checking no setup is running."
    owns_lock=yes
    if [ "$update" = yes ]; then
        check_update_source
    elif [ -L "$launcher" ] || { [ -e "$launcher" ] && { [ ! -f "$launcher" ] || ! grep -Fqx '# CouchCHD managed launcher' "$launcher"; }; }; then
        fail "Will not replace unmanaged $launcher"
    fi
    if [ "$card" = yes ]; then
        card_call setup
        tmp=$(mktemp -d "$base/builds/.setup.XXXXXX")
    else
        [ -n "$tmp" ] || stage_on_volume || fail 'Could not stage an executable build. No build activated.'
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
    if [ "$card" = yes ]; then
        image=$final/bundle.squashfs
        if [ ! -e "$final" ]; then
            mkdir "$tmp/install"
            image=$tmp/install/bundle.squashfs
            mv "$tmp/download" "$image"
        fi
        card_call preflight "$digest" "$image" --version >&2
    elif [ ! -e "$final" ]; then
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
        COUCHCHD_HOME="$tmp" "$tmp/install/run" --version >&2 || fail 'Bundle cannot run here. Check glibc (2.36+), mount permissions and architecture.'
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
        if [ "$card" = yes ]; then
            printf '%s\n' '# CouchCHD Recalbox card launcher' "card_digest=$digest" \
                'set -- launch "$card_digest" "$here/builds/$card_digest/bundle.squashfs" "$@"'
            card_script
        else
            printf '%s\n' "exec \"\$here/builds/${final##*/}/run\" \"\$@\""
        fi
    } > "$entry"
    if [ "$card" != yes ]; then chmod 755 "$entry"; fi
    if [ -d "$tmp/install" ]; then
        pending=$final
        mv "$tmp/install" "$final"
    fi
    if [ -n "$fresh_base" ]; then
        # Remember the chosen disk on SHARE itself. Its absence on a later
        # boot must not silently activate a second install on another disk.
        binding_tmp=$(mktemp "$binding.XXXXXX")
        printf '%s\n' "${fresh_base%/*}" > "$binding_tmp"
        mv -f "$binding_tmp" "$binding" && [ -f "$binding" ] && [ ! -L "$binding" ] ||
            fail 'Could not record the selected Recalbox volume.'
        binding_tmp=
    fi
    printf 'CouchCHD: Switching the launcher to the verified build.\n' >&2
    mv -f "$entry" "$launcher" || fail 'Could not switch the launcher; previous installation unchanged.'
    if [ "$update" = yes ]; then return 0; fi
    cleanup; trap - 0 HUP INT TERM
    if [ "$card" = yes ]; then
        set -- /bin/sh "$launcher"
        printf 'Installed. Relaunch with: /bin/sh %s\n' "$launcher" >&2
    else
        set -- "$launcher"
        printf 'Installed. Relaunch with: %s\n' "$launcher" >&2
    fi
    if ! "$@" --register-frontend "$frontend" "$launcher"; then
        printf 'CouchCHD is installed, but menu registration did not finish.\n' >&2
        [ "$frontend" = desktop ] && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || return 1
    fi
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
