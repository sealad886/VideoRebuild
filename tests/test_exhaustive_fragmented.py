"""Exhaustive indexed-start discovery keeps bounded reads and immutable clips."""
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fragmented_helpers import full, generate, repacketize
from videorebuild import __version__
from videorebuild.bmff import make_box
from videorebuild.image_recovery import recover_fragmented
from videorebuild.ranges import AllocationScope
from videorebuild.storage import Case, ReaderPool, SafetyError, scan_logical
from videorebuild.validate import doctor


class ExhaustiveFragmentedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture.cleanup)
        raw, prefix = generate(Path(cls.fixture.name)/'generated.mp4', 'avc')
        cls.initialization = prefix.initialization.data
        cls.movie = repacketize(raw, prefix, with_tfdt=True)
        cls.tools = doctor()
        cls.bad = make_box(b'moof', full(b'mfhd', 0, struct.pack('>I', 2))+make_box(b'traf', b''))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def options(self, **changes):
        values = dict(max_record_bytes=1024*1024, max_segments=None, timeout=30, max_output_bytes=1024*1024)
        values.update(changes)
        return SimpleNamespace(**values)

    def indexed(self, case, data):
        image = self.root/'image.bin'
        image.write_bytes(data)
        sid = case.register_source(image, 'image')
        scan_logical(case, sid)
        return sid

    def test_more_than_64_starts_and_distant_media_are_recovered(self):
        window = len(self.movie)+128
        start = len(self.initialization)+70*(len(self.bad)+8)+window*2
        data = self.initialization+(self.bad+make_box(b'mdat', b''))*70+b'\0'*(window*2)+self.movie[len(self.initialization):]
        with Case(self.root/'case') as case:
            sid = self.indexed(case, data)
            with ReaderPool(case.sources()) as pool:
                recover_fragmented(case, pool, self.tools, self.options(max_record_bytes=window))
            job = next(case.get_jobs('fragmented'))
            self.assertEqual(job['status'], 'decoded_segments')
            self.assertGreater(job['detail']['candidate_starts_considered'], 64)
            segment = next(item for item in job['detail']['segments'] if item.get('output'))
            self.assertEqual(segment['logical_start'], start)
            self.assertGreater(start, window)
            self.assertEqual((case.root/segment['output']).read_bytes(), self.movie)
            self.assertEqual(segment['source_ranges'][-1]['source_id'], sid)

    def test_repeated_malformed_reads_do_not_end_discovery(self):
        count, tail = 12, 64*1024
        total = len(self.initialization)+count*(len(self.bad)+8)+tail
        parts = [self.initialization]
        for index in range(count):
            offset = len(self.initialization)+index*(len(self.bad)+8)
            parts.extend([self.bad, struct.pack('>I4s', total-offset-len(self.bad), b'mdat')])
        parts.append(bytes(tail))
        with Case(self.root/'case') as case:
            self.indexed(case, b''.join(parts))
            with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix') as validate:
                largest = 0
                read = pool.read
                def bounded(sid, offset, size):
                    nonlocal largest
                    largest = max(largest, size)
                    return read(sid, offset, size)
                with patch.object(pool, 'read', side_effect=bounded):
                    recover_fragmented(case, pool, {}, self.options())
            job = next(case.get_jobs('fragmented'))
            self.assertEqual(job['status'], 'blocked')
            self.assertEqual(job['detail']['candidate_starts_considered'], count)
            self.assertGreater(job['detail']['media_read_bytes_charged'], 2*total)
            self.assertLessEqual(largest, self.options().max_record_bytes)
            validate.assert_not_called()

    def test_allocation_scope_rejects_initialization_before_reading_excluded_bytes(self):
        moov_at = int.from_bytes(self.initialization[:4], 'big')
        for allowed in ([], [(0, 16)], [(0, 16), (moov_at, 16)]):
            with self.subTest(allowed=allowed), Case(self.root/f'case-{len(allowed)}') as case:
                sid = self.indexed(case, self.movie)
                scope = AllocationScope({sid: allowed}, 'test-scope')
                with ReaderPool(case.sources(), allocation_scope=scope) as pool:
                    read = pool.read
                    requests = []
                    def observed(sid, offset, size):
                        self.assertTrue(pool.permits(sid, offset, size))
                        requests.append((offset, size))
                        return read(sid, offset, size)
                    with patch.object(pool, 'read', side_effect=observed):
                        self.assertEqual(recover_fragmented(case, pool, {}, self.options()), 0)
                self.assertEqual(list(case.get_jobs('fragmented')), [])
                self.assertLessEqual(len(requests), len(allowed))

    def test_unallocated_sections_stay_separate_across_allocated_gap(self):
        payload = self.movie[len(self.initialization):]
        gap = b'allocated bytes'*200
        later = len(self.movie)+len(gap)
        with Case(self.root/'case') as case:
            sid = self.indexed(case, self.movie+gap+payload)
            scope = AllocationScope({sid: [(0, len(self.movie)), (later, len(payload))]}, 'test-scope')
            with ReaderPool(case.sources(), allocation_scope=scope) as pool:
                read = pool.read
                def permitted(sid, offset, size):
                    self.assertTrue(pool.permits(sid, offset, size))
                    return read(sid, offset, size)
                with patch.object(pool, 'read', side_effect=permitted):
                    recover_fragmented(case, pool, self.tools, self.options())
            job = next(case.get_jobs('fragmented'))
            self.assertEqual(job['status'], 'decoded_segments')
            self.assertEqual(len(job['detail']['outputs']), 2)
            for name in job['detail']['outputs']:
                self.assertEqual((case.root/name).read_bytes(), self.movie)
            for segment in job['detail']['segments']:
                if segment.get('output'):
                    for extent in segment['source_ranges']:
                        self.assertTrue(extent['source_offset']+extent['length'] <= len(self.movie) or
                                        extent['source_offset'] >= later)

    def test_scope_change_requires_retry_and_reconsiders_blocked_job(self):
        with Case(self.root/'case') as case:
            sid = self.indexed(case, self.movie)
            first = AllocationScope({sid: [(0, len(self.initialization))]}, 'scope-A')
            second = AllocationScope({sid: [(0, len(self.movie))]}, 'scope-B')
            with ReaderPool(case.sources(), allocation_scope=first) as pool:
                recover_fragmented(case, pool, self.tools, self.options())
            self.assertEqual(next(case.get_jobs('fragmented'))['status'], 'blocked')
            with ReaderPool(case.sources(), allocation_scope=second) as pool:
                with self.assertRaises(SafetyError):
                    recover_fragmented(case, pool, self.tools, self.options())
                self.assertEqual(recover_fragmented(case, pool, self.tools, self.options(), retry=True), 1)
            job = next(case.get_jobs('fragmented'))
            self.assertEqual(job['status'], 'decoded_prefix')
            self.assertEqual(job['detail']['search_policy']['allocation_scope'], 'scope-B')
            self.assertEqual(job['detail']['segments'][-1]['allocation_scope'], 'scope-B')

    def test_excluded_publication_remains_historical_without_recertification(self):
        with Case(self.root/'case') as case:
            sid = self.indexed(case, self.movie)
            first = AllocationScope({sid: [(0, len(self.movie))]}, 'scope-A')
            with ReaderPool(case.sources(), allocation_scope=first) as pool:
                recover_fragmented(case, pool, self.tools, self.options())
            old = next(case.get_jobs('fragmented'))
            outputs = {name: (case.root/name).read_bytes() for name in old['detail']['outputs']}
            second = AllocationScope({sid: []}, 'scope-B')
            with ReaderPool(case.sources(), allocation_scope=second) as pool:
                with patch('videorebuild.image_recovery.validate_prefix') as validate:
                    self.assertEqual(recover_fragmented(case, pool, self.tools, self.options(), retry=True), 0)
                validate.assert_not_called()
            detail = next(case.get_jobs('fragmented'))['detail']
            self.assertEqual(detail['historical_allocation_scope'], 'scope-A')
            self.assertEqual(detail['current_allocation_scope'], 'scope-B')
            self.assertEqual(detail['historical_outputs'], list(outputs))
            self.assertEqual(detail['search_policy']['allocation_scope'], 'scope-A')
            self.assertTrue(all(segment['allocation_scope'] == 'scope-A' for segment in detail['segments']))
            for name, before in outputs.items():
                self.assertEqual((case.root/name).read_bytes(), before)

    def test_policy_expansion_remains_pending_across_batch_limit(self):
        recording = self.movie+b'gap'+self.movie[len(self.initialization):]
        with Case(self.root/'case') as case:
            self.indexed(case, recording+b'between'+recording)
            with ReaderPool(case.sources()) as pool:
                self.assertEqual(recover_fragmented(case, pool, self.tools, self.options(max_segments=1)), 2)
                self.assertTrue(all(job['status'] == 'search_limited' for job in case.get_jobs('fragmented')))
                self.assertEqual(recover_fragmented(case, pool, self.tools, self.options(), limit=1, retry=True), 1)
                # The case policy has already expanded, but the other job still
                # records its old coverage and must remain pending next batch.
                self.assertEqual(recover_fragmented(case, pool, self.tools, self.options(), limit=1), 1)
            jobs = list(case.get_jobs('fragmented'))
            self.assertEqual(len(jobs), 2)
            self.assertTrue(all(job['status'] == 'decoded_segments' for job in jobs))
            self.assertTrue(all(len(job['detail']['outputs']) == 2 for job in jobs))

    def test_retry_expands_limited_job_and_preserves_published_prefix(self):
        data = self.movie+b'corrupt gap'+self.movie[len(self.initialization):]
        with Case(self.root/'case') as case:
            self.indexed(case, data)
            with ReaderPool(case.sources()) as pool:
                recover_fragmented(case, pool, self.tools, self.options(max_segments=1))
            job = next(case.get_jobs('fragmented'))
            self.assertEqual(job['status'], 'search_limited')
            original = {name: (case.root/name).read_bytes() for name in job['detail']['outputs']}
            self.assertEqual(len(original), 1)
            # Emulate the prior combined binding while keeping its saved output.
            legacy = dict(version=__version__, max_record_bytes=1024*1024, max_segments=1,
                          timeout=30, tools=self.tools)
            case.db.execute("UPDATE settings SET value=? WHERE name='fragmented_settings'", (json.dumps(legacy),))
            case.db.execute("DELETE FROM settings WHERE name='fragmented_search_policy'")
            case.db.commit()
            with ReaderPool(case.sources()) as pool:
                with self.assertRaises(SafetyError):
                    recover_fragmented(case, pool, self.tools, self.options(timeout=31), retry=True)
                with self.assertRaises(SafetyError):
                    recover_fragmented(case, pool, self.tools, self.options())
                self.assertEqual(recover_fragmented(case, pool, self.tools, self.options(), retry=True), 1)
            job = next(case.get_jobs('fragmented'))
            self.assertEqual(job['status'], 'decoded_segments')
            self.assertEqual(len(job['detail']['outputs']), 2)
            for name, before in original.items():
                self.assertEqual((case.root/name).read_bytes(), before)
                self.assertIn(name, job['detail']['outputs'])
            event = case.db.execute("SELECT detail FROM events WHERE kind='fragmented_search_policy_expanded'").fetchone()
            backup = case.root/json.loads(event[0])['backup']
            self.assertEqual(json.loads(backup.read_text())['fragmented_settings'], legacy)
            settings = json.loads(case.db.execute("SELECT value FROM settings WHERE name='fragmented_settings'").fetchone()[0])
            self.assertEqual(settings, {key: value for key, value in legacy.items() if key != 'max_segments'})
