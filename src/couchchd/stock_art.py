"""Upgrade only identified stock bytes; never infer ownership from an image's name."""
from hashlib import sha256
from pathlib import Path
import stat

from .menu_entry import read_regular, store_entry


# Tagged release assets. The five PNGs were byte-identical in previews 5–7.
# Earlier AppImages had their own embedded couchchd.svg, which Steam users
# could also select as their shortcut icon.
_PREVIOUS = {
    'grid.png': {70626: '41792c44f663bac4b127aab5054f77fe62bbb91d5b7120cb54e0e982d8177dfb'},
    'hero.png': {111660: '9a27c755fe15477eef57fa72dc62bc8d25cfb42965290c600b00ea1c8def71ba'},
    'landscape.png': {53984: '69ccbe3c3e1ca48d01004a7a888c63fc3b460c66e3e424c7b49557971d835ba0'},
    'logo.png': {39468: 'bddb9bee1e1d53b13a8048f4d605af2bc57b37a6d9b8189a9d271721baa058d9'},
    'icon.png': {25539: '701e920050e3becb3f531ad2de71e6af8903beb842b904dd10f8f36a6c3461a7'},
    'grid.svg': {661: 'fb9b03daeae831b99f6e4f1508ff8c8ca3285121ac9e9f5ac08f2c8f7ba6d9c7'},
    'hero.svg': {670: 'fe097720acb471bd3d69ea176aa929e1d4e4c478d9e83a8eca6c1da7f01ad3de'},
    'landscape.svg': {993: '95b8cbca9bb77ba3bb5cae9cc17912b9b1c11e83c925e27e5e0970f01aca310a'},
    'logo.svg': {1043: '25fce1bc4f9574b73be34d4bd337b6838cfd4148108f17046d90a751f96c72b5'},
    'icon.svg': {443: 'df3bd1f1509f861da75dc4f13dc460a2bab14e7cfca9f7779cf7a08e22874c47',
                 520: 'c74cf0339e6b44264899a3e0a3b40f8a64771a3c3d241c363466779735325997'},
}


def upgrade_stock(path: Path, replacement: Path, *, steam_legacy: bool = False) -> bool:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        return False
    previous = None
    if digest := _PREVIOUS[replacement.name].get(info.st_size):
        candidate = read_regular(path)
        if candidate is not None and sha256(candidate).hexdigest() == digest:
            previous = candidate
    if previous is None:
        if not steam_legacy or replacement.name not in ('grid.png', 'hero.png'):
            return False
        previous = _preview4_png(path, replacement)
        if previous is None:
            return False
    store_entry(path, replacement.read_bytes(), previous)
    return True


def _preview4_png(path: Path, replacement: Path) -> bytes | None:
    """Recreate previews 1–4's font-dependent PNG locally and require exact bytes."""
    # Skip current stock (sRGB) and unrelated formats using the Qt writer's
    # header. Never feed a user's image to a decoder or read more than the
    # comparison needs; filenames, sizes and headers are not ownership proof.
    width, height = (600, 900) if replacement.stem == 'grid' else (1920, 620)
    with path.open('rb') as f:
        header = f.read(41)
        if (header[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'
                or int.from_bytes(header[16:20], 'big') != width
                or int.from_bytes(header[20:24], 'big') != height
                or header[24:29] != b'\x08\x06\x00\x00\x00'
                or header[37:41] not in (b'pHYs', b'IDAT')):
            return None
        # Qt copied the paint device's resolution into pHYs. A Deck can
        # register while docked and refresh in Gaming Mode (or vice versa).
        # Use that resolution, but never hand absurd font sizes to Qt.
        if header[37:41] == b'pHYs':
            density = f.read(9)
            x, y = int.from_bytes(density[:4], 'big'), int.from_bytes(density[4:8], 'big')
            if density[8:] != b'\x01' or not (1000 <= x <= 24000 and 1000 <= y <= 24000):
                return None
        try:
            from PySide6.QtCore import QByteArray, QBuffer, QIODevice
            from PySide6.QtGui import QGuiApplication, QImage, QPainter
            from PySide6.QtSvg import QSvgRenderer
        except ImportError:
            return None
        # The installer has no GUI; the Settings worker already has one.
        app = QGuiApplication.instance() or QGuiApplication(['couchchd', '-platform', 'offscreen'])
        if not isinstance(app, QGuiApplication):
            return None
        renderer = QSvgRenderer(str(replacement.with_name(f'preview4-{replacement.stem}.svg')))
        image = QImage(renderer.defaultSize(), QImage.Format.Format_ARGB32_Premultiplied)
        if header[37:41] == b'pHYs':
            image.setDotsPerMeterX(x)
            image.setDotsPerMeterY(y)
        image.fill(0)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, 'PNG'):
            return None
        encoded = bytes(data)
        f.seek(0)
        return encoded if f.read(len(encoded) + 1) == encoded else None
