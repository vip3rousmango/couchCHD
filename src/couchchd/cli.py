"""Library commands run without Qt or SDL. Default unattended action: scan."""

import argparse
import os
import json
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__, profiles, scanner


def _parser():
    p = argparse.ArgumentParser(prog='couchchd', description='Verify and organize your disc library without discarding the originals.')
    p.add_argument('--version', action='version', version=f'CouchCHD {__version__}')
    p.add_argument('--root', type=Path, help='ROM root containing system folders (e.g. psx, dreamcast)')
    p.add_argument('--headless', action='store_true', help='run without Qt; scan unless an action is given')
    actions = p.add_mutually_exclusive_group()
    actions.add_argument('--register-frontend', nargs=2, metavar=('FRONTEND', 'LAUNCHER'),
                         help='register an installed launcher; use ports + --root for a custom shell-compatible library')
    actions.add_argument('--auto', action='store_true', help='convert selected systems and quarantine eligible verified originals')
    actions.add_argument('--history', action='store_true', help='show the batch ledger')
    actions.add_argument('--undo', action='store_true', help='restore originals from the most recent undoable batch')
    actions.add_argument('--purge', action='store_true', help='permanently purge verified backups (requires --yes)')
    actions.add_argument('--extract', metavar='CHD', type=Path, help='extract a CD image to a new BIN/CUE pair')
    actions.add_argument('--health', action='store_true', help='check for library issues without writing to the library')
    actions.add_argument('--plan-gib', type=int, metavar='GIB', help='estimate recoverable space (0: all that fit)')
    actions.add_argument('--apply-plan', type=Path, metavar='JSON', help='run an unchanged --plan-gib preview; never purge')
    actions.add_argument('--review', action='store_true', help='list verified outputs and play-test IDs')
    actions.add_argument('--mark-tested', metavar='ID', help='record your play-test of this verified output')
    actions.add_argument('--unmark-tested', metavar='ID', help='clear a play-test mark')
    actions.add_argument('--review-cleanup', action='store_true', help='preview reviewed original files and confirmation token')
    actions.add_argument('--reclaim-reviewed', metavar='TOKEN', help='permanently remove exactly the reviewed backups (requires --yes)')
    actions.add_argument('--handheld-profiles', action='store_true', help='list supported export profiles and player capabilities')
    actions.add_argument('--handheld-list', action='store_true', help='list playable copies for the selected export profile')
    p.add_argument('--handheld-profile', metavar='PROFILE', help='destination firmware/profile ID (default: RG405M)')
    p.add_argument('--handheld-player', metavar='PLAYER', action='append', help='confirmed player ID; repeat to combine instead of profile defaults')
    actions.add_argument('--handheld-export', nargs='+', metavar='ENTRY', help='copy selected handheld entries to --dest, without overwriting')
    actions.add_argument('--catalog-add', nargs=2, metavar=('LABEL', 'LOCATION'),
                         help='remember a mounted ROM library or configured rclone remote:path')
    actions.add_argument('--catalog-forget', metavar='SOURCE_ID', help='forget a source without changing its media')
    actions.add_argument('--catalog-sources', action='store_true', help='list named home/device libraries and last refresh status')
    actions.add_argument('--catalog-refresh', action='store_true', help='recursively inventory ready games and supported originals; retain offline results')
    actions.add_argument('--catalog-list', action='store_true', help='list cached games and originals with their source libraries')
    actions.add_argument('--catalog-where', metavar='RELATIVE_PATH', help='list libraries reporting this relative path')
    actions.add_argument('--catalog-pull', nargs=2, metavar=('SOURCE_ID', 'RELATIVE_PATH'),
                         help='pull an existing compressed game to --dest without conversion')
    p.add_argument('--dest', type=Path, help='destination directory for extraction, handheld export or catalog pull')
    p.add_argument('--yes', action='store_true', help='confirm permanent backup purge')
    p.add_argument('--keep-originals', action='store_true', help='leave input files in place instead of quarantining')
    p.add_argument('--ingest', metavar='SOURCE', help='read-only mounted library path or rclone remote for --auto')
    p.add_argument('--scratch', type=Path, metavar='DIR', help='existing temporary workspace outside the library for --auto')
    p.add_argument('--system', action='append', choices=sorted(scanner.SYSTEM_LABELS), help='restrict batch to this system; repeat to combine')
    p.add_argument('--psp-format', choices=('chd', 'cso'), help='output format for PSP ISO files (default: chd)')
    p.add_argument('--preset', choices=('silent', 'balanced', 'docked'), help='CPU profile; defaults to silent on battery')
    return p


