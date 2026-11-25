"""Replace only byte-identical stock PNGs distributed with previews 6 and 7."""
from hashlib import sha256
from pathlib import Path
import stat

from .menu_entry import read_regular, store_entry


# Size first: avoid reading/sizing up unrelated or already-updated library art.
_PREVIOUS = {
    'grid': (70626, '41792c44f663bac4b127aab5054f77fe62bbb91d5b7120cb54e0e982d8177dfb'),
    'hero': (111660, '9a27c755fe15477eef57fa72dc62bc8d25cfb42965290c600b00ea1c8def71ba'),
    'landscape': (53984, '69ccbe3c3e1ca48d01004a7a888c63fc3b460c66e3e424c7b49557971d835ba0'),
    'logo': (39468, 'bddb9bee1e1d53b13a8048f4d605af2bc57b37a6d9b8189a9d271721baa058d9'),
    'icon': (25539, '701e920050e3becb3f531ad2de71e6af8903beb842b904dd10f8f36a6c3461a7'),
}


def upgrade_stock(path: Path, replacement: Path) -> bool:
    size, digest = _PREVIOUS[replacement.stem]
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != size:
        return False
    previous = read_regular(path)
    if previous is None or sha256(previous).hexdigest() != digest:
        return False
    store_entry(path, replacement.read_bytes(), previous)
    return True
