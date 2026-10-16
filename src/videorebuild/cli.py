"""CLI entry points. All source access is read-only; results live in a case."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import html
import json
import logging
import os
from pathlib import Path
import platform
import sys
import sqlite3
from xml.etree.ElementTree import ParseError
import time

from . import __version__
from .engine import Options, recover, _write_json
from .storage import Case, SafetyError, scan
from .validate import doctor

LOG = logging.getLogger(__name__)


def positive_int(text: str) -> int:
    n = int(text)
    if n <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return n


def report(case: Case) -> dict:
    counts = dict(case.db.execute('SELECT status,COUNT(*) FROM results GROUP BY status').fetchall())
    sources = [dict(r) for r in case.db.execute('SELECT * FROM sources ORDER BY id')]
    atom_counts = dict(case.db.execute('SELECT kind,COUNT(*) FROM atoms GROUP BY kind').fetchall())
    items = []
    for row in case.db.execute('''SELECT m.digest,m.summary,m.error,r.status,r.detail FROM movies m
                               LEFT JOIN results r ON r.movie_digest=m.digest ORDER BY m.digest'''):
        detail = json.loads(row['detail']) if row['detail'] else {}
        summary = json.loads(row['summary']) if row['summary'] else {}
        items.append({'movie': row['digest'], 'status': row['status'] or 'pending',
                      'summary': summary, 'error': row['error'], 'detail': detail})
    overview = {'tool': 'VideoRebuild', 'version': __version__, 'generated_unix': time.time(),
                'sources': len(sources), 'source_bytes': sum(s['size'] for s in sources),
                'incomplete_source_scans': sum(not s['complete'] for s in sources),
                'atom_candidates': atom_counts, 'movie_metadata_records': len(items),
                'status_counts': counts, 'pending': sum(i['status'] == 'pending' for i in items),
                'codec_track_counts': dict(Counter(t['codec'] for i in items for t in i['summary'].get('tracks', []))),
                'unsupported_reasons': dict(Counter(i['error'] or i['detail'].get('reason', '') for i in items if i['status'] == 'unsupported')),
                'warnings': [
                    'Matched means one byte-distinct decode-checked result within the search model, not proven historical identity.',
                    'No filesystem reconstruction, missing-moov inference, arbitrary sector permutation, or wall-clock joining is performed.',
                    'A source without a usable moov cannot generate a recording reconstruction in this version.',
                    'Legacy backend does not assemble moof; use recover-fragmented with the original image.',
                    'Published files use case-internal hard links when available, otherwise independent copies. Never source links. Copy before editing.']}
    _write_json(case.root / 'reports' / 'summary.json', overview)
    _write_json(case.root / 'reports' / 'results.json', {'summary': overview, 'movies': items, 'sources': sources})
    csvpath = case.root / 'reports' / 'results.csv'
    if csvpath.is_symlink():
        raise SafetyError('Report path is a symlink')
    with csvpath.open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['movie_sha256', 'status', 'stage', 'tracks', 'chunks', 'media_bytes', 'outputs', 'reason'])
        for i in items:
            s, d = i['summary'], i['detail']
            writer.writerow([i['movie'], i['status'], d.get('stage', ''), len(s.get('tracks', [])),
                             s.get('chunks', ''), s.get('media_size', ''), ';'.join(d.get('outputs', [])),
                             i['error'] or d.get('reason', '')])
    rows = []
    for i in items:
        d = i['detail']
        links = []
        for c in d.get('candidates', []):
            p = c['path']
            links.append(f'<a href="../{html.escape(p, quote=True)}">candidate {html.escape(c["media_sha256"][:12])}</a>')
        reason = i['error'] or d.get('reason', '')
        rows.append('<tr><td><code>'+html.escape(i['movie'][:16])+'</code></td><td>'+html.escape(i['status'])+
                    '</td><td>'+html.escape(d.get('stage', ''))+'</td><td>'+'<br>'.join(links)+
                    '</td><td>'+html.escape(reason)+'</td></tr>')
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>VideoRebuild case report</title><style>body{font:16px system-ui;max-width:1200px;margin:36px auto;padding:0 24px;line-height:1.5}
table{border-collapse:collapse;width:100%}td,th{text-align:left;border-bottom:1px solid #ccc;padding:10px;vertical-align:top}
code{font-size:.9em}pre{white-space:pre-wrap}aside{border:1px solid #888;padding:16px}a{overflow-wrap:anywhere}</style>
<h1>VideoRebuild case report</h1><p>No source media was modified.</p><aside><strong>Interpretation:</strong>
“Matched” means one byte-distinct candidate passed the documented search and full decoding checks.
It does not prove recording identity, timestamps, or that a camera never dropped frames.</aside>
<h2>Summary</h2><pre>'''+html.escape(json.dumps(overview, indent=2))+'''</pre><h2>Recordings</h2>
<table><thead><tr><th>Metadata SHA-256</th><th>Status</th><th>Search</th><th>Decode-checked candidates</th><th>Reason</th></tr></thead><tbody>'''+''.join(rows)+'''</tbody></table>
<p>Full details: <a href="results.json">JSON</a> · <a href="results.csv">CSV</a>.</p></html>'''
    target = case.root / 'reports' / 'index.html'
    if target.is_symlink():
        raise SafetyError('HTML report is a symlink')
    target.write_text(page, encoding='utf-8')
    return overview


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='videorebuild', description='Reconstruct AVC/HEVC MP4s from surviving sample tables and recovered media.')
    p.add_argument('--version', action='version', version=f'VideoRebuild {__version__}')
    subs = p.add_subparsers(dest='command', required=True)
    subs.add_parser('doctor', help='Check FFmpeg/ffprobe and print exact versions')
    from .image_recovery import add_parser
    add_parser(subs)
    for name in ('scan', 'recover', 'run', 'report'):
        q = subs.add_parser(name, help={'scan':'Index recovered files and disk images read-only',
                                       'recover':'Reconstruct indexed media', 'run':'Scan, reconstruct, and report',
                                       'report':'Refresh JSON/CSV/HTML reports'}[name])
        q.add_argument('--case', required=True, type=Path, help='Case/output folder on healthy storage, outside every input directory')
        if name in ('scan', 'run'):
            q.add_argument('--input', action='append', type=Path, default=[], help='Recovered file or directory; repeatable')
            q.add_argument('--image', action='append', type=Path, default=[], help='Regular raw disk-image file; repeatable; never a device node')
            q.add_argument('--all-files', action='store_true', help='Inspect every regular file rather than only media-like extensions')
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S')
    if os.name != 'posix':
        print('ERROR: This release supports macOS and Linux; use WSL on Windows.', file=sys.stderr)
        return 2
    try:
        if args.command == 'recover-fragmented':
            from .image_recovery import run as run_fragmented
            return run_fragmented(args)
        if args.command == 'doctor':
            print(json.dumps({'version': __version__, 'python': sys.version, 'platform': platform.platform(),
                              'tools': doctor()}, indent=2))
            return 0
        if args.command in ('scan', 'run') and not (args.input or args.image):
            raise SafetyError('Provide at least one --input or --image')
        # Validate root containment before creating the case directory.
        root = args.case.expanduser().resolve()
        if args.command in ('scan', 'run'):
            for source in args.input:
                source = source.expanduser().resolve()
                if source.is_dir() and (root == source or source in root.parents or root in source.parents):
                    raise SafetyError('Case and input directories must not contain one another')
            for source in [*args.input, *args.image]:
                source = source.expanduser().resolve()
                if source == root or root in source.parents:
                    raise SafetyError('Source is inside the case directory')
        with Case(root) as case:
            logpath = root / 'run.log'
            if logpath.is_symlink():
                raise SafetyError('Log path is a symlink')
            handler = logging.FileHandler(logpath, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            logging.getLogger().addHandler(handler)
            try:
                if args.command in ('scan', 'run'):
                    scan(case, args.input, args.image, args.all_files)
                if args.command in ('recover', 'run'):
                    fields = {name: getattr(args, name) for name in Options.__dataclass_fields__}
                    recover(case, Options(**fields), args.limit, args.retry)
                summary = report(case)
                print(json.dumps(summary, indent=2))
                print(f'Report: {case.root / "reports" / "index.html"}')
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