def _register_desktop_steam(launcher: Path) -> str:
    retry = ('With Steam closed, rerun the same install command or open CouchCHD from Applications '
             'and choose Settings > Add to Steam.')
    if os.environ.get('FLATPAK_ID'):
        return 'Steam shortcut skipped: not registering a host launcher from inside Flatpak.'
    try:
        from .ui import steam
        deadline = None
        while True:
            try:
                return steam.register(launcher)
            except steam.SteamRunning:
                if deadline is None:
                    print('Steam is running. In Desktop Mode, choose Steam > Exit.', file=sys.stderr, flush=True)
                    # The installer is often piped into sh: stdin belongs to the script.
                    if not sys.stderr.isatty():
                        break
                    print('Waiting up to 90 seconds to add CouchCHD after Steam exits…', file=sys.stderr, flush=True)
                    deadline = time.monotonic() + 90
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(1, remaining))
        # An existing shortcut's grid images can be updated independently.
        # Never try to create or repair a live client's shortcut database.
        try:
            art = steam.refresh_artwork(launcher)
        except (OSError, RuntimeError, ValueError) as exc:
            art = f'Steam artwork not refreshed: {exc}'
        return f'{art}\nSteam shortcut registration deferred; the Applications menu launcher is ready. {retry}'
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        return f'Steam shortcut not registered: {exc} The Applications menu launcher is ready. {retry}'


