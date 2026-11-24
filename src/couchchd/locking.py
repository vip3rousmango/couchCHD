"""A persistent inode, not a PID file: the OS releases the lock on crash."""

import os
import stat
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def library_lock(root: Path):
    path = root / 'couchchd.lock'
    if path.is_symlink():
        raise ValueError(f'Refusing symbolic link: {path}')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f'Not a regular lockfile: {path}')
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f'Cannot lock {root}. Close other CouchCHD instances and check filesystem locking support.') from exc
        yield
    finally:
        os.close(fd)
