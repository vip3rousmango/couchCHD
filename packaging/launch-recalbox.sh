#!/bin/sh
# Extract the bundle below /recalbox/share/system/couchchd, on an executable mount.
# Stop EmulationStation before modifying its gamelist. No read-only system files change.
set -eu
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
case $(uname -m) in aarch64) ;; *) echo 'This bundle requires a 64-bit ARM Linux userspace.' >&2; exit 2;; esac
state=${COUCHCHD_HOME:-/recalbox/share/system/couchchd}
: "${XDG_CONFIG_HOME:=$state/config}" "${XDG_DATA_HOME:=$state/data}" "${XDG_STATE_HOME:=$state/state}" "${XDG_CACHE_HOME:=$state/cache}" "${TMPDIR:=$state/tmp}"
export XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME TMPDIR
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME" "$TMPDIR"
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}${QT_QPA_PLATFORM:-}" ]; then
    export QT_QPA_PLATFORM=eglfs
fi
export QT_QPA_FONTDIR="${QT_QPA_FONTDIR:-$here/fonts}"
exec "$here/couchchd" "$@"
