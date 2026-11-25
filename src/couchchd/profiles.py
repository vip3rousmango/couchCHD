"""Library discovery and hardware-appropriate defaults; preferences remain overridable."""
from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

from . import hardware


_RECALBOX = Path('/recalbox/share')
_BATOCERA_ROMS = Path('/userdata/roms')
_ROCKNIX_ROMS = Path('/storage/roms')
_MODEL = Path('/proc/device-tree/model')
_MEDIA = Path('/run/media')


def is_recalbox() -> bool:
    return _RECALBOX.is_dir()


def is_pi() -> bool:
    try:
        return b'raspberry pi' in _MODEL.read_bytes().lower()
    except OSError:
        return False


def native_root(frontend: str) -> Path:
    if frontend == 'retropie':
        return Path.home() / 'RetroPie/roms'
    if frontend == 'batocera':
        return _BATOCERA_ROMS
    if frontend == 'rocknix':
        return _ROCKNIX_ROMS
    raise ValueError(f'Unknown native frontend: {frontend}')


def roots() -> list[Path]:
    home = Path.home()
    candidates = [home / 'Emulation/roms', _MEDIA / 'mmcblk0p1/Emulation/roms',
                  home / 'RetroDECK/roms', home / 'retrodeck/roms', _RECALBOX / 'roms']
    for path in (native_root('retropie'), _BATOCERA_ROMS, _ROCKNIX_ROMS, home / 'ROMs'):
        if not any(p.is_symlink() for p in (*reversed(path.parents), path)):
            candidates.append(path)
    for pattern in ('*/Emulation/roms', '*/*/Emulation/roms', '*/*/retrodeck/roms'):
        candidates.extend(sorted(_MEDIA.glob(pattern)))
    result: list[Path] = []
    seen = set()
    for path in candidates:
        try:
            st = path.stat()
        except OSError:
            continue
        key = st.st_dev, st.st_ino
        if stat.S_ISDIR(st.st_mode) and key not in seen:
            seen.add(key)
            result.append(path.resolve())
    return result


def default_preset() -> str:
    return 'silent' if hardware.on_battery() else 'balanced'


def worker_limit(preset: str) -> int:
    quarters = {'silent': 2, 'balanced': 3, 'docked': 4}
    if preset not in quarters:
        raise ValueError(f'Unknown thermal preset: {preset}')
    limit = max(1, hardware.cpu_count() * quarters[preset] // 4)
    return min(2, limit) if is_pi() else limit


def _supports_psp_chd() -> bool:
    executable = shutil.which('PPSSPPSDL')
    if not executable:
        return False
    try:
        version = subprocess.run([executable, '--version'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, timeout=4, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    # Accept both "v1.17.1-…" and "PPSSPP 1.17"; don't interpret versions
    # from e.g. SDL/driver warnings as the emulator version.
    match = re.search(rb'^\s*(?:PPSSPP(?:SDL)?(?:\s+version)?[\s:]+)?v?(\d+)\.(\d+)\b',
                      version, re.I | re.M)
    return bool(match and (int(match[1]), int(match[2])) >= (1, 17))


def default_systems() -> set[str]:
    enabled = {'psx', 'segacd', 'saturn', 'dreamcast', 'ps2', 'psp'}
    pi = is_pi()
    if pi:
        enabled.remove('ps2')
    if (pi or is_recalbox()) and not _supports_psp_chd():
        enabled.remove('psp')
    return enabled
