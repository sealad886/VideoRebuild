"""Exact framing certificates survive bounded interruption without recording false misses."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_structural_index import source_row
from videorebuild import sample_certificates as certificates, structural_index
from videorebuild.bmff import FormatError
from videorebuild.storage import ReaderPool, SafetyError


PROFILE = (4, b'avc1', 4)
VALID = b'\0\0\0\x04\x65ABC'
NON_VCL = b'\0\0\0\x04\x67ABC'


class Task:
    def __init__(self, after_update=None):
        self.updates = []
        self.after_update = after_update

    def update(self, completed=None, **counts):
        self.updates.append((completed, counts))
        if self.after_update is not None:
            self.after_update(completed)

    def heartbeat(self, **counts):
        pass


class CertificateTests(unittest.TestCase):
    @contextmanager
    def postings(self, root, source, rows):
        case = SimpleNamespace(root=root)

        def scan(pool, maximum, wanted, options, state, task, *, source_ids):
            # The structural index accepts a codec union of plausible starts.
            # Supply both exact AVC positives and plausible false positives.
            for sid in source_ids:
                for offset in range(0, rows*len(VALID), len(VALID)):
                    yield sid, offset, 4

        with ReaderPool({1: source}) as pool, structural_index.postings(
                case, pool, [4], scan, SimpleNamespace(search_workers=1), Task()) as (db, stats):
            yield case, pool, db, stats['discovery_index']

    def test_exact_codec_and_scope_results_reopen_without_sample_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root/'samples.bin'
            path.write_bytes(VALID+NON_VCL+VALID)
            hevc_profile = (4, b'hvc1', 4)
            with self.postings(root, source_row(path), 3) as args:
                pool = args[1]
                original = pool.read

                def unavailable(sid, offset, size):
                    if offset == 2*len(VALID):
                        raise FormatError('unmapped sample')
                    return original(sid, offset, size)

                with certificates.sample_certificates(*args) as cache, patch.object(
                        pool, 'read', side_effect=unavailable):
                    with self.assertRaises(SafetyError):
                        cache.locations(PROFILE)
                    cache.prepare([PROFILE, hevc_profile], Task())
                    self.assertEqual(list(cache.locations(PROFILE)), [(1, 0)])
                    self.assertEqual(list(cache.locations(hevc_profile)), [])
                with certificates.sample_certificates(*args) as cache, patch.object(
                        pool, 'read', side_effect=AssertionError('certified sample read again')):
                    task = Task()
                    cache.prepare([PROFILE, hevc_profile], task)
                    self.assertEqual(task.updates, [(6, {'total': 6, 'reused_samples': 6})])
                    self.assertEqual(list(cache.locations(PROFILE)), [(1, 0)])

    def test_failed_read_replays_only_uncommitted_page_including_prior_positive(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(certificates, 'PAGE_ROWS', 2):
            root = Path(tmp)
            path = root/'samples.bin'
            path.write_bytes(VALID+NON_VCL+VALID+VALID)
            with self.postings(root, source_row(path), 4) as args:
                pool = args[1]
                original = pool.read

                def failed(sid, offset, size):
                    if offset == 3*len(VALID):
                        raise OSError('source read failed')
                    return original(sid, offset, size)

                with certificates.sample_certificates(*args) as cache, patch.object(
                        pool, 'read', side_effect=failed):
                    with self.assertRaises(OSError):
                        cache.prepare([PROFILE], Task())
                    with self.assertRaises(SafetyError):
                        cache.locations(PROFILE)
                with certificates.sample_certificates(*args) as cache, patch.object(
                        pool, 'read', wraps=original) as reads:
                    task = Task()
                    cache.prepare([PROFILE], task)
                    self.assertEqual(task.updates[0], (2, {'total': 4, 'reused_samples': 2}))
                    self.assertEqual([call.args[1] for call in reads.call_args_list], [16, 24])
                    self.assertEqual(list(cache.locations(PROFILE)), [(1, 0), (1, 16), (1, 24)])

    def test_interrupt_after_zero_hit_page_reuses_coverage(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(certificates, 'PAGE_ROWS', 1):
            root = Path(tmp)
            path = root/'samples.bin'
            path.write_bytes(NON_VCL+VALID)

            def interrupt_after_commit(completed):
                if completed == 1:
                    raise KeyboardInterrupt

            with self.postings(root, source_row(path), 2) as args:
                with certificates.sample_certificates(*args) as cache:
                    with self.assertRaises(KeyboardInterrupt):
                        cache.prepare([PROFILE], Task(interrupt_after_commit))
                with certificates.sample_certificates(*args) as cache:
                    task = Task()
                    cache.prepare([PROFILE], task)
                    self.assertEqual(task.updates[0], (1, {'total': 2, 'reused_samples': 1}))
                    self.assertEqual(list(cache.locations(PROFILE)), [(1, 8)])

    def test_same_mtime_source_rewrite_invalidates_warm_negative_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root/'samples.bin'
            path.write_bytes(NON_VCL)
            source = source_row(path)
            before = path.stat()
            with self.postings(root, source, 1) as args, certificates.sample_certificates(*args) as cache:
                cache.prepare([PROFILE], Task())
                self.assertEqual(list(cache.locations(PROFILE)), [])
            path.write_bytes(VALID)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.postings(root, source, 1) as args:
                with self.assertRaises(SafetyError), certificates.sample_certificates(*args):
                    pass

    def test_space_pause_leaves_prior_page_and_prevents_incomplete_readback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(certificates, 'PAGE_ROWS', 1):
            root = Path(tmp)
            path = root/'samples.bin'
            path.write_bytes(VALID*2)
            space = SimpleNamespace(free=certificates.SEARCH_RESERVE_BYTES)

            def fill_after_commit(completed):
                if completed == 1:
                    space.free = 0

            with self.postings(root, source_row(path), 2) as args, patch.object(
                    certificates.shutil, 'disk_usage', return_value=space):
                with certificates.sample_certificates(*args) as cache:
                    with self.assertRaises(SafetyError):
                        cache.prepare([PROFILE], Task(fill_after_commit))
                    with self.assertRaises(SafetyError):
                        cache.locations(PROFILE)
                space.free = certificates.SEARCH_RESERVE_BYTES
                with certificates.sample_certificates(*args) as cache:
                    task = Task()
                    cache.prepare([PROFILE], task)
                    self.assertEqual(task.updates[0], (1, {'total': 2, 'reused_samples': 1}))
                    self.assertEqual(list(cache.locations(PROFILE)), [(1, 0), (1, 8)])

    def test_sqlite_full_rolls_back_page_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(certificates, 'PAGE_ROWS', 256):
            root = Path(tmp)
            path = root/'samples.bin'
            path.write_bytes(VALID*1024)
            with self.postings(root, source_row(path), 1024) as args:
                with certificates.sample_certificates(*args) as cache:
                    def limit_after_commit(completed):
                        if completed == 256:
                            pages = cache.db.execute('PRAGMA page_count').fetchone()[0]
                            cache.db.execute(f'PRAGMA max_page_count={pages}')

                    with self.assertRaises(SafetyError) as full:
                        cache.prepare([PROFILE], Task(limit_after_commit))
                    self.assertTrue(certificates.search_disk_full(full.exception.__cause__))
                with certificates.sample_certificates(*args) as cache:
                    task = Task()
                    cache.prepare([PROFILE], task)
                    self.assertEqual(task.updates[0], (256, {'total': 1024, 'reused_samples': 256}))
                    self.assertEqual(list(cache.locations(PROFILE)), [(1, n*8) for n in range(1024)])

    def test_changed_posting_frequency_rejects_completed_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root/'samples.bin'
            path.write_bytes(VALID)
            with self.postings(root, source_row(path), 1) as args:
                with certificates.sample_certificates(*args) as cache:
                    cache.prepare([PROFILE], Task())
                with args[2] as db:
                    db.execute('UPDATE frequencies SET total=2 WHERE length=4')
                with certificates.sample_certificates(*args) as cache:
                    with self.assertRaises(SafetyError):
                        cache.prepare([PROFILE], Task())

    def test_linked_journal_cannot_point_outside_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root/'samples.bin'
            path.write_bytes(VALID)
            outside = root/'outside'
            outside.write_bytes(b'preserve outside file')
            with self.postings(root, source_row(path), 1) as args:
                with certificates.sample_certificates(*args) as cache:
                    cache.prepare([PROFILE], Task())
                cache_path, = (root/'work'/'structural-index').glob('*.samples-*.sqlite3')
                cache_path.with_name(cache_path.name+'-journal').symlink_to(outside)
                with self.assertRaises(SafetyError), certificates.sample_certificates(*args):
                    pass
                self.assertEqual(outside.read_bytes(), b'preserve outside file')
