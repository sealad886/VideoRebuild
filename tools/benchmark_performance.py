#!/usr/bin/env python3
"""Measure synthetic recovery hot paths; writes only disposable temporary cases.

Run with PYTHONPATH pointing to the source revision being measured. Timings are
diagnostic, never a pass/fail threshold or a full-image recovery prediction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import struct
import sys
import tempfile
import time
import tracemalloc
from types import SimpleNamespace
from unittest.mock import patch

import videorebuild
from videorebuild.bmff import Chunk
from videorebuild.candidate_tiers import CandidateTiers
from videorebuild.engine import _TranslationPlan, check_samples
from videorebuild.image_recovery import _backfill_fragment_manifests, validate_prefix
from videorebuild.ranges import Extent
from videorebuild.reports import report
from videorebuild.storage import Case, ReaderPool, scan_logical, source_identity


def quick_screen(trials: int) -> dict:
    sample_count, hypotheses = 100_000, 100
    elapsed = []
    with tempfile.TemporaryDirectory(prefix='videorebuild-benchmark-') as directory:
        source = Path(directory) / 'samples.bin'
        source.write_bytes(b'\0\0\0\4\x65ABC' * sample_count)
        with ReaderPool({1: source_identity(source)}) as pool:
            for _ in range(trials):
                chunk = Chunk(0, 0, 0, (8,) * sample_count, 0)
                movie = SimpleNamespace(chunks=[chunk], tracks=[SimpleNamespace(
                    handler=b'vide', codec=b'avc1', nal_length=4)])
                started = time.perf_counter()
                plan = _TranslationPlan(movie)
                accepted = sum(check_samples(movie, plan.mapping(1, 0), pool, quick=True)
                               for _ in range(hypotheses))
                elapsed.append(time.perf_counter() - started)
                if accepted != hypotheses:
                    raise AssertionError('quick screening lost a valid hypothesis')
        if source.read_bytes() != b'\0\0\0\4\x65ABC' * sample_count:
            raise AssertionError('screening changed its source')
    return result(elapsed, samples=sample_count, hypotheses=hypotheses,
                  scope='plan construction and sparse framing checks; no decode')


def tier_gate(trials: int) -> dict:
    count, requests = 100_000, 1_000
    elapsed = []
    with tempfile.TemporaryDirectory(prefix='videorebuild-benchmark-') as directory:
        root = Path(directory)
        source = root / 'image.bin'
        source.write_bytes(b'X' * (2 * count))
        with Case(root / 'case') as case:
            sid = case.register_source(source, 'image')
            logical = case.add_stream('mapped.mp4',
                [Extent(i, sid, 2 * i, 1) for i in range(count)], size=count)
            sources = {row['id']: dict(row) for row in case.db.execute('SELECT * FROM sources')}
            with ReaderPool(sources, physical_sources=case.physical_sources()) as pool:
                view = CandidateTiers(case, pool).view()
                for _ in range(trials):
                    started = time.perf_counter()
                    permitted = sum(view.candidate_permits(logical, count - 1, 1)
                                    for _ in range(requests))
                    elapsed.append(time.perf_counter() - started)
                    if permitted != requests:
                        raise AssertionError('tier lookup rejected an unused mapped byte')
    return result(elapsed, extents=count, requests=requests,
                  scope='narrow candidate tier lookups near end of mapped source')


def scan(trials: int) -> dict:
    count = 30_000
    raw = (struct.pack('>I4s', 12, b'mdat') + b'DATA') * count
    digest = hashlib.sha256(raw).hexdigest()
    elapsed = []
    with tempfile.TemporaryDirectory(prefix='videorebuild-benchmark-') as directory:
        root = Path(directory)
        source = root / 'atoms.bin'
        source.write_bytes(raw)
        for trial in range(trials):
            with Case(root / f'case-{trial}') as case:
                sid = case.register_source(source, 'image')
                started = time.perf_counter()
                scan_logical(case, sid)
                elapsed.append(time.perf_counter() - started)
                atoms = case.db.execute('SELECT offset,size,header FROM atoms ORDER BY offset').fetchall()
                if [(row[0], row[1], row[2]) for row in atoms] != [
                        (i * 12, 12, 8) for i in range(count)]:
                    raise AssertionError('scan changed atom locations or sizes')
                if case.sources()[sid]['sha256'] != digest:
                    raise AssertionError('scan changed source digest')
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise AssertionError('scan changed source bytes')
    return result(elapsed, atoms=count, bytes=len(raw),
                  scope='dense synthetic atom indexing including database commits')


def catalogue(trials: int) -> dict:
    count = 30_000
    detail = {'reason': 'synthetic < & é', 'outputs': [],
              'nested': {'samples': list(range(10)), 'truth': True}}
    elapsed = []
    with tempfile.TemporaryDirectory(prefix='videorebuild-benchmark-') as directory:
        with Case(Path(directory) / 'case') as case:
            case.db.executemany('INSERT INTO jobs VALUES(?,?,?,?,?)',
                (('ordinary', f'movie-{i:06}', 'blocked', json.dumps(detail), 0.0)
                 for i in range(count)))
            case.db.commit()
            for _ in range(trials):
                started = time.perf_counter()
                overview = report(case)
                elapsed.append(time.perf_counter() - started)
                document = json.loads((case.root / 'reports' / 'results.json').read_text())
                if overview['processed'] != count or len(document['jobs']) != count:
                    raise AssertionError('catalogue lost jobs')
                if any(job['detail'] != detail for job in document['jobs']):
                    raise AssertionError('catalogue changed job details')
    return result(elapsed, jobs=count, scope='all JSON, CSV and HTML report passes')


def packet_validation(trials: int) -> dict:
    count = 100_000
    payload = b'\0\0\0\2\x65X'
    digest = hashlib.sha256(payload).hexdigest()
    samples = [SimpleNamespace(track_id=1, offset=i * len(payload), size=len(payload),
                               duration=1, composition_offset=0) for i in range(count)]
    prefix = SimpleNamespace(starts={1: 0}, counts={1: count},
        fragments=[SimpleNamespace(samples=samples)],
        initialization=SimpleNamespace(tracks={1: SimpleNamespace(
            handler='vide', codec='avc1', timescale=1000)}))
    probe_json = json.dumps({'streams': [{'id': '0x1', 'index': 0, 'codec_name': 'h264',
        'time_base': '1/1000', 'nb_read_packets': str(count), 'nb_read_frames': str(count)}],
        'packets': [{'stream_index': 0, 'data_hash': 'SHA256:' + digest,
                     'dts': i, 'pts': i, 'duration': 1} for i in range(count)]})

    def fake_tool(argv, timeout, **kwargs):
        return {'argv': argv, 'returncode': 0, 'timeout': False, 'stderr': '',
                'stdout': probe_json if argv[0] == 'ffprobe' else '',
                'stdout_truncated': False, 'stderr_truncated': False}

    elapsed = []
    raw = payload * count
    with patch('videorebuild.image_recovery.run_tool', side_effect=fake_tool):
        for _ in range(trials):
            started = time.perf_counter()
            evidence = validate_prefix(Path('synthetic.mp4'), raw, prefix,
                {'ffprobe': {'path': 'ffprobe'}, 'ffmpeg': {'path': 'ffmpeg'}}, 30)
            elapsed.append(time.perf_counter() - started)
            if not evidence['ok']:
                raise AssertionError(evidence['reason'])
    return result(elapsed, packets=count,
                  scope='Python packet hashing, JSON parsing and exact timing; subprocesses stubbed')


def manifest_backfill(trials: int) -> dict:
    count = 2_000
    elapsed = []
    with tempfile.TemporaryDirectory(prefix='videorebuild-benchmark-') as directory:
        with Case(Path(directory) / 'case') as case:
            detail = json.dumps({'outputs': [], 'padding': 'X' * 8192})
            case.db.executemany('INSERT INTO jobs VALUES(?,?,?,?,?)',
                (('fragmented', str(i), 'blocked', detail, 0.0) for i in range(count)))
            case.db.commit()
            for _ in range(trials):
                started = time.perf_counter()
                _backfill_fragment_manifests(case, SimpleNamespace())
                elapsed.append(time.perf_counter() - started)
            tracemalloc.start()
            try:
                _backfill_fragment_manifests(case, SimpleNamespace())
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            unchanged = case.db.execute('SELECT COUNT(*) FROM jobs WHERE detail=?', (detail,)).fetchone()[0]
            if unchanged != count:
                raise AssertionError('manifest backfill changed blocked jobs')
    return result(elapsed, jobs=count, detail_padding_bytes=8192, peak_traced_bytes=peak,
                  scope='manifest startup over completed blocked jobs; Python allocations exclude setup')


def result(elapsed: list[float], **workload) -> dict:
    return {'seconds': elapsed, 'median_seconds': statistics.median(elapsed), **workload}


SCENARIOS = {'quick-screen': quick_screen, 'tier-gate': tier_gate,
             'scan': scan, 'catalogue': catalogue, 'packet-validation': packet_validation,
             'manifest-backfill': manifest_backfill}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=SCENARIOS, action='append',
                        help='Repeat to select scenarios; default runs all')
    parser.add_argument('--trials', type=int, default=5)
    parser.add_argument('--output', type=Path, help='Write JSON evidence here; default stdout')
    args = parser.parse_args()
    if args.trials < 1:
        parser.error('--trials must be positive')
    modules = Path(videorebuild.__file__).resolve().parent
    evidence = {'python': sys.version, 'platform': platform.platform(),
        'source_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(modules.glob('*.py'))},
        'scenarios': {name: SCENARIOS[name](args.trials)
                      for name in dict.fromkeys(args.scenario or SCENARIOS)}}
    rendered = json.dumps(evidence, indent=2) + '\n'
    if args.output:
        args.output.write_text(rendered, encoding='utf-8')
    else:
        print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
