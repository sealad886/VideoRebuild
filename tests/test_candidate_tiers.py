"""Accepted physical references demote without deleting evidence or aliases."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.candidate_tiers import CandidateTiers
from videorebuild.ranges import Extent, AllocationScope
from videorebuild.storage import Case, ReaderPool, SafetyError, file_sha256, write_json


class CandidateTierTests(unittest.TestCase):
    def test_narrow_candidate_check_does_not_walk_unrelated_extents(self):
        class Counted:
            def __init__(self, values):
                self.values, self.reads = values, 0

            def __len__(self):
                return len(self.values)

            def __getitem__(self, index):
                self.reads += 1
                return self.values[index]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'image.bin'
            source.write_bytes(b'x' * 20_000)
            with Case(root / 'case') as case:
                physical = case.register_source(source, 'image')
                logical = case.add_stream('many.mp4',
                    [Extent(i, physical, i * 2, 1) for i in range(10_000)], size=10_000)
                rows = {r['id']: dict(r) for r in case.db.execute('SELECT * FROM sources')}
                with ReaderPool(rows, physical_sources=case.physical_sources()) as pool:
                    self.publish(case, pool, physical, 19_998, 1)
                    view = CandidateTiers(case, pool).view()
                    counted = Counted(pool.extents[logical])
                    pool.extents[logical] = counted
                    self.assertFalse(view.candidate_permits(logical, 9_999, 1))
                    self.assertLess(counted.reads, 40)

    def test_candidate_gap_boundaries_and_empty_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            case, physical, _ = self.setup_case(Path(directory))
            logical = case.add_stream('gapped.mp4',
                [Extent(0, physical, 0, 10), Extent(20, physical, 20, 10),
                 Extent(40, physical, 40, 10)], size=50)
            rows = {r['id']: dict(r) for r in case.db.execute('SELECT * FROM sources')}
            with ReaderPool(rows, physical_sources=case.physical_sources()) as pool:
                self.publish(case, pool, physical, 20, 10)
                view = CandidateTiers(case, pool).view()
                self.assertTrue(view.candidate_permits(logical, 5, 10))
                self.assertTrue(view.candidate_permits(logical, 15, 5))
                self.assertFalse(view.candidate_permits(logical, 15, 6))
                self.assertTrue(view.candidate_permits(logical, 30, 10))
                self.assertTrue(view.candidate_permits(logical, 25, 0))
                self.assertTrue(view.candidate_permits(logical, 50, 0))

    def test_candidate_limits_stay_in_logical_coordinates_across_extents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'image.bin'
            source.write_bytes(bytes(range(256)))
            with Case(root / 'case') as case:
                physical = case.register_source(source, 'image')
                above = case.add_stream('physical-above.mp4',
                    [Extent(0, physical, 100, 1), Extent(1, physical, 200, 1)], size=2)
                below = case.add_stream('physical-below.mp4',
                    [Extent(100, physical, 0, 1), Extent(101, physical, 1, 1)], size=102)
                rows = {r['id']: dict(r) for r in case.db.execute('SELECT * FROM sources')}
                with ReaderPool(rows, physical_sources=case.physical_sources()) as pool:
                    self.publish(case, pool, physical, 200, 1, key='accepted-high')
                    self.publish(case, pool, physical, 1, 1, key='accepted-low')
                    view = CandidateTiers(case, pool).view()
                    for sid, offset, length, expected in (
                            (above, 0, 1, True), (above, 1, 1, False), (above, 0, 2, False),
                            (below, 100, 2, False), (below, 99, 1, True),
                            (below, 99, 2, True), (below, 99, 3, False)):
                        with self.subTest(source=sid, offset=offset, length=length):
                            self.assertEqual(view.candidate_permits(sid, offset, length), expected)

    def setup_case(self, root):
        source = root / 'image.bin'
        source.write_bytes(bytes(range(100)))
        case = Case(root / 'case').__enter__()
        self.addCleanup(case.__exit__, None, None, None)
        sid = case.register_source(source, 'image')
        case.db.execute('UPDATE sources SET complete=1,sha256=?', ('f' * 64,))
        case.db.commit()
        return case, sid, source

    def publish(self, case, pool, sid, offset, length, key='accepted'):
        output = case.root / 'reconstructed' / (key + '.mp4')
        candidate = case.root / 'candidates' / (key + '.mp4')
        raw = pool.read(sid, offset, length)
        output.write_bytes(raw)
        candidate.write_bytes(raw)
        digest = file_sha256(output)
        write_json(candidate.with_suffix('.json'), {'provenance': {'chunks': [
            {'physical_extents': pool.provenance(sid, offset, length)}]}})
        case.result(key, 'reconstructed', {'outputs': [str(output.relative_to(case.root))],
                    'candidates': [{'path': str(candidate.relative_to(case.root)), 'sha256': digest}]})
        return output

    def test_interval_boundaries_owner_exemption_and_evidence_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            case, sid, source = self.setup_case(Path(d))
            with ReaderPool(case.sources()) as pool:
                tiers = CandidateTiers(case, pool)
                view = tiers.view()
                self.assertTrue(view.permits(sid, 10, 10))
                self.publish(case, pool, sid, 20, 10)
                tiers.refresh()
                self.assertEqual(list(view.available_ranges(sid)), [(0, 20), (30, 70)])
                self.assertTrue(view.permits(sid, 10, 10))
                self.assertTrue(view.permits(sid, 30, 1))
                self.assertFalse(view.permits(sid, 19, 2))
                self.assertFalse(view.permits(sid, 29, 2))
                self.assertTrue(tiers.view(('ordinary', 'accepted')).permits(sid, 20, 10))
                self.assertEqual(view.read(sid, 20, 10), bytes(range(20, 30)))
                self.assertEqual(source.read_bytes(), bytes(range(100)))
                self.assertEqual(case.db.execute('SELECT count(*) FROM sources').fetchone()[0], 1)

    def test_hardlink_alias_and_logical_mapping_demote_same_physical_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            case, sid, source = self.setup_case(root)
            alias = root / 'alias.bin'
            os.link(source, alias)
            alias_id = case.register_source(alias, 'image')
            logical = case.add_stream('logical', [Extent(0, sid, 10, 30), Extent(30, sid, 60, 30)], size=60)
            case.db.execute('UPDATE sources SET complete=1')
            case.db.commit()
            with ReaderPool(case.sources(), physical_sources=case.physical_sources()) as pool:
                self.publish(case, pool, sid, 20, 10)
                tiers = CandidateTiers(case, pool)
                self.assertFalse(tiers.view().permits(alias_id, 20, 1))
                self.assertEqual(list(tiers.view().available_ranges(logical)), [(0, 10), (20, 40)])
                self.assertEqual(b''.join(data[:owned] for _, data, owned in tiers.view().iter_blocks(logical)),
                                 bytes(range(10, 20)) + bytes(range(30, 40)) + bytes(range(60, 90)))

    def test_resume_rebuilds_only_from_publications_and_checks_output_changes(self):
        with tempfile.TemporaryDirectory() as d:
            case, sid, source = self.setup_case(Path(d))
            with ReaderPool(case.sources()) as pool:
                output = self.publish(case, pool, sid, 40, 10)
                tiers = CandidateTiers(case, pool)
                cache = json.loads(case.db.execute("SELECT value FROM settings WHERE name='candidate_tiers'").fetchone()[0])
                self.assertEqual(cache['references'][0]['source_identity']['ino'], source.stat().st_ino)
                restarted = CandidateTiers(case, pool)
                self.assertFalse(restarted.view().permits(sid, 40, 1))
                with patch('videorebuild.candidate_tiers.file_sha256', side_effect=AssertionError('redundant hash')), \
                        patch.object(Path, 'read_text', side_effect=AssertionError('unchanged manifest reread')):
                    tiers.refresh()
                output.write_bytes(b'changed!!!')
                with self.assertRaises(SafetyError):
                    tiers.refresh()
                case.result('accepted', 'unresolved', {'outputs': []})
                tiers.refresh()
                self.assertTrue(tiers.view().permits(sid, 40, 10))

    def test_base_allocation_scope_still_limits_first_and_second_tier(self):
        with tempfile.TemporaryDirectory() as d:
            case, sid, _ = self.setup_case(Path(d))
            scope = AllocationScope({sid: [(10, 40)]}, 'fixture')
            with ReaderPool(case.sources(), allocation_scope=scope) as pool:
                self.publish(case, pool, sid, 20, 10)
                tiers = CandidateTiers(case, pool)
                self.assertEqual(list(tiers.view().available_ranges(sid)), [(10, 10), (30, 20)])
                self.assertFalse(tiers.view(('ordinary', 'accepted')).permits(sid, 0, 1))
                self.assertTrue(tiers.view(('ordinary', 'accepted')).permits(sid, 20, 10))

    def test_candidate_bounding_span_does_not_require_allocated_gaps(self):
        with tempfile.TemporaryDirectory() as d:
            case, sid, _ = self.setup_case(Path(d))
            scope = AllocationScope({sid: [(10, 10), (40, 10)]}, 'fixture')
            with ReaderPool(case.sources(), allocation_scope=scope) as pool:
                tiers = CandidateTiers(case, pool)
                view = tiers.view()
                self.assertTrue(view.candidate_permits(sid, 10, 40))
                self.assertFalse(view.permits(sid, 10, 40))
                self.publish(case, pool, sid, 40, 10)
                tiers.refresh()
                self.assertTrue(tiers.has_recovered)
                self.assertFalse(view.candidate_permits(sid, 10, 40))
                self.assertTrue(view.candidate_permits(sid, 10, 30))
                self.assertTrue(tiers.view(('ordinary', 'accepted')).candidate_permits(sid, 10, 40))

    def test_historical_publications_keep_demotions_when_current_search_is_unresolved(self):
        with tempfile.TemporaryDirectory() as d:
            case, sid, _ = self.setup_case(Path(d))
            with ReaderPool(case.sources()) as pool:
                output = self.publish(case, pool, sid, 20, 10)
                tiers = CandidateTiers(case, pool)
                candidate = case.job('ordinary', 'accepted')['detail']['candidates'][0]
                case.result('accepted', 'unresolved', {'outputs': [], 'candidates': [],
                    'historical_publications': [{'path': str(output.relative_to(case.root)),
                        'candidate': candidate['path'], 'sha256': candidate['sha256']}]})
                tiers.refresh()
                restarted = CandidateTiers(case, pool)
                self.assertFalse(restarted.view().permits(sid, 20, 10))
                self.assertTrue(restarted.view(('ordinary', 'accepted')).permits(sid, 20, 10))
                self.assertTrue(output.is_file())

    def test_fragmented_sidecar_hash_is_verified_before_demoting(self):
        with tempfile.TemporaryDirectory() as d:
            case, sid, _ = self.setup_case(Path(d))
            with ReaderPool(case.sources()) as pool:
                output = case.root / 'reconstructed' / 'fragment.mp4'
                output.write_bytes(pool.read(sid, 60, 10))
                manifest = output.with_suffix('.json')
                manifest.write_text('{}')
                relative = str(output.relative_to(case.root))
                case.result('fragment', 'reconstructed_prefix', {'outputs': [relative],
                    'output_evidence': [{'path': relative, 'sha256': file_sha256(output),
                        'manifest_path': str(manifest.relative_to(case.root)), 'manifest_sha256': file_sha256(manifest)}],
                    'segments': [{'output': relative, 'source_ranges': pool.provenance(sid, 60, 10)}]}, kind='fragmented')
                manifest.write_text('{"changed": true}')
                with self.assertRaises(SafetyError):
                    CandidateTiers(case, pool)

    def test_fragmented_source_ranges_and_nonaccepted_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            case, sid, _ = self.setup_case(Path(d))
            with ReaderPool(case.sources()) as pool:
                output = case.root / 'reconstructed' / 'fragment.mp4'
                output.write_bytes(pool.read(sid, 60, 10))
                relative = str(output.relative_to(case.root))
                detail = {'outputs': [relative], 'output_evidence': [{'path': relative, 'sha256': file_sha256(output)}],
                          'segments': [{'output': relative, 'source_ranges': pool.provenance(sid, 60, 10)}]}
                case.result('fragment', 'reconstructed_prefix', detail, kind='fragmented')
                case.result('ambiguous', 'ambiguous', {'outputs': [], 'candidates': []})
                tiers = CandidateTiers(case, pool)
                self.assertEqual(list(tiers.view().available_ranges(sid)), [(0, 60), (70, 30)])
                self.assertTrue(tiers.view(('fragmented', 'fragment')).permits(sid, 60, 10))


if __name__ == '__main__':
    unittest.main()
