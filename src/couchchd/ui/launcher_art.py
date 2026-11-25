"""Fill artwork for existing managed entries, independently of binary updates."""
import os
from pathlib import Path
import sys

from .. import menu_entry, recalbox_menu
from . import steam


def managed_launcher() -> Path | None:
    value = os.environ.get('COUCHCHD_LAUNCHER')
    if sys.platform != 'linux' or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() and path.name == 'run-couchchd' else None


def refresh(launcher: Path) -> str:
    share = Path('/recalbox/share')
    if (share / 'system').is_dir():
        actions = [(recalbox_menu.refresh_artwork, share)]
    else:
        actions = [(menu_entry.refresh_desktop_artwork, launcher), (steam.refresh_artwork, launcher)]
    results = []
    for action, target in actions:
        try:
            results.append(action(target))
        except (OSError, ValueError, RuntimeError) as exc:
            results.append(str(exc))
    return '\n\n'.join(results)
