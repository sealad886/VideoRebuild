#!/usr/bin/env python3
"""Audit up to nine existing recovered prefixes against their original image.

Private media stays on the source volumes. Only structural/hash results are
written to the supplied local JSON destination; no recovery job is resumed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from videorebuild.fragmented import box_at, walk_prefix
from videorebuild.image_recovery import sha_file, signature, validate_prefix
from videorebuild.storage import SafetyError
from videorebuild.validate import doctor


def audit(case: Path, image: Path) -> dict:
    started = time.monotonic()
    tools = doctor()
    image_before = signature(image)
    db = sqlite3.connect((case/'fragmented.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)
    try:
        counts = dict(db.execute('SELECT status,COUNT(*) FROM results GROUP BY status'))
        count = counts.get('decoded_prefix', 0)
        selected = sorted({i * (count-1)//8 for i in range(9)}) if count else []
        rows = [db.execute("SELECT offset,detail_path FROM results WHERE status='decoded_prefix' ORDER BY offset LIMIT 1 OFFSET ?", (index,)).fetchone()
                for index in selected]
    finally:
        db.close()
    results = []
    with image.open('rb') as source:
        for offset, detail_path in rows:
            saved = json.loads((case/detail_path).read_text())
            length = saved['output_bytes']
            if not isinstance(length, int) or not 0 < length <= 256*1024*1024:
                raise ValueError('Saved prefix length must be between 1 byte and 256 MiB')
            output = case/'recovered'/f'offset-{offset:014d}-prefix.mp4'
            output_before = signature(output)
            source.seek(offset)
            data = source.read(length)
            if len(data) != length:
                raise ValueError('Short source-image read')
            init_size = box_at(data, box_at(data, 0).end).end
            prefix = walk_prefix(data, init_size)
            checked = validate_prefix(output, data, prefix, tools, 120)
            row = {
                'image_offset': offset, 'bytes': length,
                'source_sha256': hashlib.sha256(data).hexdigest(),
                'published_sha256': sha_file(output),
                'saved_sha256': saved['candidate_sha256'],
                'parsed_end': prefix.end, 'fragment_count': len(prefix.fragments),
                'sample_counts': prefix.counts, 'eligibility_error': prefix.eligibility_error(),
                'validation_ok': checked['ok'], 'validation_reason': checked['reason'],
                'output_unchanged': signature(output) == output_before,
            }
            row['ok'] = (row['source_sha256'] == row['published_sha256'] == row['saved_sha256']
                         and prefix.end == length and row['eligibility_error'] is None
                         and checked['ok'] and row['output_unchanged'])
            results.append(row)
            print(f"{len(results)}/{len(selected)} offset={offset} bytes={length} ok={row['ok']}", flush=True)
    result = {'tools': tools, 'image_signature': image_before, 'case_status_counts': counts,
              'selection': 'Up to nine evenly spaced successful rows by offset, including first and last',
              'results': results, 'image_unchanged': signature(image) == image_before,
              'elapsed_seconds': time.monotonic()-started,
              'scope': 'Sampled existing publications only; no whole-case or historical-identity claim'}
    result['ok'] = bool(results) and all(r['ok'] for r in results) and result['image_unchanged']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        epilog='Exit codes: 0 = sampled prefixes passed, 1 = audit mismatch, 2 = input/tool error. '
               'The JSON report contains local paths; inspect it before sharing.')
    parser.add_argument('case', type=Path, help='Existing fragmented recovery case (read-only)')
    parser.add_argument('image', type=Path, help='Original regular raw image (read-only)')
    parser.add_argument('output', type=Path, help='New JSON report outside the case; never overwritten')
    args = parser.parse_args()
    try:
        if args.output.resolve().is_relative_to(args.case.resolve()):
            raise ValueError('Report must be outside the read-only recovery case')
        if args.output.exists():
            raise ValueError('Report already exists; choose a new output filename')
        result = audit(args.case, args.image)
        with args.output.open('x', encoding='utf-8') as f:
            json.dump(result, f, indent=2)
            f.write('\n')
        print(f"Audit {'passed' if result['ok'] else 'failed'}. Report: {args.output}")
        return 0 if result['ok'] else 1
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, SafetyError) as error:
        print(f'ERROR: {error}. Check the case/image inputs, installed FFmpeg tools, '
              'and a new writable report path.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
