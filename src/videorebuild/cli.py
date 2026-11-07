"""CLI entry points. All source access is read-only; results live in a case."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import platform
import sys
import sqlite3
from xml.etree.ElementTree import ParseError

from . import __version__
from .engine import Options
from .storage import Case, SafetyError
from .reports import report
from .progress import reporting
from .validate import doctor

LOG = logging.getLogger(__name__)


def positive_int(text: str) -> int:
    n = int(text)
    if n <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return n


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='videorebuild', description='Reconstruct supported MP4 recordings from recovered files and mapped image byte ranges.')
    p.add_argument('--version', action='version', version=f'VideoRebuild {__version__}')
    p.add_argument('--no-progress', action='store_true', help='Disable progress feedback; errors and warnings remain visible')
    subs = p.add_subparsers(dest='command', required=True)
    subs.add_parser('doctor', help='Check FFmpeg/ffprobe and print exact versions')
    terminal = subs.add_parser('tui', help='Open keyboard-driven recovery menus or watch an existing case read-only',
        description='Keyboard-driven recovery menus. Review a command before running it, or watch a case without controlling its process.',
        epilog='Examples: videorebuild tui; videorebuild tui --case /healthy/case --watch. Use arrow keys and Enter; Q returns or quits.')
    terminal.add_argument('--case', type=Path, help='Case/output folder to prefill')
    terminal.add_argument('--watch', action='store_true', help='Open the read-only case viewer directly; requires --case')
    migration = subs.add_parser('migrate-case', help='Back up and explicitly migrate an older case')
    migration.add_argument('--case', required=True, type=Path)
    for name in ('scan', 'recover', 'run', 'report'):
        q = subs.add_parser(name, help={'scan':'Index recovered files and disk images read-only',
                                       'recover':'Reconstruct indexed media', 'run':'Scan, reconstruct, and report',
                                       'report':'Refresh JSON/CSV/HTML reports'}[name])
        q.add_argument('--case', required=True, type=Path, help='Case/output folder on healthy storage, outside every input directory')
        if name in ('scan', 'run'):
            q.add_argument('--input', action='append', type=Path, default=[], help='Recovered file or directory; repeatable')
            q.add_argument('--image', action='append', type=Path, default=[], help='Raw image; without --extents indexes all available bytes, including allocated space; never a device node')
            q.add_argument('--all-files', action='store_true', help='Inspect every regular file rather than only media-like extensions')
            q.add_argument('--report', type=Path, help='PhotoRec report.xml; verifies headers against one image but does not restrict scanning to free space')
            q.add_argument('--extents', type=Path, help='Filesystem DFXML raw byte-run report for exactly one image')
            q.add_argument('--mapfile', type=Path, help='Matching GNU ddrescue mapfile for exactly one image')
            q.add_argument('--map-offset', type=int, default=0, help='Explicit image offset minus mapfile input offset in bytes')
        if name in ('recover', 'run'):
            q.add_argument('--reconstruct-only', action='store_true', help='Export structurally reconstructed encoded bytes without FFmpeg/ffprobe quality checks; outputs are not decode-validated')
            q.add_argument('--deep', action='store_true', help='Search scattered sample-table extents when whole-payload matching fails')
            q.add_argument('--search-workers', type=positive_int, choices=range(1, 9), default=1,
                           help='Independent source-discovery workers (1-8; default: 1); publication remains ordered')
            q.add_argument('--include-recovered', action='store_true', help='If no first-tier match is found, also search second-tier ranges already used by recovered outputs; default searches first tier only')
            if name == 'recover':
                q.add_argument('--allocation-map', type=Path, help='Image-bound filesystem-unallocated JSON policy for an existing index; rejects allocated image reads without reindexing')
            q.add_argument('--max-candidates', type=positive_int, help='Optional mdat candidate cutoff; default searches every eligible candidate')
            q.add_argument('--max-padding', type=int, help='Optional trailing mdat-byte cutoff; default has no padding cutoff')
            q.add_argument('--timeout', type=float, default=300, help='Seconds per FFmpeg or ffprobe validation subprocess (default: 300)')
            q.add_argument('--max-output-bytes', type=positive_int, default=16*1024**3, help='Per-candidate output size ceiling; not a total-case quota')
            q.add_argument('--max-groups', type=positive_int, default=200_000, help='Extent-group batch capacity; batches do not truncate the search')
            q.add_argument('--max-hits', type=positive_int, help='Optional byte-distinct location cutoff per extent; default uncapped')
            q.add_argument('--max-assemblies', type=positive_int, help='Optional assembly cutoff per recording; default uncapped')
            q.add_argument('--max-anchor-checks', type=positive_int, help='Optional NAL/anchor search cutoff per batch; default uncapped')
            q.add_argument('--limit', type=positive_int, help='Process this many new/invalidated metadata records; omit to process all')
            q.add_argument('--retry', action='store_true', help='Retry nonmatched results under unchanged search settings')
            q.add_argument('--max-record-bytes', type=positive_int, default=16*1024*1024, help='Per-fragmented-section buffer ceiling, at most 256 MiB; later indexed starts are still searched')
            q.add_argument('--max-segments', type=positive_int, help='Optional fragmented-start cutoff per initialization; default searches every indexed start')
    for command in subs.choices.values():
        command.add_argument('--no-progress', action='store_true', default=argparse.SUPPRESS,
                             help='Disable progress feedback; errors and warnings remain visible')
    return p


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(effective_argv)
    if args.command == 'tui':
        from .tui import run
        return run(args.case, watch=args.watch, no_progress=args.no_progress)
    with reporting(enabled=not args.no_progress) as feedback:
        return _execute(args, feedback, effective_argv)


def _execute(args, feedback, argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S')
    if os.name != 'posix':
        print('ERROR: This release supports macOS and Linux; use WSL on Windows.', file=sys.stderr)
        return 2
    try:
        if args.command == 'migrate-case':
            from .migration import migrate_case
            print(json.dumps(migrate_case(args.case), indent=2))
            return 0
        if args.command == 'doctor':
            print(json.dumps({'version': __version__, 'python': sys.version, 'platform': platform.platform(),
                              'tools': doctor()}, indent=2))
            return 0
        if args.command in ('recover', 'run'):
            Options(**{name: getattr(args, name) for name in Options.__dataclass_fields__}).check()
            from .image_recovery import check_options
            check_options(args)
        if args.command in ('scan', 'run') and not (args.input or args.image):
            raise SafetyError('Provide at least one --input or --image')
        # Validate root containment before creating the case directory.
        if args.case.expanduser().is_symlink():
            raise SafetyError('Case root must not be a symlink')
        root = args.case.expanduser().resolve()
        if args.command in ('scan', 'run'):
            for source in args.input:
                source = source.expanduser().resolve()
                if source.is_dir() and (root == source or source in root.parents or root in source.parents):
                    raise SafetyError('Case and input directories must not contain one another')
            for source in [*args.input, *args.image, *[p for p in (args.report, args.extents, args.mapfile) if p]]:
                source = source.expanduser().resolve()
                if source == root or root in source.parents:
                    raise SafetyError('Source is inside the case directory')
        with Case(root) as case:
            logpath = root / 'run.log'
            if logpath.is_symlink() or (logpath.exists() and logpath.stat().st_nlink != 1):
                raise SafetyError('Log path is a symlink')
            handler = logging.FileHandler(logpath, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            logging.getLogger().addHandler(handler)
            feedback.log = lambda message: handler.handle(logging.makeLogRecord({
                'name': 'videorebuild.progress', 'levelno': logging.INFO, 'levelname': 'INFO',
                'msg': message, 'args': ()}))
            try:
                handler.handle(logging.makeLogRecord({
                    'name': 'videorebuild.startup', 'levelno': logging.INFO, 'levelname': 'INFO',
                    'msg': json.dumps({'event': 'startup', 'tool': 'VideoRebuild', 'version': __version__,
                        'utc': datetime.now(timezone.utc).isoformat(), 'pid': os.getpid(),
                        'command': args.command, 'argv': argv,
                        'python_executable': sys.executable, 'python_version': sys.version,
                        'cwd': str(Path.cwd()), 'case': str(root),
                        'cli_origin': __file__, 'entrypoint': sys.argv[0]}), 'args': ()}))
                if args.command in ('scan', 'run'):
                    from .workflow import intake
                    intake(case, args)
                if args.command in ('recover', 'run'):
                    from .workflow import reconstruct
                    reconstruct(case, args)
                summary = report(case)
                print(json.dumps(summary, indent=2))
                print(f'Report: {case.root / "reports" / "index.html"}')
            except BaseException as error:
                if isinstance(error, Exception):
                    # The outer error handler runs after this file handler closes.
                    # Save the reason here while keeping terminal reporting there.
                    handler.handle(logging.makeLogRecord({
                        'name': __name__, 'levelno': logging.ERROR, 'levelname': 'ERROR',
                        'msg': str(error), 'args': ()}))
                try:
                    report(case)
                except Exception as error:
                    LOG.warning('Could not refresh reports after interruption: %s', error)
                raise
            finally:
                feedback.log = None
                logging.getLogger().removeHandler(handler)
                handler.close()
        # Completion is not a guarantee that every recording was recovered;
        # statuses in the report are the authoritative machine-readable result.
        return 0
    except KeyboardInterrupt:
        print('\nInterrupted. Re-run the same command to resume completed source scans and attempts.', file=sys.stderr)
        return 130
    except (SafetyError, OSError, ValueError, sqlite3.Error, ParseError) as e:
        LOG.error('%s', e)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
