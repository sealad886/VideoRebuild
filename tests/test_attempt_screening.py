"""Sparse negative screening retains source integrity and accepted-output gates."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_deep_witnesses import structural_movie
from videorebuild import engine
from videorebuild.bmff import FormatError
from videorebuild.storage import Case, ReaderPool, SafetyError
from videorebuild.ranges import load_allocation_scope


class AttemptScreeningTests(unittest.TestCase):
    def test_negative_screen_is_durable_without_per_chunk_identity_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'invalid.bin'
            source.write_bytes(b'\0' * (256 * 8))
            movie = structural_movie([i * 8 for i in range(256)], [8] * 256)
            with Case(root / 'case') as case:
                sid = case.register_source(source)
                with ReaderPool({row['id']: dict(row) for row in case.db.execute('SELECT * FROM sources')}) as pool:
                    mapping = {(c.track, c.index): (sid, c.offset) for c in movie.chunks}
                    with patch.object(pool, '_check_identity', wraps=pool._check_identity) as identities, \
                            patch.object(pool, 'provenance', side_effect=AssertionError('negative needs no full proof')):
                        cold = engine.attempt(case, movie, mapping, pool, {}, engine.Options(reconstruct_only=True), 'whole_mdat')
                        self.assertLess(identities.call_count, 20)
                        identities.reset_mock()
                        warm = engine.attempt(case, movie, mapping, pool, {}, engine.Options(reconstruct_only=True), 'whole_mdat')
                        self.assertLess(identities.call_count, 20)
                    self.assertEqual(cold, warm)
                    self.assertTrue(cold['negative_screen'])
                    self.assertEqual(cold['range_validation'], 'not_evaluated')
                    persisted = json.loads(case.db.execute('SELECT detail FROM attempts').fetchone()[0])
                    self.assertEqual(persisted, cold)
                    self.assertEqual(list((case.root / 'candidates').iterdir()), [])

    def test_existing_rejected_attempt_is_promoted_by_sparse_byte_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'invalid.bin'
            source.write_bytes(b'\0' * (256 * 8))
            movie = structural_movie([i * 8 for i in range(256)], [8] * 256)
            with Case(root / 'case') as case:
                sid = case.register_source(source)
                rows = {sid: dict(case.db.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone())}
                mapping = {(c.track, c.index): (sid, c.offset) for c in movie.chunks}
                old = dict(status='rejected', reason='NAL_sample_boundaries_do_not_match', tools={}, timeout=300.0,
                           validation_revision=engine.VALIDATION_REVISION, reconstruct_only=True)
                md = engine.mapping_digest(movie, mapping)
                case.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)',
                                (1, movie.digest, md, 'whole_mdat', 'rejected', json.dumps(old)))
                case.db.commit()
                with ReaderPool(rows) as pool, \
                        patch.object(pool, '_check_identity', wraps=pool._check_identity) as identities, \
                        patch.object(pool, 'provenance', side_effect=AssertionError('no full chunk validation')):
                    result = engine.attempt(case, movie, mapping, pool, {}, engine.Options(reconstruct_only=True), 'whole_mdat')
                    self.assertLess(identities.call_count, 20)
                self.assertTrue(result['negative_screen'])
                self.assertEqual(json.loads(case.db.execute('SELECT detail FROM attempts').fetchone()[0]), result)

    def test_negative_cache_still_checks_unscreened_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = [root / name for name in ('negative.bin', 'other.bin')]
            for source in files:
                source.write_bytes(b'\0' * 104)
            movie = structural_movie([i * 8 for i in range(13)], [8] * 13)
            with Case(root / 'case') as case:
                ids = [case.register_source(source) for source in files]
                with ReaderPool({row['id']: dict(row) for row in case.db.execute('SELECT * FROM sources')}) as pool:
                    mapping = {(c.track, c.index): (ids[c.index == 4], c.offset) for c in movie.chunks}
                    engine.attempt(case, movie, mapping, pool, {}, engine.Options(), 'whole_mdat')
                    files[1].write_bytes(b'x' * 105)
                    with self.assertRaises(SafetyError):
                        engine.attempt(case, movie, mapping, pool, {}, engine.Options(), 'whole_mdat')

    def test_scope_expansion_reconsiders_unreadable_sample_without_stale_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'image.bin'
            data = b'\0\0\0\x04\x65ABC' + b'Z' * 8
            source.write_bytes(data)
            movie = structural_movie([0], [8])
            with Case(root / 'case') as case:
                sid = case.register_source(source, 'image')
                row = dict(case.db.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone())
                row['sha256'] = hashlib.sha256(data).hexdigest()
                sources = {sid: row}
                policy = root / 'allocation.json'
                for ranges, expected in (([[8, 8]], 'rejected'), ([[0, 16]], 'valid'), ([[8, 8]], 'rejected')):
                    policy.write_text(json.dumps(dict(schema=1, kind='filesystem-unallocated', images=[
                        dict(path=str(source), size=len(data), sha256=row['sha256'], unallocated_ranges=ranges)])))
                    scope = load_allocation_scope(policy, sources)
                    with ReaderPool(sources, allocation_scope=scope) as pool:
                        result = engine.attempt(case, movie, {(0, 0): (sid, 0)}, pool, {},
                                                engine.Options(reconstruct_only=True), 'whole_mdat')
                    self.assertEqual(result['status'], expected)
                    self.assertFalse(result.get('negative_screen', False))
                    if expected == 'rejected' and not case.db.execute('SELECT 1 FROM attempts').fetchone():
                        self.assertEqual(list((case.root / 'candidates').iterdir()), [])
                self.assertEqual(json.loads(case.db.execute('SELECT detail FROM attempts').fetchone()[0])['status'], 'valid')

    def test_surviving_quick_screen_requires_full_range_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'tokens.bin'
            source.write_bytes(b'\0\0\0\x04\x65ABC' * 2)
            movie = structural_movie([0, 8], [8, 8])
            with Case(root / 'case') as case:
                sid = case.register_source(source)
                with ReaderPool({row['id']: dict(row) for row in case.db.execute('SELECT * FROM sources')}) as pool:
                    mapping = {(c.track, c.index): (sid, c.offset) for c in movie.chunks}
                    with patch.object(pool, 'provenance', side_effect=FormatError('allocation exclusion')), \
                            patch.object(engine, 'reconstruct', side_effect=AssertionError('must not copy')):
                        result = engine.attempt(case, movie, mapping, pool, {}, engine.Options(reconstruct_only=True), 'whole_mdat')
                    self.assertEqual(result['status'], 'rejected')
                    self.assertIn('allocation', result['reason'])
                    self.assertFalse(result.get('negative_screen', False))


if __name__ == '__main__':
    unittest.main()