def _catalog_action(args):
    from .catalog import Catalog
    catalog = Catalog()
    if args.catalog_add:
        return asdict(catalog.add(*args.catalog_add))
    if args.catalog_forget is not None:
        catalog.remove(args.catalog_forget)
        return {'forgotten': args.catalog_forget}
    if args.catalog_refresh:
        return [asdict(source) for source in catalog.refresh()]
    if args.catalog_sources:
        return [asdict(source) for source in catalog.sources()]
    if args.catalog_pull:
        return {'paths': list(catalog.pull(*args.catalog_pull, args.dest))}
    return [asdict(entry) for entry in catalog.entries(args.catalog_where)]


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if (args.purge or args.reclaim_reviewed is not None) != args.yes:
        parser.error('Permanent cleanup requires --yes; use --yes only with --purge or --reclaim-reviewed.')
    if bool(args.extract or args.handheld_export or args.catalog_pull) != bool(args.dest):
        parser.error('Supply --dest only with --extract, --handheld-export or --catalog-pull')
    if args.plan_gib is not None and args.plan_gib < 0:
        parser.error('--plan-gib must be zero or greater')
    if (args.ingest or args.keep_originals or args.scratch) and not args.auto:
        parser.error('--ingest, --keep-originals and --scratch require --auto')
    if (args.system or args.psp_format) and not args.auto and args.plan_gib is None:
        parser.error('--system and --psp-format require --auto or --plan-gib')
    if args.preset and not (args.auto or args.apply_plan):
        parser.error('--preset requires --auto or --apply-plan')
    if (args.handheld_profile or args.handheld_player) and not (args.handheld_list or args.handheld_export):
        parser.error('Choose --handheld-list or --handheld-export when setting a destination profile or player.')
    if args.handheld_profiles:
        from .handheld_profiles import profiles as destinations
        print(json.dumps(destinations(), ensure_ascii=False, indent=2))
        return 0
    if (args.catalog_add or args.catalog_forget is not None or args.catalog_refresh or args.catalog_sources
            or args.catalog_pull or args.catalog_where is not None or args.catalog_list):
        try:
            print(json.dumps(_catalog_action(args), ensure_ascii=False, indent=2, default=str))
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            print(f'CouchCHD catalog: {exc}', file=sys.stderr)
            return 1
        return 0
    if args.register_frontend:
        from .menu_entry import host_home_path, install_desktop, install_script
        frontend, executable = args.register_frontend
        try:
            launcher = Path(executable).expanduser().absolute()
            if frontend == 'recalbox':
                from .recalbox_menu import register
                register(launcher=launcher)
                return 0
            if not launcher.is_file() or not os.access(launcher, os.X_OK):
                raise ValueError(f'Not an executable CouchCHD launcher: {launcher}')
            if frontend == 'desktop':
                from . import es_frontends
                launcher = host_home_path(launcher)
                print('Applications menu (Utilities):', install_desktop(launcher))
                messages = es_frontends.register(launcher, roms=args.root)
                if sys.platform == 'linux':
                    messages.append(_register_desktop_steam(launcher))
            elif frontend == 'ports':
                if args.root is None or not args.root.is_dir():
                    raise ValueError('Supply the existing ROM root of a shell-compatible ports system with --root.')
                root = args.root.expanduser().absolute()
                entry = root / 'ports/CouchCHD.sh'
                install_script(entry, launcher, args=('--root', str(root)))
                messages = [f'Installed {entry}. Refresh the frontend’s game list.']
            else:
                from . import native_frontends
                messages = native_frontends.register(launcher, frontend, roms=args.root)
            for message in messages:
                print(message)
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            print(f'CouchCHD menu setup: {exc}', file=sys.stderr)
            return 1
    if not (args.headless or args.auto or args.undo or args.history or args.purge or args.extract
            or args.health or args.plan_gib is not None or args.apply_plan or args.review or args.mark_tested
            or args.unmark_tested or args.review_cleanup or args.reclaim_reviewed or args.handheld_list or args.handheld_export):
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
        if args.auto or args.apply_plan:
            cancel = threading.Event()
            previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
            try:
                options = dict(preset=args.preset or profiles.default_preset(),
                               progress=lambda message: print(message, file=sys.stderr, flush=True), cancel=cancel)
                if args.apply_plan:
                    preview = json.loads(args.apply_plan.read_text(encoding='utf-8'))
                    if (not isinstance(preview, dict) or preview.get('root') != str(app.root)
                            or not isinstance(preview.get('sources'), list)
                            or not all(isinstance(path, str) for path in preview['sources'])
                            or not isinstance(preview.get('systems'), list)
                            or not all(isinstance(s, str) and s in scanner.SYSTEM_LABELS for s in preview['systems'])
                            or type(preview.get('target_bytes')) is not int or preview['target_bytes'] < 0
                            or preview.get('psp_format') not in ('chd', 'cso')):
                        raise ValueError('Not a space plan for this library. Create a new --plan-gib preview.')
                    result = app.run_plan(preview['systems'], preview['psp_format'], preview['target_bytes'],
                                          preview['sources'], **options)
                else:
                    result = app.run(systems=set(args.system) if args.system else None, ingest=args.ingest,
                                     psp_format=args.psp_format or 'chd', quarantine=not args.keep_originals,
                                     scratch_dir=args.scratch, **options)
            finally:
                signal.signal(signal.SIGINT, previous)
            code = 2 if result['paused'] else int(bool(result['errors']))
        elif args.health:
            result = app.health()
        elif args.plan_gib is not None:
            result = app.plan(args.system, args.psp_format or 'chd', args.plan_gib * 1024**3)
        elif args.review:
            result = app.review()
        elif args.mark_tested or args.unmark_tested:
            app.set_play_tested(args.mark_tested or args.unmark_tested, bool(args.mark_tested))
            result = {'tested': bool(args.mark_tested)}
        elif args.review_cleanup:
            result = app.reclaim_preview()
        elif args.reclaim_reviewed:
            result = {'purged': app.reclaim_reviewed(args.reclaim_reviewed)}
        elif args.handheld_list or args.handheld_export:
            from .handheld_profiles import DEFAULT_PROFILE
            profile = args.handheld_profile or DEFAULT_PROFILE
            result = (app.export_entries(profile, players=args.handheld_player) if args.handheld_list else
                      app.export_handheld(args.handheld_export, args.dest, profile, players=args.handheld_player))
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
