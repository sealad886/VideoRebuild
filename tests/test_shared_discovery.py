"""Deep metadata batches share discovery without losing physical alternatives."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_deep_witnesses import structural_movie
from videorebuild import engine
from videorebuild.storage import Case, ReaderPool, SafetyError, scan_logical


class SharedDiscoveryTests(unittest.TestCase):
    def recover(self, maximum_groups, *, max_anchor_checks=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'tokens.bin'
            source.write_bytes(b'\0\0\0\x04\x65ABC' + b'Z' * 8 + b'\0\0\0\x04\x65ABC')
            targets = [structural_movie([100+i, 200+i], [8, 8]) for i in range(8)]
            with Case(root / 'case') as case:
                sid = case.register_source(source)
                rows = {sid: dict(case.db.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone())}
                with ReaderPool(rows) as pool, patch.object(pool, 'iter_blocks', wraps=pool.iter_blocks) as scans:
                    engine.deep_search(case, targets, pool, {}, engine.Options(deep=True, reconstruct_only=True,
                                       max_groups=maximum_groups, max_anchor_checks=max_anchor_checks), 'fixture')
                    scan_count = scans.call_count
                jobs = [case.job('ordinary', movie.digest) for movie in targets]
                result = [(j['status'], {c['media_sha256'] for c in j['detail']['candidates']}) for j in jobs]
                self.assertTrue(all(not j['detail']['decode_checked'] for j in jobs))
                self.assertEqual(source.read_bytes(), b'\0\0\0\x04\x65ABC' + b'Z' * 8 + b'\0\0\0\x04\x65ABC')
                return result, scan_count, jobs

    def test_bounded_batches_share_one_scan_and_preserve_all_outputs(self):
        batched, scan_count, jobs = self.recover(2)
        together, together_scans, _ = self.recover(100)
        self.assertEqual(batched, together)
        self.assertTrue(all(status == 'reconstructed' for status, _ in batched))
        self.assertEqual(scan_count, 1)
        self.assertEqual(together_scans, 1)
        self.assertTrue(all(j['detail']['shared_discovery'] for j in jobs))

    def test_recover_shares_discovery_across_metadata_memory_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'tokens.bin'
            targets = [structural_movie([100+i, 200+i], [8, 8]) for i in range(8)]
            source.write_bytes(b''.join(movie.data for movie in targets) +
                               b'\0\0\0\x04\x65ABC' + b'Z' * 8 + b'\0\0\0\x04\x65ABC')
            with Case(root / 'case') as case:
                scan_logical(case, case.register_source(source))
                visited = []
                original = ReaderPool.iter_blocks
                def counted(pool, *args, **kwargs):
                    visited.append(args[0])
                    yield from original(pool, *args, **kwargs)
                # Force the old outer metadata-flush condition with tiny metadata.
                with patch.object(engine, 'MAX_DEEP_METADATA', 1), \
                        patch.object(engine, 'whole_payload', return_value=('unresolved', [], False)), \
                        patch.object(ReaderPool, 'iter_blocks', counted):
                    engine.recover(case, engine.Options(deep=True, reconstruct_only=True, include_recovered=True))
                # Base-scoped discovery is reused across tiers and metadata batches.
                self.assertEqual(len(visited), 1)
                jobs = list(case.get_jobs('ordinary'))
                self.assertEqual(len(jobs), len(targets))
                self.assertTrue(all(job['status'] == 'reconstructed' for job in jobs))
                self.assertTrue(all(job['detail']['shared_discovery'] for job in jobs))

    def test_explicit_scan_budget_remains_honestly_limited(self):
        results, scans, jobs = self.recover(2, max_anchor_checks=1)
        self.assertEqual(scans, 8)
        self.assertTrue(all(status == 'search_limited' for status, _ in results))
        self.assertTrue(all(not j['detail']['outputs'] for j in jobs))

    def test_source_mutation_after_shared_discovery_cannot_be_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'tokens.bin'
            source.write_bytes(b'\0\0\0\x04\x65ABC')
            targets = [structural_movie([100+i], [8]) for i in range(2)]
            with Case(root / 'case') as case:
                sid = case.register_source(source)
                rows = {sid: dict(case.db.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone())}
                real_batch = engine._deep_batch
                def changed(*args, **kwargs):
                    source.write_bytes(b'changed data')
                    return real_batch(*args, **kwargs)
                with ReaderPool(rows) as pool, patch.object(engine, '_deep_batch', side_effect=changed):
                    with self.assertRaises(SafetyError):
                        engine.deep_search(case, targets, pool, {}, engine.Options(deep=True, reconstruct_only=True, max_groups=1), 'fixture')


if __name__ == '__main__':
    unittest.main()
