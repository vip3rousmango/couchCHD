"""Library discovery and hardware-appropriate defaults; preferences remain overridable."""
from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import hardware, scanner


_RECALBOX = Path('/recalbox/share')
_BATOCERA_ROMS = Path('/userdata/roms')
_ROCKNIX_ROMS = Path('/storage/roms')
_MODEL = Path('/proc/device-tree/model')
_DMI = Path('/sys/class/dmi/id')
_MEDIA = Path('/run/media')


def is_recalbox() -> bool:
    return _RECALBOX.is_dir()


def _board_model() -> bytes:
    try:
        return _MODEL.read_bytes().lower()
    except OSError:
        return b''


def is_pi() -> bool:
    return b'raspberry pi' in _board_model()


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


@dataclass(frozen=True)
class CompressionPlan:
    cpus: int
    portable: bool


def _deck_board() -> bool:
    # LCD and OLED Decks expose these model names regardless of the distro.
    for name in ('product_name', 'board_name'):
        try:
            model = (_DMI / name).read_text('ascii').strip().lower()
        except (OSError, UnicodeError):
            continue
        if model in {'jupiter', 'galileo', 'steam deck', 'steam deck oled'}:
            return True
    return False


def host_kind() -> str:
    """Hardware pictogram hint. A SteamOS install alone doesn't identify a Deck."""
    return 'deck' if _deck_board() else 'pi' if is_pi() else 'computer'


def _steam_deck() -> bool:
    if os.environ.get('SteamDeck') == '1' or _deck_board():
        return True
    try:
        return platform.freedesktop_os_release().get('ID') == 'steamos'
    except (OSError, UnicodeError):
        return False


def compression_plan(preset: str) -> CompressionPlan:
    quarters = {'silent': 2, 'balanced': 3, 'docked': 4}
    if preset not in quarters:
        raise ValueError(f'Unknown thermal preset: {preset}')
    cpus = hardware.cpu_count()
    budget = max(1, cpus * quarters[preset] // 4)
    if sys.platform != 'linux':
        return CompressionPlan(budget, False)

    ram = hardware.memory_bytes()
    model = _board_model()
    handheld_board = b'anbernic' in model or b'raspberry pi' in model
    # Also protect small ARM boards with an unreadable device tree and
    # low-RAM Linux systems. MemTotal excludes firmware/kernel reservations:
    # 3 GiB separates 1/2 GiB Pi 4s from 4/8 GiB models.
    arm_board = cpus <= 4 and platform.machine().lower() in ('arm64', 'aarch64', 'armv7l')
    low_ram = ram is not None and ram < 3 * 1024**3
    if not (handheld_board or arm_board or (cpus <= 8 and low_ram) or _steam_deck()):
        return CompressionPlan(budget, False)
    budget = min(budget, max(1, (3 * cpus + 2) // 5))  # nearest logical CPU to 60%
    if handheld_board or arm_board:
        budget = min(budget, 1 if ram is None or low_ram else 2)
    elif low_ram:
        budget = 1
    return CompressionPlan(budget, True)


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


def default_systems(psp_format: str = 'chd') -> set[str]:
    if psp_format not in ('chd', 'cso'):
        raise ValueError(f'Unknown PSP format: {psp_format}')
    enabled = set(scanner.FORMATS)
    pi = is_pi()
    if pi:
        enabled.remove('ps2')
    if psp_format == 'chd' and (pi or is_recalbox()) and not _supports_psp_chd():
        enabled.remove('psp')
    return enabled
