#!/usr/bin/env bash
# Run as root ONLY in a clean Debian 12 build container; never on the target device.
set -euo pipefail
. /etc/os-release
[[ $ID == debian && $VERSION_ID == 12 ]] || { echo 'Use the Debian 12 release container.' >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    build-essential binutils git ca-certificates curl pkg-config python3 python3-venv \
    libpython3.11 squashfs-tools zstd fonts-dejavu-core xauth xvfb xdotool \
    libfontconfig1 libglib2.0-0 libdbus-1-3 libgl1 libegl1 libgl1-mesa-dri libgbm1 \
    libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 libxcb-xinerama0 \
    libxi6 libxrender1 libx11-xcb1 libegl-mesa0 libwayland-egl1 libwayland-client0 \
    libinput10 libudev1 libdrm2 libasound2 libpulse0 libxext6 file
