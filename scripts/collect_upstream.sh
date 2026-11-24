#!/usr/bin/env bash
# Arguments are fresh source workspace, installed notices directory, release directory.
# Keep downloads compressed (the complete Qt source alone expands to several GiB).
set -euo pipefail
src=$1 notices=$2 out=$3
mkdir -p "$src"
cd "$src"
fetch() {
    curl --proto '=https' --proto-redir '=https' -fsSL "$1" -o "$2"
    printf '%s  %s\n' "$3" "$2" | sha256sum --check
}
fetch https://download.qt.io/archive/qt/6.7/6.7.3/single/qt-everywhere-src-6.7.3.tar.xz qt-everywhere-src-6.7.3.tar.xz a3f1d257cbb14c6536585ffccf7c203ce7017418e1a0c2ed7c316c20c729c801
fetch https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.7.3-src/pyside-setup-everywhere-src-6.7.3.tar.xz pyside-setup-everywhere-src-6.7.3.tar.xz a4c414be013d5051a2d10a9a1151e686488a3172c08a57461ea04b0a0ab74e09
fetch https://files.pythonhosted.org/packages/c9/be/af69521e694442dbde5db29069953f25367ddacaa50d9ae644745853d37c/pygame_ce-2.5.5.tar.gz pygame_ce-2.5.5.tar.gz a7f297c223c6e35f16d65d47a19757005763ea7e90795ccc37c0bc562364ae6b
fetch https://files.pythonhosted.org/packages/64/17/b2bb4de22650adbeef401fa82a1b43028976547a8728602e4d29735b455e/pyinstaller-6.15.0.tar.gz pyinstaller-6.15.0.tar.gz a48fc4644ee4aa2aa2a35e7b51f496f8fbd7eecf6a2150646bbf1613ad07bc2d
if [[ $(uname -m) == x86_64 ]]; then
    fetch https://github.com/AppImage/type2-runtime/archive/dd6cebedcbddde9c82f89b011e8e1d40b6e43868.tar.gz type2-runtime-20251108.tar.gz f5fec23be76e50e2445ed2d018bac49b367490fc483c62c4637e99ec705d27ba
fi
# Copy notices without blindly extracting archive paths, links or devices. Record
# their upstream paths. QtWebEngine (Chromium) is not part of Qt Essentials.
python - "$notices" <<'PY'
import pathlib, shutil, sys, tarfile
for archive in sorted(pathlib.Path('.').glob('*.tar.*')):
    with tarfile.open(archive, mode='r|*') as src, (pathlib.Path(sys.argv[1]) / (archive.name + '-NOTICES.txt')).open('wb') as out:
        for item in src:
            path = pathlib.PurePosixPath(item.name)
            if not item.isfile() or 'qtwebengine' in path.parts:
                continue
            parts = [part.lower() for part in path.parts[1:]]
            if not parts or not (any(p in ('licenses', 'licences') for p in parts) or parts[-1].startswith(('copying', 'copyright', 'lgpl', 'license', 'licence', 'notice', 'qt_attribution', 'qt_attributions'))):
                continue
            out.write(b'\n---- ' + item.name.encode() + b' ----\n')
            with src.extractfile(item) as file:
                shutil.copyfileobj(file, out)
PY
# Each source is identical across architectures. Publish just one copy per release.
if [[ $(uname -m) == x86_64 ]]; then mv -- *.tar.* "$out/"; fi
