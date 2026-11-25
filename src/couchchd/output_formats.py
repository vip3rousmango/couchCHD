"""Published game artifacts, distinct from playlists and user-supplied disc images."""
from pathlib import Path


SUFFIX_BY_MODE = {
    'createcd': '.chd', 'createdvd': '.chd', 'cso': '.cso', 'rvz': '.rvz',
    'xiso': '.xiso.iso', 'trim3ds': '.trim.cci', '7z': '.7z',
}
_SUFFIXES = tuple(set(SUFFIX_BY_MODE.values()))
# An XISO omits original disc layout; a trimmed cartridge omits physical padding.
# Even a verified derivative must never authorize quarantine or permanent purge.
_REVERSIBLE = ('.chd', '.cso', '.rvz', '.7z')


def is_output(path: Path) -> bool:
    return path.name.casefold().endswith(_SUFFIXES)


def can_replace_source(path: Path) -> bool:
    return path.name.casefold().endswith(_REVERSIBLE)
