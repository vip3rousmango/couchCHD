#!/bin/sh
# The archive and this entry point retain their original names on all ARM targets.
# Frontend installers set COUCHCHD_HOME to their persistent application directory.
set -eu
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
case $(uname -m) in aarch64) ;; *) echo 'This bundle requires a 64-bit ARM Linux userspace.' >&2; exit 2;; esac
if [ -n "${COUCHCHD_HOME:-}" ]; then
    state=$COUCHCHD_HOME
elif [ -d /recalbox/share/system ]; then
    state=/recalbox/share/system/couchchd
else
    state=${XDG_DATA_HOME:-$HOME/.local/share}/couchchd
fi
: "${XDG_CONFIG_HOME:=$state/config}" "${XDG_DATA_HOME:=$state/data}" "${XDG_STATE_HOME:=$state/state}" "${XDG_CACHE_HOME:=$state/cache}" "${TMPDIR:=$state/tmp}"
export XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME TMPDIR
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME" "$TMPDIR"
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}${QT_QPA_PLATFORM:-}" ]; then
    export QT_QPA_PLATFORM=eglfs
fi
export QT_QPA_FONTDIR="${QT_QPA_FONTDIR:-$here/fonts}"
exec "$here/couchchd" "$@"
