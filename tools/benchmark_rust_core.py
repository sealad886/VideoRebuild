#!/usr/bin/env python3
"""Synthetic CPU, I/O and application-cache benchmarks for the Rust migration.

Run with PYTHONPATH selecting the revision under test. Every scenario runs in
its own child process; temporary source/case/output files are generated locally.
Cold means a fresh application index, never a flushed operating-system cache.
No private recordings, active cases, decoding, or external volumes are used.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import cProfile
import hashlib
import json
from pathlib import Path
import platform
import resource
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import tracemalloc
from types import SimpleNamespace

import videorebuild
from videorebuild import engine, structural_index
from videorebuild.bmff import Chunk, make_box, parse_movie
from videorebuild.constraints import prune_domains
from videorebuild.storage import Case, ReaderPool, source_identity


class Task:
    def update(self, *args, **kwargs):
        pass

    def heartbeat(self, *args, **kwargs):
        pass

    def advance(self, *args, **kwargs):
        pass


class CountedReader(ReaderPool):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read_calls = 0
        self.read_bytes = 0

    def read(self, sid, offset, count):
        self.read_calls += 1
        self.read_bytes += count
        return super().read(sid, offset, count)


def synthetic_movie(count, size, offset):
    """One AVC track/chunk with explicit synthetic encoded sample boundaries."""
    return chunked_movie([([size] * count, offset)])


def chunked_movie(chunks):
    """Build supported synthetic sample tables; no expected content is supplied."""
    def integers(*values):
        return struct.pack('>' + 'I' * len(values), *values)

    def full(kind, body):
        return make_box(kind, bytes(4) + body)

    entry = make_box(b'avc1', bytes(6) + b'\0\1' + bytes(70) +
                     make_box(b'avcC', b'\1\x42\0\x1e\xff'))
    sizes = [size for sizes, _ in chunks for size in sizes]
    count = len(sizes)
    table = make_box(b'stbl', full(b'stsd', integers(1) + entry) +
                     full(b'stsz', integers(0, count, *sizes)) +
                     full(b'stsc', integers(len(chunks), *(value for index, (sizes, _) in
                          enumerate(chunks) for value in (index + 1, len(sizes), 1)))) +
                     full(b'stco', integers(len(chunks), *(offset for _, offset in chunks))) +
                     full(b'stts', integers(1, count, 1)))
    references = full(b'dref', integers(1) + make_box(b'url ', b'\0\0\0\1'))
    media = make_box(b'mdia', full(b'mdhd', integers(0, 0, 1000, count)) +
                     full(b'hdlr', integers(0) + b'vide') +
                     make_box(b'minf', make_box(b'dinf', references) + table))
    track = make_box(b'trak', full(b'tkhd', integers(0, 0, 1, 0)) + media)
    return parse_movie(make_box(b'moov', full(b'mvhd', bytes(20)) + track))


def sample(size):
    return struct.pack('>I', size - 4) + b'\x65' + b'X' * (size - 5)


@contextmanager
def workload(name, root):
    if name == 'ffi-crossings':
        from videorebuild import _native
        count = 100_000
        token = sample(8)
        raw, sizes = token * count, (8,) * count

        def run():
            started = time.perf_counter()
            if sum(_native.sample_kind(token, b'avc1', 4) == 'vcl'
                   for _ in range(count)) != count:
                raise AssertionError('single-sample native classification changed')
            per_sample = time.perf_counter() - started
            started = time.perf_counter()
            validator = _native.SampleValidator(sizes, b'avc1', 4)
            if validator.validate_window(raw) != (count, len(raw), True):
                raise AssertionError('batched native classification changed')
            batched = time.perf_counter() - started
            return dict(samples=count, input_bytes=len(raw), crossings=count,
                        per_sample_seconds=per_sample, batch_seconds=batched,
                        batch_includes_metadata_transfer=True)
        yield run
        return
    if name.startswith('samples-'):
        size, count = (8, 100_000) if name.endswith('small') else (1024, 16_384)
        path = root / 'samples.bin'
        path.write_bytes(sample(size) * count)
        row = source_identity(path)
        movie = SimpleNamespace(chunks=[Chunk(0, 0, 0, (size,) * count, 0)],
            tracks=[SimpleNamespace(handler=b'vide', codec=b'avc1', nal_length=4)])

        def run():
            with CountedReader({1: row}) as pool:
                if not engine.check_samples(movie, {(0, 0): (1, 0)}, pool):
                    raise AssertionError('full validation rejected valid sample framing')
                return dict(samples=count, input_bytes=count * size,
                            read_calls=pool.read_calls, read_bytes=pool.read_bytes)
        yield run
        return
    if name.startswith('discovery-'):
        workers = int(name.rsplit('-', 1)[1])
        dense = '-dense-' in name
        raw = sample(8) * 4000 if dense else (sample(8) + b'X' * 4088) * 1024
        rows = {}
        for sid in range(1, 5):
            path = root / f'source-{sid}.bin'
            path.write_bytes(raw)
            rows[sid] = source_identity(path)
        # Finder metadata deliberately lies beside sources; explicit fixture
        # inventory never treats it as evidence or includes it in byte totals.
        (root / '.DS_Store').write_bytes(b'Finder metadata')
        maximum = 252 if dense else 4
        trial = 0

        def run():
            nonlocal trial
            trial += 1
            case = SimpleNamespace(root=root / f'case-{trial}')
            case.root.mkdir()
            result = {}
            with ReaderPool(rows) as pool:
                for phase in ('cold', 'warm'):
                    start, cpu = time.perf_counter(), time.process_time()
                    with structural_index.postings(case, pool, [maximum],
                            engine._anchor_locations, engine.Options(search_workers=workers),
                            Task()) as (db, stats):
                        locations = db.execute('SELECT length,sid,start FROM locations ORDER BY length,sid,start').fetchall()
                        expected = [(maximum, sid, index * (8 if dense else 4096))
                                    for sid in rows for index in range(3969 if dense else 1024)]
                        if locations != expected:
                            raise AssertionError('discovery changed exact structural posting set')
                        location_digest = hashlib.sha256(b''.join(struct.pack('>QQQ', *location)
                            for location in locations)).hexdigest()
                        result[phase] = dict(seconds=time.perf_counter() - start,
                            cpu_seconds=time.process_time() - cpu, locations=len(locations),
                            locations_sha256=location_digest,
                            counters={key: value for key, value in stats.items()
                                      if key != 'discovery_index'})
                if result['warm']['counters']['nal_checks']:
                    raise AssertionError('warm coverage rescanned source bytes')
            return dict(workers=workers, source_bytes=len(raw) * len(rows), **result)
        yield run
        return
    if name == 'constraints':
        domains = [[((g, c), ((1, 1, c * 8, c * 8 + 8),)) for c in range(120)]
                   for g in range(20)]

        def run():
            pruned, stats = prune_domains(domains)
            if pruned != domains:
                raise AssertionError('support pruning changed feasible symmetric domains')
            return dict(groups=20, candidates=2400, counters=stats)
        yield run
        return
    if name == 'scattered-operation':
        tables = [([320 + 13 * group] * 128, 1_000_000 + group * 100_000)
                 for group in range(8)]
        movie = chunked_movie(tables)
        chunks = [sample(sizes[0]) * len(sizes) for sizes, _ in tables]
        digest = hashlib.sha256(b''.join(chunks)).hexdigest()
        header = root / 'header.bin'
        header.write_bytes(movie.data)
        source = root / 'scattered.bin'
        source.write_bytes(b'Z' * 123 + (b'Q' * 137).join(reversed(chunks)))
        sequence = 0

        def run():
            nonlocal sequence
            sequence += 1
            with Case(root / f'case-{sequence}') as case:
                from videorebuild.storage import scan_logical
                scan_logical(case, case.register_source(header))
                scan_logical(case, case.register_source(source))
                result = {}
                for phase in ('cold', 'warm'):
                    start, cpu = time.perf_counter(), time.process_time()
                    engine.recover(case, engine.Options(reconstruct_only=True, deep=True))
                    result[phase] = dict(seconds=time.perf_counter() - start,
                                         cpu_seconds=time.process_time() - cpu)
                    job = case.job('ordinary', movie.digest)
                    candidates = job['detail'].get('candidates', [])
                    if job['status'] != 'reconstructed' or len(candidates) != 1:
                        raise AssertionError(f'scattered recovery changed candidate set: {job}')
                    if candidates[0]['media_sha256'] != digest:
                        raise AssertionError('scattered recovery changed encoded bytes')
                    result[phase]['counters'] = {key: value for key, value in job['detail'].items()
                        if isinstance(value, (int, float, bool)) or key == 'discovery'}
                return dict(samples=1024, groups=8, media_bytes=sum(map(len, chunks)),
                            media_sha256=digest, candidate_count=1, **result)
        yield run
        return
    if name in {'reconstruct', 'whole-operation'}:
        # Reconstruction is dominated by a few large samples; whole-operation
        # uses many small samples to expose complete-validation CPU overhead.
        size, count = (1024 * 1024, 64) if name == 'reconstruct' else (64, 100_000)
        media = sample(size) * count
        digest = hashlib.sha256(media).hexdigest()
        movie = synthetic_movie(count, size, 8)
        source = root / 'payload.bin'
        source.write_bytes(make_box(b'mdat', media))
        row = source_identity(source)
        row['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        sequence = 0

        def run():
            nonlocal sequence
            sequence += 1
            if name == 'reconstruct':
                output = root / f'reconstructed-{sequence}.mp4'
                with CountedReader({1: row}) as pool:
                    evidence = engine.reconstruct(movie, {(0, 0): (1, 8)}, pool, output, 128 * 1024**2)
                    reads = dict(read_calls=pool.read_calls, read_bytes=pool.read_bytes)
                if evidence['media_sha256'] != digest:
                    raise AssertionError('reconstruction changed encoded bytes')
                # Include unlink in measured operation to bound disk growth.
                output.unlink()
                return dict(samples=count, media_bytes=len(media), media_sha256=digest, **reads)
            with Case(root / f'case-{sequence}') as case:
                sid = case.register_source(source)
                case.db.execute('UPDATE sources SET complete=1 WHERE id=?', (sid,))
                case.db.execute('''INSERT INTO atoms(source_id,kind,offset,size,header,available,payload_size)
                                  VALUES(?,'mdat',0,?,8,?,?)''',
                                (sid, 8 + len(media), 8 + len(media), len(media)))
                case.db.commit()
                with CountedReader(case.sources()) as pool:
                    status, candidates, limited = engine.whole_payload(case, movie, pool, {},
                        engine.Options(reconstruct_only=True), 'benchmark')
                    reads = dict(read_calls=pool.read_calls, read_bytes=pool.read_bytes)
                if status != 'reconstructed' or limited or len(candidates) != 1:
                    raise AssertionError('whole-operation changed result set')
                if candidates[0]['media_sha256'] != digest:
                    raise AssertionError('whole-operation changed encoded bytes')
                return dict(samples=count, media_bytes=len(media), candidate_count=1,
                            media_sha256=digest, **reads)
        yield run
        return
    raise ValueError(name)


SCENARIOS = ('samples-small', 'samples-large', 'discovery-dense-1',
             'discovery-dense-2', 'discovery-dense-4', 'discovery-sparse-1',
             'discovery-sparse-2', 'discovery-sparse-4', 'constraints',
             'reconstruct', 'whole-operation', 'scattered-operation')


def child(args):
    with tempfile.TemporaryDirectory(prefix='videorebuild-rust-benchmark-') as directory:
        with workload(args.worker, Path(directory)) as run:
            timings, cpus, outputs = [], [], []
            usage_before = resource.getrusage(resource.RUSAGE_SELF)
            for _ in range(args.trials):
                start, cpu = time.perf_counter(), time.process_time()
                outputs.append(run())
                timings.append(time.perf_counter() - start)
                cpus.append(time.process_time() - cpu)
            # Separate allocation run: tracemalloc overhead never enters timing.
            tracemalloc.start()
            run()
            _, traced_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            if args.profile_dir:
                args.profile_dir.mkdir(parents=True, exist_ok=True)
                profiler = cProfile.Profile()
                profiler.runcall(run)
                profiler.dump_stats(args.profile_dir / f'{args.worker}.prof')
            usage = resource.getrusage(resource.RUSAGE_SELF)
    # Student t approximation, two-sided 95%, df=trials-1 (large df -> 1.96).
    t95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
           6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}.get(args.trials - 1, 1.96)
    half = (t95 * statistics.stdev(timings) / args.trials**0.5
            if args.trials > 1 else None)
    return dict(seconds=timings, median_seconds=statistics.median(timings),
        mean_seconds=statistics.mean(timings), mean_95ci_half_seconds=half,
        cpu_seconds=cpus, peak_rss_bytes=usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
        peak_python_traced_bytes=traced_peak,
        input_block_operations=usage.ru_inblock - usage_before.ru_inblock,
        output_block_operations=usage.ru_oublock - usage_before.ru_oublock,
        observations=outputs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', action='append', choices=SCENARIOS + ('ffi-crossings',))
    parser.add_argument('--trials', type=int, default=5)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile-dir', type=Path)
    parser.add_argument('--compare', type=Path,
                        help='Check output-set equivalence and report ratios against prior JSON')
    parser.add_argument('--worker', choices=SCENARIOS + ('ffi-crossings',), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error('--trials must be positive')
    if args.worker:
        print(json.dumps(child(args)))
        return 0
    modules = Path(videorebuild.__file__).resolve().parent
    evidence = dict(schema=1, python=sys.version, platform=platform.platform(),
        version=videorebuild.__version__, scope='synthetic; cold application indexes, warm OS caches',
        source_sha256={str(path.relative_to(modules)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in sorted(modules.rglob('*'))
                       if path.is_file() and path.suffix in {'.py', '.so'} and path.name != '.DS_Store'},
        scenarios={})
    for name in dict.fromkeys(args.scenario or SCENARIOS):
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', name,
                   '--trials', str(args.trials)]
        if args.profile_dir:
            command += ['--profile-dir', str(args.profile_dir.resolve())]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if result.returncode:
            print(result.stderr, file=sys.stderr)
            result.check_returncode()
        evidence['scenarios'][name] = json.loads(result.stdout)
        print(f'{name}: {evidence["scenarios"][name]["median_seconds"]:.6f}s', file=sys.stderr)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
    rendered = json.dumps(evidence, indent=2) + '\n'
    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding='utf-8'))
        comparisons = {}
        for name, after in evidence['scenarios'].items():
            if name not in baseline['scenarios']:
                continue
            before = baseline['scenarios'][name]
            old, new = before['observations'][0], after['observations'][0]
            for key in ('samples', 'media_bytes', 'media_sha256', 'candidate_count', 'source_bytes'):
                if old.get(key) != new.get(key):
                    raise AssertionError(f'{name}: output equivalence changed for {key}')
            for phase in ('cold', 'warm'):
                if phase in old:
                    for key in ('locations', 'locations_sha256'):
                        if old[phase].get(key) != new[phase].get(key):
                            raise AssertionError(f'{name}: exact {phase} posting-set equivalence changed')
            comparisons[name] = dict(equivalent=True,
                speedup=before['median_seconds'] / after['median_seconds'],
                rss_ratio=after['peak_rss_bytes'] / before['peak_rss_bytes'],
                traced_allocation_ratio=after['peak_python_traced_bytes'] / before['peak_python_traced_bytes'])
        evidence['comparison'] = comparisons
        evidence['compared_with'] = str(args.compare)
        rendered = json.dumps(evidence, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding='utf-8')
    else:
        print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
