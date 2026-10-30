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
from videorebuild.fragmented import walk_prefix
from videorebuild.image_recovery import signature, validate_prefix
from videorebuild.storage import Case, ReaderPool, SafetyError, create_private_json, file_sha256
from videorebuild.validate import doctor


def audit(case: Path, image: Path) -> dict:
    """Sample canonical publications through their original logical source map."""
    started = time.monotonic()
    tools = doctor()
    image_before = signature(image)
    db = sqlite3.connect((case/'case.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        owner = Case(case)
        owner.db = db  # Read-only catalogue access; do not enter a writer case.
        physical = owner.physical_sources()
        if not any(Path(row['path']).resolve() == image.resolve() for row in physical.values()):
            raise SafetyError('Supplied image is not a source of this case')
        counts = dict(db.execute("SELECT status,COUNT(*) FROM jobs WHERE kind='fragmented' GROUP BY status"))
        condition = "kind='fragmented' AND status IN ('decoded_prefix','decoded_segments')"
        count = db.execute('SELECT COUNT(*) FROM jobs WHERE '+condition).fetchone()[0]
        selected = sorted({i * (count-1)//8 for i in range(9)}) if count else []
        results = []
        with ReaderPool(owner.sources(), physical_sources=physical) as pool:
            for index in selected:
                row = db.execute('SELECT detail FROM jobs WHERE '+condition+' ORDER BY key LIMIT 1 OFFSET ?', (index,)).fetchone()
                saved = json.loads(row['detail'])
                if saved.get('historical_validation'):
                    continue
                sid, offset = saved['source_id'], saved['logical_offset']
                # Sample one section per selected initialization. The report
                # states this scope; other sections are not silently certified.
                segment = next(item for item in saved['segments'] if item.get('output'))
                output_name = segment['output']
                expected = next(item for item in saved['output_evidence'] if item['path'] == output_name)
                length = expected['size']
                if not isinstance(length, int) or not 0 < length <= 256*1024*1024:
                    raise ValueError('Saved section exceeds audit size budget')
                ftyp_head = pool.read(sid, offset, 16)
                ftyp_size = int.from_bytes(ftyp_head[:4], 'big')
                if ftyp_size == 1:
                    ftyp_size = int.from_bytes(ftyp_head[8:16], 'big')
                if ftyp_head[4:8] != b'ftyp' or not 8 <= ftyp_size <= 4096:
                    raise SafetyError('Invalid initialization in source')
                moov_head = pool.read(sid, offset+ftyp_size, 16)
                moov_size = int.from_bytes(moov_head[:4], 'big')
                if moov_size == 1:
                    moov_size = int.from_bytes(moov_head[8:16], 'big')
                init_size = ftyp_size+moov_size
                if moov_head[4:8] != b'moov' or not ftyp_size+8 <= init_size <= min(length,1024*1024):
                    raise SafetyError('Invalid bounded initialization size')
                data = pool.read(sid, offset, init_size)+pool.read(sid, segment['logical_start'],
                    segment['logical_end']-segment['logical_start'])
                prefix = walk_prefix(data, init_size, allow_later_start=segment['later_section'])
                output = case/output_name
                if not output.resolve().is_relative_to(case.resolve()/'reconstructed') or output.is_symlink():
                    raise SafetyError('Unsafe publication path in case')
                result = {'image_offset': offset, 'source_id': sid, 'bytes': length,
                          'source_sha256': hashlib.sha256(data).hexdigest(), 'saved_sha256': expected['sha256'],
                          'parsed_end': prefix.end, 'fragment_count': len(prefix.fragments),
                          'sample_counts': prefix.counts, 'eligibility_error': prefix.eligibility_error()}
                try:
                    output_before = signature(output)
                    checked = validate_prefix(output, data, prefix, tools, 120)
                    result.update(published_sha256=file_sha256(output), validation_ok=checked['ok'],
                                  validation_reason=checked['reason'], output_unchanged=signature(output)==output_before)
                    result['ok'] = (result['source_sha256']==result['published_sha256']==result['saved_sha256']
                                    and prefix.end==length and result['eligibility_error'] is None
                                    and checked['ok'] and result['output_unchanged'])
                except FileNotFoundError:
                    result.update(published_sha256=None,validation_ok=False,validation_reason='missing_publication',
                                  output_unchanged=False,ok=False)
                results.append(result)
    finally:
        db.close()
    result = {'tools': tools, 'image_signature': image_before, 'case_status_counts': counts,
              'selection': 'First successful section of up to nine evenly spaced initialization jobs',
              'results': results, 'image_unchanged': signature(image)==image_before,
              'elapsed_seconds': time.monotonic()-started,
              'scope': 'Sampled existing publications only; no whole-case, all-sections or historical-identity claim'}
    result['ok'] = bool(results) and all(row['ok'] for row in results) and result['image_unchanged']
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
        create_private_json(args.output, result)
        print(f"Audit {'passed' if result['ok'] else 'failed'}. Report: {args.output}")
        return 0 if result['ok'] else 1
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, SafetyError) as error:
        print(f'ERROR: {error}. Check the case/image inputs, installed FFmpeg tools, '
              'and a new writable report path.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
