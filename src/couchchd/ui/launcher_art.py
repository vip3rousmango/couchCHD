"""Fill artwork for existing managed entries, independently of binary updates."""
from functools import partial
import os
from pathlib import Path
import sys

from .. import menu_entry, recalbox_menu


def managed_launcher() -> Path | None:
    value = os.environ.get('COUCHCHD_LAUNCHER')
    if sys.platform != 'linux' or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() and path.name == 'run-couchchd' else None


def refresh(launcher: Path) -> str:
    share = recalbox_menu.SHARE
    if (share / 'system').is_dir():
        actions = [partial(recalbox_menu.refresh_artwork, share)]
    else:
        from . import steam
        actions = [menu_entry.refresh_desktop_artwork, steam.refresh_artwork]
    results = []
    for action in actions:
        try:
            results.append(action(launcher))
        except (OSError, ValueError, RuntimeError) as exc:
            results.append(str(exc))
    return '\n\n'.join(results)
