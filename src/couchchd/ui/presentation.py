"""Text and row values shared by the controller screens."""
from datetime import datetime

from .. import scanner

_ACTION_ICONS = {
    'tool_devices': 'wireless-handheld.svg',
    'tool_nearby_receiver': 'handheld.svg',
    'tool_export': 'card-transfer.svg',
    'catalog_open': 'library-drive.svg',
}
_SYSTEM_ROWS = frozenset({'system', 'tool_system', 'catalog_system', 'inventory_system', 'tool_filter_by'})


def size(count: int) -> str:
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if abs(count) < 1024 or unit == 'GiB':
            return f'{count:,.0f} B' if unit == 'B' else f'{count:,.1f} {unit}'
        count /= 1024


def row(key, label, detail='', value='', arg=''):
    entry = {'key': key, 'label': str(label), 'detail': str(detail), 'value': str(value), 'arg': str(arg)}
    if key in _SYSTEM_ROWS and arg in scanner.CATALOG_LABELS:
        entry['icon'] = f'console-{arg}.svg'
    elif key in _ACTION_ICONS:
        entry['icon'] = _ACTION_ICONS[key]
    return entry


def _when(value):
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value).astimezone().strftime('%Y-%m-%d %H:%M')
    return str(value or 'Not recorded')
