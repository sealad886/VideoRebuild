"""CLI entry points. All source access is read-only; results live in a case."""
from __future__ import annotations

import argparse
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
    subs = p.add_subparsers(dest='command', required=True)
    subs.add_parser('doctor', help='Check FFmpeg/ffprobe and print exact versions')
    migration = subs.add_parser('migrate-case', help='Back up and explicitly migrate an older case')
    migration.add_argument('--case', required=True, type=Path)
    for name in ('scan', 'recover', 'run', 'report'):
        q = subs.add_parser(name, help={'scan':'Index recovered files and disk images read-only',
                                       'recover':'Reconstruct indexed media', 'run':'Scan, reconstruct, and report',
                                       'report':'Refresh JSON/CSV/HTML reports'}[name])
        q.add_argument('--case', required=True, type=Path, help='Case/output folder on healthy storage, outside every input directory')
        if name in ('scan', 'run'):
            q.add_argument('--input', action='append', type=Path, default=[], help='Recovered file or directory; repeatable')
            q.add_argument('--image', action='append', type=Path, default=[], help='Regular raw disk-image file; repeatable; never a device node')
            q.add_argument('--all-files', action='store_true', help='Inspect every regular file rather than only media-like extensions')
            q.add_argument('--report', type=Path, help='PhotoRec report.xml; verifies recovered headers against exactly one image')
            q.add_argument('--extents', type=Path, help='Filesystem DFXML raw byte-run report for exactly one image')
            q.add_argument('--mapfile', type=Path, help='Matching GNU ddrescue mapfile for exactly one image')
            q.add_argument('--map-offset', type=int, default=0, help='Explicit image offset minus mapfile input offset in bytes')
        if name in ('recover', 'run'):
            q.add_argument('--deep', action='store_true', help='Search scattered sample-table extents when whole-payload matching fails')
            q.add_argument('--max-candidates', type=positive_int, default=512, help='Maximum mdat candidates per metadata record (default: 512)')
            q.add_argument('--max-padding', type=int, default=1024*1024, help='Maximum trailing mdat bytes beyond expected span (default: 1048576)')
            q.add_argument('--timeout', type=float, default=300, help='Seconds per FFmpeg or ffprobe validation subprocess (default: 300)')
            q.add_argument('--max-output-bytes', type=positive_int, default=16*1024**3, help='Per-candidate output size ceiling; not a total-case quota')
            q.add_argument('--max-groups', type=positive_int, default=200_000, help='Maximum extent targets per deep-search batch')
            q.add_argument('--max-hits', type=positive_int, default=32, help='Maximum byte-distinct locations per extent')
            q.add_argument('--max-assemblies', type=positive_int, default=64, help='Maximum complete extent combinations per recording')
            q.add_argument('--max-anchor-checks', type=positive_int, default=2_000_000, help='Separate limits for NAL-chain checks and structural anchor checks per deep-search batch')
            q.add_argument('--limit', type=positive_int, help='Process this many new/invalidated metadata records; omit to process all')
            q.add_argument('--retry', action='store_true', help='Retry nonmatched results under unchanged search settings')
            q.add_argument('--max-record-bytes', type=positive_int, default=16*1024*1024, help='Fragmented recording window ceiling, at most 256 MiB')
            q.add_argument('--max-segments', type=positive_int, default=64, help='Maximum candidate starts examined per initialization, including rejected starts')
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
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
            try:
                if args.command in ('scan', 'run'):
                    from .workflow import intake
                    intake(case, args)
                if args.command in ('recover', 'run'):
                    from .workflow import reconstruct
                    reconstruct(case, args)
                summary = report(case)
                print(json.dumps(summary, indent=2))
                print(f'Report: {case.root / "reports" / "index.html"}')
            except BaseException:
                try:
                    report(case)
                except Exception as error:
                    LOG.warning('Could not refresh reports after interruption: %s', error)
                raise
            finally:
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
