"""Text and row values shared by the controller screens."""
from datetime import datetime


def size(count: int) -> str:
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if abs(count) < 1024 or unit == 'GiB':
            return f'{count:,.0f} B' if unit == 'B' else f'{count:,.1f} {unit}'
        count /= 1024


def row(key, label, detail='', value='', arg=''):
    return {'key': key, 'label': str(label), 'detail': str(detail), 'value': str(value), 'arg': str(arg)}


def _when(value):
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value).astimezone().strftime('%Y-%m-%d %H:%M')
    return str(value or 'Not recorded')
