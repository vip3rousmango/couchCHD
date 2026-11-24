"""Headless commands never import Qt or SDL. Default unattended action: scan."""

import argparse
import json
import signal
import sqlite3
import sys
import threading
from pathlib import Path

from . import __version__, profiles


def _parser():
    p = argparse.ArgumentParser(prog='couchchd', description='Verify and organize your disc library without discarding the originals.')
    p.add_argument('--version', action='version', version=f'CouchCHD {__version__}')
    p.add_argument('--root', type=Path, help='ROM root containing system folders (e.g. psx, dreamcast)')
    p.add_argument('--headless', action='store_true', help='run without Qt; scan unless an action is given')
    actions = p.add_mutually_exclusive_group()
    actions.add_argument('--auto', action='store_true', help='compress selected systems and quarantine verified originals')
    actions.add_argument('--history', action='store_true', help='show the batch ledger')
    actions.add_argument('--undo', action='store_true', help='restore originals from the most recent undoable batch')
    actions.add_argument('--purge', action='store_true', help='permanently purge verified backups (requires --yes)')
    actions.add_argument('--extract', metavar='CHD', type=Path, help='extract a CD image to a new BIN/CUE pair')
    p.add_argument('--dest', type=Path, help='extraction directory for --extract')
    p.add_argument('--yes', action='store_true', help='confirm permanent backup purge')
    p.add_argument('--keep-originals', action='store_true', help='leave input files in place instead of quarantining')
    p.add_argument('--ingest', metavar='SOURCE', help='read-only mounted library path or rclone remote for --auto')
    p.add_argument('--system', action='append', choices=['psx', 'segacd', 'saturn', 'dreamcast', 'ps2', 'psp'], help='restrict batch to this system; repeat to combine')
    p.add_argument('--preset', choices=('silent', 'balanced', 'docked'), help='CPU profile; defaults to silent on battery')
    return p


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.purge and not args.yes:
        parser.error('--purge permanently deletes verified originals. Supply --yes to confirm.')
    if bool(args.extract) != bool(args.dest):
        parser.error('--extract CHD and --dest DIRECTORY must be used together')
    if (args.ingest or args.system or args.keep_originals or args.preset) and not args.auto:
        parser.error('--ingest, --system, --keep-originals and --preset require --auto')
    if args.yes and not args.purge:
        parser.error('--yes is only meaningful with --purge')
    if not (args.headless or args.auto or args.undo or args.history or args.purge or args.extract):
        try:
            from .ui import launch
        except ImportError as exc:
            print(f'Cannot start the interface: {exc}. Install couchchd[gui] or use --headless.', file=sys.stderr)
            return 1
        return launch(args.root)
    from .app import Backend
    root = args.root or next((p for p in profiles.roots() if p.is_dir()), None)
    if root is None:
        parser.error('No known library found. Supply --root DIRECTORY.')
    try:
        app = Backend(root)
        code = 0
        if args.auto:
            cancel = threading.Event()
            previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
            try:
                result = app.run(systems=set(args.system) if args.system else None, ingest=args.ingest,
                                 quarantine=not args.keep_originals, preset=args.preset or profiles.default_preset(),
                                 progress=lambda message: print(message, file=sys.stderr, flush=True), cancel=cancel)
            finally:
                signal.signal(signal.SIGINT, previous)
            code = 2 if result['paused'] else int(bool(result['errors']))
        elif args.history:
            result = app.history()
        elif args.undo:
            result = {'undone': app.undo()}
        elif args.purge:
            result = {'purged': app.purge()}
        elif args.extract:
            result = {'extracted': str(app.extract(args.extract, args.dest))}
        else:
            result = app.scan()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return code
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f'CouchCHD: {exc}', file=sys.stderr)
        return 1
