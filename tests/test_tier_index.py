"""Owner-labelled interval updates preserve eligibility without rebuilding views."""
import json
from pathlib import Path
import random
import tempfile
import unittest

from videorebuild.candidate_tiers import CandidateTiers, _IntervalIndex
from videorebuild.storage import ReaderPool, SafetyError
import test_candidate_tiers as fixtures


class TierIndexTests(unittest.TestCase):
    setup_case = fixtures.CandidateTierTests.setup_case
    publish = fixtures.CandidateTierTests.publish

    def test_overlapping_owners_and_removed_publications(self):
        with tempfile.TemporaryDirectory() as directory:
            case, sid, _ = self.setup_case(Path(directory))
            with ReaderPool(case.sources()) as pool:
                self.publish(case, pool, sid, 20, 20, key='a')
                self.publish(case, pool, sid, 30, 20, key='b')
                tiers = CandidateTiers(case, pool)
                first, second = tiers.view(('ordinary', 'a')), tiers.view(('ordinary', 'b'))
                self.assertTrue(first.permits(sid, 20, 10))
                self.assertFalse(first.permits(sid, 30, 10))
                self.assertFalse(second.permits(sid, 30, 10))
                self.assertTrue(second.permits(sid, 40, 10))
                self.assertEqual(list(tiers.view().available_ranges(sid)), [(0, 20), (50, 50)])
                case.result('b', 'unresolved', {'outputs': []})
                tiers.refresh()
                self.assertTrue(first.permits(sid, 20, 30))
                self.assertFalse(second.permits(sid, 20, 20))
                self.assertEqual(list(tiers.view().available_ranges(sid)), [(0, 20), (40, 60)])

    def test_new_owner_updates_only_its_intervals_and_views_share_index(self):
        with tempfile.TemporaryDirectory() as directory:
            case, sid, _ = self.setup_case(Path(directory))
            with ReaderPool(case.sources()) as pool:
                for i in range(20):
                    self.publish(case, pool, sid, i * 3, 2, key=str(i))
                tiers = CandidateTiers(case, pool)
                updates = tiers.stats['interval_updates']
                for i in range(20):
                    self.assertTrue(tiers.view(('ordinary', str(i))).permits(sid, i * 3, 2))
                self.assertEqual(tiers.stats['interval_updates'], updates)
                self.publish(case, pool, sid, 80, 2, key='new')
                tiers.refresh()
                self.assertEqual(tiers.stats['interval_updates'] - updates, 1)
                self.assertEqual(case.db.execute('SELECT count(*) FROM candidate_references').fetchone()[0], 21)
                generation = tiers.generation
                tiers.refresh()
                self.assertEqual(tiers.generation, generation)
                self.assertEqual(tiers.stats['interval_updates'] - updates, 1)

    def test_old_derived_cache_is_backed_up_and_restart_uses_publications(self):
        with tempfile.TemporaryDirectory() as directory:
            case, sid, _ = self.setup_case(Path(directory))
            with ReaderPool(case.sources()) as pool:
                self.publish(case, pool, sid, 20, 10)
                old = json.dumps({'revision': 1, 'references': ['untrusted old cache']})
                case.db.execute("INSERT INTO settings VALUES('candidate_tiers',?)", (old,))
                case.db.commit()
                CandidateTiers(case, pool)
                backup = case.db.execute("SELECT value FROM settings WHERE name='candidate_tiers_before_indexed_matching'").fetchone()[0]
                self.assertEqual(backup, old)
                case.db.execute("UPDATE candidate_references SET reference='{}'")
                case.db.commit()
                restarted = CandidateTiers(case, pool)
                self.assertFalse(restarted.view().permits(sid, 20, 10))
                reference = json.loads(case.db.execute('SELECT reference FROM candidate_references').fetchone()[0])
                self.assertEqual(reference['offset'], 20)

    def test_publisher_delta_does_not_scan_empty_jobs_but_rechecks_publications(self):
        with tempfile.TemporaryDirectory() as directory:
            case, sid, _ = self.setup_case(Path(directory))
            with ReaderPool(case.sources()) as pool:
                output = self.publish(case, pool, sid, 20, 10)
                for i in range(100):
                    case.result(str(i), 'awaiting_deep', {'outputs': []})
                tiers = CandidateTiers(case, pool)
                case.result('new-negative', 'unresolved', {'outputs': []})
                before = tiers.stats['jobs_checked']
                queries = []
                case.db.set_trace_callback(queries.append)
                tiers.refresh(('ordinary', 'new-negative'))
                case.db.set_trace_callback(None)
                self.assertEqual(tiers.stats['jobs_checked'] - before, 2)
                self.assertFalse(any('FROM jobs ORDER BY' in query for query in queries))
                output.write_bytes(b'changed!!!')
                with self.assertRaises(SafetyError):
                    tiers.refresh(('ordinary', 'new-negative'))

    def test_interval_updates_match_exhaustive_owner_union(self):
        rng = random.Random(587)
        stats = dict(interval_updates=0, interval_queries=0, interval_nodes_visited=0)
        index = _IntervalIndex(stats)
        keys = set()
        for _ in range(300):
            lo = rng.randrange(64)
            key = lo, rng.randrange(lo + 1, 66), ('ordinary', str(rng.randrange(7)))
            remove = key in keys
            index.change(key, remove=remove)
            if remove:
                keys.remove(key)
            else:
                keys.add(key)
            start = rng.randrange(64)
            end = rng.randrange(start + 1, 66)
            owner = None if rng.randrange(3) == 0 else ('ordinary', str(rng.randrange(7)))
            expected = {position for lo, hi, claimed in keys if claimed != owner
                        for position in range(max(start, lo), min(end, hi))}
            actual = {position for lo, hi in index.overlaps(start, end, owner)
                      for position in range(lo, hi)}
            self.assertEqual(actual, expected)
        self.assertLess(stats['interval_nodes_visited'], 300 * len(keys))

    def test_narrow_interval_queries_and_owner_exemption_avoid_full_scan(self):
        stats = dict(interval_updates=0, interval_queries=0, interval_nodes_visited=0)
        index = _IntervalIndex(stats)
        owner = ('ordinary', 'one-recording')
        for i in range(4000):
            index.change((i * 3, i * 3 + 2, owner))
        self.assertEqual(list(index.overlaps(9000, 9001, owner)), [])
        self.assertEqual(stats['interval_nodes_visited'], 0)
        self.assertEqual(list(index.overlaps(9000, 9001, None)), [(9000, 9001)])
        self.assertLess(stats['interval_nodes_visited'], 100)
        before = stats['interval_updates']
        index.change((9000, 9002, owner), remove=True)
        self.assertEqual(stats['interval_updates'] - before, 1)
        self.assertEqual(list(index.overlaps(9000, 9001, None)), [])


if __name__ == '__main__':
    unittest.main()
