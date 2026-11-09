"""Checkpoint cadence bounds durable metadata without saving on source work."""
from contextlib import closing, contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from videorebuild import association_resume
from videorebuild.storage import ReaderPool, SafetyError


SETTING = 'VIDEOREBUILD_ASSOCIATION_CHECKPOINT_SECONDS'


class AssociationCheckpointPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.case = SimpleNamespace(root=self.root/'case')
        self.case.root.mkdir()
        self.source = self.root/'source.bin'
        self.source.write_bytes(b'A'*100_000)
        self.other_source = self.root/'unused.bin'
        self.other_source.write_bytes(b'other evidence')
        self.domain = 'd'
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(SETTING, None)

    @staticmethod
    def source_row(path):
        state = path.stat()
        return dict(path=str(path), kind='file', size=state.st_size,
                    mtime_ns=state.st_mtime_ns, ino=state.st_ino, dev=state.st_dev,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    @contextmanager
    def opened(self, clock, *, interval=None, case=None):
        values = {} if interval is None else {SETTING: interval}
        with patch.dict(os.environ, values), patch.object(
                association_resume.time, 'monotonic', side_effect=lambda: clock.now), ReaderPool({
                    1: self.source_row(self.source), 2: self.source_row(self.other_source)}) as pool:
            checkpoint = association_resume.AssociationCheckpoint(case or self.case, pool, 'policy')
            checkpoint.register(self.domain)
            try:
                yield checkpoint
            finally:
                checkpoint.close(successful=False)

    @staticmethod
    def persisted(path):
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as database:
            database.row_factory = sqlite3.Row
            return dict(progress=[dict(row) for row in database.execute('SELECT * FROM progress ORDER BY domain')],
                        hits=[tuple(row) for row in database.execute(
                            'SELECT domain,fingerprint,sid,start FROM hit_locations ORDER BY domain,sid,start')],
                        identity=database.execute('SELECT value FROM identity').fetchone()[0])

    def visit(self, checkpoint, at, *, positive=False):
        if positive:
            checkpoint.add_hit(self.domain, 'f'*64, 1, at)
        checkpoint.advance(self.domain, 1, at, int(positive), int(positive))

    def test_default_is_60_seconds_without_processed_count_commits(self):
        clock = SimpleNamespace(now=0.0)
        with self.opened(clock) as checkpoint:
            self.visit(checkpoint, 0, positive=True)
            clock.now = 59.999
            self.visit(checkpoint, 1, positive=True)
            self.assertEqual(self.persisted(checkpoint.path)['progress'], [])
            clock.now = 60.0
            self.visit(checkpoint, 2, positive=True)
            saved = self.persisted(checkpoint.path)
            self.assertEqual(len(saved['hits']), 3)
            self.assertEqual(saved['progress'][0]['processed'], 3)
            self.assertEqual(saved['progress'][0]['complete'], 0)

    def test_custom_fractional_and_longer_intervals_control_save_boundary(self):
        for setting, seconds in (('0.25', 0.25), ('90', 90.0)):
            with self.subTest(interval=setting):
                case = SimpleNamespace(root=self.root/('case-'+setting))
                case.root.mkdir()
                clock = SimpleNamespace(now=0.0)
                with self.opened(clock, interval=setting, case=case) as checkpoint:
                    clock.now = seconds-0.001
                    self.visit(checkpoint, 0, positive=True)
                    self.assertEqual(self.persisted(checkpoint.path)['progress'], [])
                    clock.now = seconds
                    self.visit(checkpoint, 1, positive=True)
                    self.assertEqual(self.persisted(checkpoint.path)['progress'][0]['hits'], 2)

    def test_invalid_interval_fails_before_creating_work_or_sqlite(self):
        for index, value in enumerate(('', 'invalid', 'nan', 'NaN', 'inf', '-inf', '0', '-1', '0.0', '1e9999')):
            with self.subTest(interval=value):
                case = SimpleNamespace(root=self.root/f'invalid-{index}')
                with patch.dict(os.environ, {SETTING: value}), patch.object(
                        association_resume.sqlite3, 'connect', side_effect=AssertionError('SQLite opened')):
                    with self.assertRaises(SafetyError):
                        association_resume.AssociationCheckpoint(case, object(), 'policy')
                self.assertFalse(case.root.exists())

    def test_many_positive_locations_before_interval_do_not_save_a_count_page(self):
        clock = SimpleNamespace(now=0.0)
        with self.opened(clock) as checkpoint:
            for index in range(4500):
                self.visit(checkpoint, index, positive=True)
            saved = self.persisted(checkpoint.path)
            self.assertEqual(saved['progress'], [])
            self.assertEqual(saved['hits'], [])

    def test_many_zero_hit_locations_before_interval_do_not_save(self):
        clock = SimpleNamespace(now=0.0)
        with self.opened(clock) as checkpoint:
            for index in range(20_000):
                self.visit(checkpoint, index)
            self.assertEqual(self.persisted(checkpoint.path)['progress'], [])
            self.assertEqual(self.persisted(checkpoint.path)['hits'], [])
            clock.now = 60.0
            self.visit(checkpoint, 20_000)
            state = self.persisted(checkpoint.path)
            self.assertEqual(state['progress'][0]['processed'], 20_001)
            self.assertEqual(state['progress'][0]['complete'], 0)
            self.assertEqual(state['hits'], [])

    def test_actual_pending_hit_cap_commits_only_after_corresponding_cursor(self):
        self.assertEqual(association_resume.MAX_PENDING_HITS, 250_000)
        clock = SimpleNamespace(now=0.0)
        with patch.object(association_resume, 'MAX_PENDING_HITS', 2):
            with self.opened(clock) as checkpoint:
                for index in range(5000):
                    self.visit(checkpoint, index)
                self.visit(checkpoint, 5000, positive=True)
                self.assertEqual(self.persisted(checkpoint.path)['progress'], [])
                checkpoint.add_hit(self.domain, 'f'*64, 1, 5001)
                self.assertEqual(self.persisted(checkpoint.path)['hits'], [],
                                 'hit insertion committed before its matching cursor advance')
                checkpoint.advance(self.domain, 1, 5001, 1, 1)
                committed = self.persisted(checkpoint.path)
                self.assertEqual(committed['progress'][0]['processed'], 5002)
                self.assertEqual(committed['progress'][0]['hits'], 2)
                self.assertEqual(len(committed['hits']), 2)
                self.visit(checkpoint, 5002, positive=True)
                self.assertEqual(self.persisted(checkpoint.path), committed,
                                 'pending-hit counter did not reset after committing')
            with self.opened(clock) as checkpoint:
                self.assertEqual(checkpoint.register(self.domain)[2:5], [5002, 2, 2])
                self.assertEqual(self.persisted(checkpoint.path), committed)

    def test_interval_change_adopts_durable_prefix_without_new_generation(self):
        clock = SimpleNamespace(now=0.0)
        with self.opened(clock, interval='7') as checkpoint:
            clock.now = 7.0
            self.visit(checkpoint, 0, positive=True)
            before = self.persisted(checkpoint.path)
            inode = checkpoint.path.stat().st_ino
        clock.now = 100.0
        with self.opened(clock, interval='0.5') as checkpoint:
            self.assertEqual(checkpoint.register(self.domain)[2:5], [1, 1, 1])
            self.assertEqual(checkpoint.path.stat().st_ino, inode)
            self.assertEqual(self.persisted(checkpoint.path), before)
            clock.now = 100.5
            self.visit(checkpoint, 1, positive=True)
            after = self.persisted(checkpoint.path)
            self.assertEqual(after['identity'], before['identity'])
            self.assertEqual(after['progress'][0]['hits'], 2)

    def test_interrupt_between_hit_insert_and_cursor_advance_rolls_back_only_partial_page(self):
        clock = SimpleNamespace(now=0.0)
        with patch.object(association_resume, 'MAX_PENDING_HITS', 1):
            with self.assertRaises(KeyboardInterrupt):
                with self.opened(clock) as checkpoint:
                    self.visit(checkpoint, 0, positive=True)
                    committed = self.persisted(checkpoint.path)
                    checkpoint.add_hit(self.domain, 'f'*64, 1, 1)
                    self.assertEqual(self.persisted(checkpoint.path), committed)
                    raise KeyboardInterrupt
            with self.opened(clock) as checkpoint:
                self.assertEqual(checkpoint.register(self.domain)[2:5], [1, 1, 1])
                self.assertEqual(self.persisted(checkpoint.path), committed)

    def test_completion_commits_zero_hit_coverage_before_interval(self):
        clock = SimpleNamespace(now=0.0)
        with self.opened(clock) as checkpoint:
            for index in range(400):
                self.visit(checkpoint, index)
            self.assertEqual(self.persisted(checkpoint.path)['progress'], [])
            checkpoint.complete([self.domain])
            state = self.persisted(checkpoint.path)
            self.assertEqual(state['hits'], [])
            self.assertEqual(state['progress'][0]['processed'], 400)
            self.assertEqual(state['progress'][0]['complete'], 1)

    def test_completion_checks_unused_sources_before_marking_coverage(self):
        clock = SimpleNamespace(now=0.0)
        with self.opened(clock) as checkpoint:
            self.visit(checkpoint, 0)
            before = self.other_source.stat()
            self.other_source.write_bytes(b'OTHER EVIDENCE')
            os.utime(self.other_source, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaises(SafetyError):
                checkpoint.complete([self.domain])
            self.assertEqual(self.persisted(checkpoint.path)['progress'], [])

    def test_timed_disk_reserve_pause_keeps_prior_prefix_without_a_new_save(self):
        clock = SimpleNamespace(now=0.0)
        space = SimpleNamespace(available=True)

        def require_space(*args):
            if not space.available:
                raise SafetyError('synthetic case reserve exhausted')

        with patch.object(association_resume, 'require_search_space', require_space):
            with self.opened(clock) as checkpoint:
                clock.now = 60.0
                self.visit(checkpoint, 0, positive=True)
                committed = self.persisted(checkpoint.path)
                clock.now = 60.999
                self.visit(checkpoint, 1, positive=True)
                space.available = False
                clock.now = 61.0
                with self.assertRaises(SafetyError):
                    self.visit(checkpoint, 2, positive=True)
            space.available = True
            with self.opened(clock) as checkpoint:
                self.assertEqual(self.persisted(checkpoint.path), committed)
                self.assertEqual(checkpoint.register(self.domain)[2:5], [1, 1, 1])

    def test_processed_count_checks_disk_reserve_without_committing_zero_hits(self):
        clock = SimpleNamespace(now=0.0)
        space = SimpleNamespace(available=True)

        def require_space(*args):
            if not space.available:
                raise SafetyError('synthetic case reserve exhausted')

        with patch.object(association_resume, 'require_search_space', require_space):
            with self.opened(clock) as checkpoint:
                for index in range(4095):
                    self.visit(checkpoint, index)
                space.available = False
                with self.assertRaises(SafetyError):
                    self.visit(checkpoint, 4095)
                self.assertEqual(self.persisted(checkpoint.path)['progress'], [])
            space.available = True
            with self.opened(clock) as checkpoint:
                self.assertEqual(checkpoint.register(self.domain)[2:], [0, 0, 0, 0])

    def test_checkpoints_update_one_database_without_archived_generations(self):
        clock = SimpleNamespace(now=0.0)
        with self.opened(clock) as checkpoint:
            inode = checkpoint.path.stat().st_ino
            for index in range(3):
                clock.now += 60.0
                self.visit(checkpoint, index, positive=True)
                self.assertEqual(checkpoint.path.stat().st_ino, inode)
                self.assertEqual([path.name for path in checkpoint.path.parent.iterdir()],
                                 ['association-checkpoint.sqlite3'])
            self.assertEqual(len(self.persisted(checkpoint.path)['hits']), 3)


if __name__ == '__main__':
    unittest.main()
