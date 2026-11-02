"""Exact structural joins, durable coverage, and source-boundary regressions."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_deep_witnesses import structural_movie
from videorebuild import engine, structural_index
from videorebuild.bmff import Chunk, FormatError, valid_sample
from videorebuild.ranges import AllocationScope, Extent
from videorebuild.storage import Case, ReaderPool, SafetyError, scan_logical


class Task:
    def update(self, *args, **kwargs):
        pass

    def heartbeat(self, **kwargs):
        pass

    def advance(self, *args, **kwargs):
        pass


def source_row(path, kind='file'):
    stat = path.stat()
    return dict(path=str(path), kind=kind, size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                dev=stat.st_dev, ino=stat.st_ino, sha256=hashlib.sha256(path.read_bytes()).hexdigest())


class StructuralIndexTests(unittest.TestCase):
    def test_worker_count_change_resumes_completed_job_with_live_proof_checks(self):
        from videorebuild.bmff import make_box
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prototype = structural_movie([0], [8])
            movie = structural_movie([len(prototype.data)+8], [8])
            source = root/'recording.bin'
            source.write_bytes(movie.data+make_box(b'mdat', b'\0\0\0\x04\x65ABC'))
            with Case(root/'case') as case:
                scan_logical(case, case.register_source(source))
                engine.recover(case, engine.Options(reconstruct_only=True, search_workers=1))
                first = case.job('ordinary', movie.digest)
                self.assertEqual(first['status'], 'reconstructed')
                with patch.object(engine, 'whole_payload', side_effect=AssertionError('completed job reprocessed')):
                    engine.recover(case, engine.Options(reconstruct_only=True, search_workers=2))
                resumed = case.job('ordinary', movie.digest)
                self.assertEqual(resumed['detail']['run_key'], first['detail']['run_key'])
                started = json.loads(case.db.execute("SELECT detail FROM events WHERE kind='recovery_started' ORDER BY id DESC LIMIT 1").fetchone()[0])
                self.assertEqual(started['options']['search_workers'], 2)
                output = case.root/resumed['detail']['outputs'][0]
                original = output.read_bytes()
                output.write_bytes(original+b'changed publication')
                with self.assertRaises(SafetyError):
                    engine.recover(case, engine.Options(reconstruct_only=True, search_workers=2))
                output.write_bytes(original)
                source.write_bytes(source.read_bytes()+b'changed source')
                with self.assertRaises(SafetyError):
                    engine.recover(case, engine.Options(reconstruct_only=True, search_workers=2))

    def test_reader_close_releases_all_handles_when_matching_context_close_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = {}
            for sid in (1, 2):
                path = root/f'{sid}.bin'
                path.write_bytes(b'synthetic source')
                rows[sid] = source_row(path)
            pool = ReaderPool(rows)
            handles = [pool.handle(sid) for sid in rows]
            def failed_final_check():
                raise SafetyError('synthetic final source check failure')
            pool._matching_contexts.callback(failed_final_check)
            try:
                with self.assertRaisesRegex(SafetyError, 'synthetic final source check failure'):
                    pool.close()
                self.assertTrue(all(handle.closed for handle in handles))
                self.assertFalse(pool.handles)
            finally:
                pool.close()

    def test_avc_sample_at_allocation_boundary_is_discovered_and_reconstructed(self):
        sample = bytes.fromhex('00000001610000000161')
        self.assertTrue(valid_sample(sample, b'avc1', 4))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'image.bin'
            path.write_bytes(sample+b'X')
            with Case(root/'case') as case:
                sid = case.register_source(path, 'image')
                rows = {sid: dict(case.db.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone())}
                scope = AllocationScope({sid: ((0, len(sample)),)}, 'fixture-boundary')
                movie = structural_movie([100], [len(sample)])
                with ReaderPool(rows, allocation_scope=scope) as pool:
                    engine.deep_search(case, [movie], pool, {}, engine.Options(reconstruct_only=True), 'boundary')
                job = case.job('ordinary', movie.digest)
                self.assertEqual(job['status'], 'reconstructed')
                self.assertEqual(job['detail']['candidates'][0]['media_sha256'], hashlib.sha256(sample).hexdigest())
                self.assertFalse(job['detail']['decode_checked'])
                self.assertEqual(path.read_bytes(), sample+b'X')

    def test_cold_warm_and_new_length_coverage(self):
        token8 = b'\0\0\0\x04\x65ABC'
        token10 = b'\0\0\0\x06\x65ABCDE'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'tokens.bin'
            path.write_bytes(token8+token10)
            case = SimpleNamespace(root=root/'case')
            case.root.mkdir()
            with ReaderPool({1: source_row(path)}) as pool:
                with structural_index.postings(case, pool, [4], engine._anchor_locations, engine.Options(), Task()) as (db, first):
                    index_path = first['discovery_index']
                    self.assertEqual(first['discovery_sources_scanned'], 1)
                    self.assertEqual(db.execute('SELECT sid,start FROM locations WHERE length=4').fetchall(), [(1, 0)])
                with patch.object(pool, 'iter_blocks', side_effect=AssertionError('warm scan')):
                    with structural_index.postings(case, pool, [4], engine._anchor_locations, engine.Options(), Task()) as (_, warm):
                        self.assertEqual(warm['discovery_sources_reused'], 1)
                        self.assertEqual(warm['nal_checks'], 0)
                with structural_index.postings(case, pool, [4, 6], engine._anchor_locations, engine.Options(), Task()) as (db, extended):
                    self.assertEqual(extended['discovery_index'], index_path)
                    self.assertEqual(db.execute('SELECT count(*) FROM coverage').fetchone()[0], 2)
                    self.assertEqual(db.execute('SELECT sid,start FROM locations WHERE length=6').fetchall(), [(1, 8)])

    def test_interrupted_source_has_no_negative_receipt_and_completed_source_is_reused(self):
        token = b'\0\0\0\x04\x65ABC'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = SimpleNamespace(root=root/'case')
            case.root.mkdir()
            rows = {}
            for sid in (1, 2):
                path = root/f'{sid}.bin'
                path.write_bytes(token)
                rows[sid] = source_row(path)
            def interrupted(pool, maximum, wanted, opt, state, task, *, source_ids):
                if source_ids == (2,):
                    raise InterruptedError('synthetic interrupted source')
                yield from engine._anchor_locations(pool, maximum, wanted, opt, state, task, source_ids=source_ids)
            with ReaderPool(rows) as pool:
                with self.assertRaises(InterruptedError):
                    with structural_index.postings(case, pool, [4], interrupted, engine.Options(), Task()):
                        self.fail('incomplete index must not be yielded')
                with patch.object(pool, 'iter_blocks', wraps=pool.iter_blocks) as scans:
                    with structural_index.postings(case, pool, [4], engine._anchor_locations, engine.Options(), Task()) as (db, resumed):
                        self.assertEqual(db.execute('SELECT count(*) FROM coverage').fetchone()[0], 2)
                        self.assertEqual(resumed['discovery_sources_reused'], 1)
                        self.assertEqual([call.args[0] for call in scans.call_args_list], [2])

    def test_zero_hit_sources_are_covered_but_still_identity_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'zero.bin'
            path.write_bytes(bytes(32))
            case = SimpleNamespace(root=root/'case')
            case.root.mkdir()
            with ReaderPool({1: source_row(path)}) as pool:
                with structural_index.postings(case, pool, [4], engine._anchor_locations, engine.Options(), Task()) as (db, _):
                    self.assertEqual(db.execute('SELECT count(*) FROM coverage').fetchone()[0], 1)
                    self.assertEqual(db.execute('SELECT count(*) FROM locations').fetchone()[0], 0)
                path.write_bytes(b'changed source')
                with self.assertRaises(SafetyError):
                    with structural_index.postings(case, pool, [4], engine._anchor_locations, engine.Options(), Task()):
                        self.fail('changed zero-hit source must not validate a cache')

    def test_parsed_mapping_and_parser_revision_change_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'physical.bin'
            path.write_bytes(b'abcdefghABCDEFGH')
            logical = dict(kind='logical', size=8, extents=[Extent(0, 1, 0, 8)])
            with ReaderPool({2: logical}, physical_sources={1: source_row(path)}) as pool:
                first = structural_index._identity(pool)
                pool.extents[2] = (Extent(0, 1, 8, 8),)
                self.assertNotEqual(first, structural_index._identity(pool))
                second = structural_index._identity(pool)
                with patch.object(structural_index, 'PARSER_REVISION', 100):
                    self.assertNotEqual(second, structural_index._identity(pool))

    def test_symlink_cache_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'tokens.bin'
            path.write_bytes(b'\0\0\0\x04\x65ABC')
            (root/'case').mkdir()
            (root/'case'/'work').symlink_to(root, target_is_directory=True)
            with ReaderPool({1: source_row(path)}) as pool, self.assertRaises(SafetyError):
                with structural_index.postings(SimpleNamespace(root=root/'case'), pool, [4],
                                               engine._anchor_locations, engine.Options(), Task()):
                    self.fail('unsafe path must not be opened')

    def test_compact_warm_translation_preserves_canonical_evidence_without_digest_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'negative.bin'
            source.write_bytes(bytes(800))
            movie = structural_movie(list(range(0, 800, 8)), [8]*100)
            with Case(root/'case') as case:
                sid = case.register_source(source)
                rows = {sid: dict(case.db.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone())}
                with ReaderPool(rows) as pool:
                    mapping = engine._TranslationPlan(movie).mapping(sid, 0)
                    with patch.object(engine, 'mapping_digest', side_effect=AssertionError('expanded cold negative')):
                        first = engine.attempt(case, movie, mapping, pool, {}, engine.Options(reconstruct_only=True), 'whole_mdat')
                    self.assertEqual(first['mapping_kind'], 'translated_negative')
                    self.assertEqual(case._matching_work['translated_canonical_rows'], 0)
                    with patch.object(engine, 'mapping_digest', side_effect=AssertionError('expanded warm mapping')):
                        self.assertEqual(first, engine.attempt(case, movie, mapping, pool, {}, engine.Options(reconstruct_only=True), 'whole_mdat'))

    def test_rarest_intersection_reduces_validations_and_preserves_match(self):
        small = b'\0\0\0\x04\x65ABC'
        large = b'\0\0\0\x06\x65ABCDE'
        movie = SimpleNamespace(digest='compound', data=b'',
                                tracks=[SimpleNamespace(handler=b'vide', codec=b'avc1', nal_length=4)],
                                chunks=[Chunk(0, 0, 100, (8, 10, 8), 0)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'tokens.bin'
            path.write_bytes(small*100+small+large+small)
            counts = []
            mappings = []
            for indexed in (False, True):
                case = SimpleNamespace(root=root/str(indexed))
                case.root.mkdir()
                with ReaderPool({1: source_row(path)}) as pool, \
                        patch.object(engine, 'check_samples', wraps=engine.check_samples) as checks, \
                        patch.object(engine, 'attempt', return_value={'status': 'rejected', 'content_evaluated': True}) as attempts, \
                        patch.object(engine, 'finish'):
                    if indexed:
                        engine.deep_search(case, [movie], pool, {}, engine.Options(), 'indexed')
                    else:
                        engine._deep_batch(case, [movie], pool, {}, engine.Options(), 'or', set())
                    counts.append(checks.call_count)
                    mappings.append({tuple(sorted(call.args[2].items())) for call in attempts.call_args_list})
            self.assertEqual(mappings[0], mappings[1])
            self.assertEqual(len(mappings[1]), 1)
            self.assertLess(counts[1]*10, counts[0])

    def test_small_random_indexed_results_equal_original_or_results(self):
        rng = random.Random(1947)
        tokens = [b'\0\0\0\x04\x65ABC', b'\0\0\0\x04\x65DEF', b'\0\0\0\x06\x65ABCDE']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for trial in range(12):
                chosen = [rng.choice(tokens) for _ in range(4)]
                data = b'ZZ'.join(chosen)
                path = root/f'{trial}.bin'
                path.write_bytes(data)
                sizes = [len(rng.choice(chosen)) for _ in range(2)]
                movie = structural_movie([100, 200], sizes)
                results = []
                for indexed in (False, True):
                    with Case(root/f'case-{trial}-{indexed}') as case, ReaderPool({1: source_row(path)}) as pool:
                        if indexed:
                            engine.deep_search(case, [movie], pool, {}, engine.Options(reconstruct_only=True), 'indexed')
                        else:
                            engine._deep_batch(case, [movie], pool, {}, engine.Options(reconstruct_only=True), 'or', set())
                        job = case.job('ordinary', movie.digest)
                        results.append({c['media_sha256'] for c in job['detail']['candidates']})
                self.assertEqual(results[0], results[1], (trial, chosen, sizes))

    def test_whole_payload_point_join_preserves_candidates(self):
        token = b'\0\0\0\x04\x65ABC'
        movie = structural_movie([100], [8])
        from videorebuild.bmff import make_box
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.bin'
            source.write_bytes(movie.data+make_box(b'mdat', token)+make_box(b'mdat', bytes(8))*50)
            outputs = []
            for indexed in (False, True):
                with Case(root/str(indexed)) as case:
                    sid = case.register_source(source)
                    scan_logical(case, sid)
                    with ReaderPool(case.sources()) as pool:
                        if indexed:
                            with structural_index.postings(case, pool, [4], engine._anchor_locations, engine.Options(), Task()) as (db, _):
                                pool.structural_discovery = db
                                _, candidates, _ = engine.whole_payload(case, movie, pool, {}, engine.Options(reconstruct_only=True), 'index')
                        else:
                            _, candidates, _ = engine.whole_payload(case, movie, pool, {}, engine.Options(reconstruct_only=True), 'or')
                        outputs.append({c['media_sha256'] for c in candidates})
                        if indexed:
                            self.assertEqual(case.job('ordinary', movie.digest)['detail']['tested_hypotheses'], 2)
            self.assertEqual(outputs[0], outputs[1])

    def test_parallel_index_matches_serial_and_receipts_stay_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = {}
            for sid in range(1, 5):
                path = root/f'{sid}.bin'
                path.write_bytes(b'\0\0\0\x04\x65ABC'*3)
                rows[sid] = source_row(path)
            outputs = []
            for workers in (1, 3):
                case = SimpleNamespace(root=root/f'case-{workers}')
                case.root.mkdir()
                with ReaderPool(rows) as pool, structural_index.postings(
                        case, pool, [4, 12, 20], engine._anchor_locations,
                        engine.Options(search_workers=workers), Task()) as (db, stats):
                    outputs.append(db.execute('SELECT * FROM locations ORDER BY length,sid,start').fetchall())
                    self.assertEqual(db.execute('SELECT count(*) FROM targets').fetchone()[0], 1)
                    self.assertEqual(db.execute('SELECT count(*) FROM coverage').fetchone()[0], len(rows))
                    self.assertEqual(stats['discovery_sources_scanned'], len(rows))
            self.assertEqual(outputs[0], outputs[1])

    def test_fork_has_private_handles_and_preserves_acquisition_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'image.bin'
            path.write_bytes(b'0123456789ABCDEF')
            rescue = root/'rescue.map'
            rescue.write_text('0 + 1\n0 16 +\n')
            row = source_row(path, 'image')
            row['binding'] = dict(mapfile=source_row(rescue), image_offset=0)
            with ReaderPool({1: row}) as parent:
                self.assertEqual(parent.read(1, 0, 4), b'0123')
                with parent.fork((1,)) as child:
                    self.assertEqual(child._map_identities, parent._map_identities)
                    self.assertEqual(child.read(1, 8, 4), b'89AB')
                    self.assertIsNot(parent.handle(1), child.handle(1))
                    self.assertEqual(parent.handle(1).tell(), 4)
                    rescue.write_text('0 + 1\n0 15 +\n15 1 -\n')
                    with self.assertRaises(SafetyError):
                        child.read(1, 0, 1)

    def test_constraint_prepass_retains_alternative_after_scope_excludes_hit(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE TABLE hits(movie,g,fingerprint,sid,start)')
            db.executemany('INSERT INTO hits VALUES(?,?,?,?,?)', [('scope', 0, 'A', 1, 0),
                           ('scope', 0, 'A', 1, 32), ('scope', 1, 'B', 1, 64)])
            groups = [SimpleNamespace(key=('scope', i), start=0,
                                      chunks=[Chunk(0, i, 0, (8,), 0)]) for i in range(2)]
            def provenance(sid, start, size):
                if start == 0:
                    raise FormatError('recovered_reference_second_tier')
                return [dict(source_id=sid, source_offset=start, length=size)]
            pool = SimpleNamespace(check=lambda sid: None, physical_key=lambda sid: (1, 1), provenance=provenance)
            stats = dict(partial_assignments_checked=0, partial_assignments_rejected=0,
                         content_prefixes_checked=0, content_prefixes_rejected=0, content_tuples_considered=0)
            contents = engine._content_assemblies(db, groups, pool, stats, Task())
            try:
                self.assertEqual(next(next(contents)), ((1, 32), (1, 64)))
            finally:
                contents.close()

    def test_recovery_scope_identity_and_coverage_metadata_load_once(self):
        targets = [structural_movie([100+i, 200+i], [8, 8]) for i in range(8)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'catalogue.bin'
            source.write_bytes(b''.join(movie.data for movie in targets)+b'\0\0\0\x04\x65ABC'*2)
            with Case(root/'case') as case:
                scan_logical(case, case.register_source(source))
                for i in range(2):
                    extra = root/f'zero-{i}.bin'
                    extra.write_bytes(bytes(32))
                    scan_logical(case, case.register_source(extra))
                with patch.object(structural_index, '_identity', wraps=structural_index._identity) as identities, \
                        patch.object(structural_index, '_common_coverage', wraps=structural_index._common_coverage) as coverages:
                    engine.recover(case, engine.Options(deep=True, reconstruct_only=True, max_groups=1))
                    self.assertEqual(identities.call_count, 1)
                    self.assertLessEqual(coverages.call_count, 2)


if __name__ == '__main__':
    unittest.main()
