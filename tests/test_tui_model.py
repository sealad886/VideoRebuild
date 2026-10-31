"""The TUI must preserve canonical argv and observe cases without creating state."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.storage import Case
from videorebuild.tui_model import Settings, build_arguments, snapshot


class TuiArgumentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input with spaces;$(echo ignored).mp4'
        self.source.write_bytes(b'synthetic')
        self.case = self.root / 'case with spaces'

    def test_safe_defaults_and_literal_path_arguments(self):
        settings = Settings(case=str(self.case), input=str(self.source))
        argv = build_arguments(settings, 'run')
        self.assertEqual(argv, ['run', '--case', str(self.case), '--input', str(self.source),
                                '--deep', '--reconstruct-only'])
        self.assertFalse(self.case.exists())
        self.assertNotIn('--include-recovered', argv)

    def test_scan_omits_recovery_flags_and_keeps_image(self):
        settings = Settings(case=str(self.case), image=str(self.source), include_recovered=True, retry=True)
        self.assertEqual(build_arguments(settings, 'scan'),
                         ['scan', '--case', str(self.case), '--image', str(self.source)])

    def test_resume_reuses_index_and_explicit_allocation_policy(self):
        with Case(self.case):
            pass
        allocation = self.root / 'allocation.json'
        allocation.write_text('{}')
        settings = Settings(case=str(self.case), input='not used for recover', image='not used',
                            allocation_map=str(allocation), include_recovered=True, retry=True)
        argv = build_arguments(settings, 'recover')
        self.assertIn('--allocation-map', argv)
        self.assertIn('--include-recovered', argv)
        self.assertIn('--retry', argv)
        self.assertNotIn('--input', argv)
        self.assertNotIn('--image', argv)

    def test_invalid_configuration_never_creates_case(self):
        for settings, action in [(Settings(), 'run'), (Settings(case=str(self.case)), 'run'),
                                 (Settings(case=str(self.case)), 'recover'),
                                 (Settings(case=str(self.case), image='/dev/null'), 'scan'),
                                 (Settings(case=str(self.case), input=str(self.source), allocation_map='map.json'), 'run'),
                                 (Settings(case=str(self.case), input=str(self.source)), 'unknown')]:
            with self.subTest(settings=settings, action=action), self.assertRaises(ValueError):
                build_arguments(settings, action)
        self.assertFalse(self.case.exists())


class TuiSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = self.root / 'case'

    def test_missing_case_is_observation_only(self):
        observed = snapshot(self.case)
        self.assertIsNotNone(observed['error'])
        self.assertIsNone(observed['busy'])
        self.assertGreater(observed['free_bytes'], 0)
        self.assertFalse(self.case.exists())

    def test_idle_case_reads_counts_without_creating_journals(self):
        with Case(self.case) as case:
            case.result('m1', 'reconstructed', {}, kind='ordinary')
            case.result('m2', 'awaiting_deep', {}, kind='ordinary')
        before = {p.name: p.read_bytes() for p in self.case.iterdir() if p.is_file()}
        observed = snapshot(self.case)
        self.assertIsNone(observed['error'])
        self.assertIs(observed['busy'], False)
        self.assertEqual(observed['jobs'], {'ordinary/reconstructed': 1, 'ordinary/awaiting_deep': 1})
        self.assertEqual(observed['sources'], 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.case.iterdir() if p.is_file()})

    def test_busy_case_reads_committed_jobs_and_closes_reader(self):
        with Case(self.case) as case:
            case.result('m1', 'reconstructed', {}, kind='ordinary')
            observed = snapshot(self.case)
            self.assertIs(observed['busy'], True)
            self.assertIsNone(observed['error'])
            self.assertEqual(observed['jobs'], {'ordinary/reconstructed': 1})
            case.result('m2', 'awaiting_deep', {}, kind='ordinary')
            self.assertEqual(snapshot(self.case)['jobs']['ordinary/awaiting_deep'], 1)
            # No observer read transaction remains to pin committed work.
            busy, frames, copied = case.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            self.assertEqual((busy, frames, copied), (0, 0, 0))

    def test_busy_case_missing_journals_is_not_initialized_by_viewer(self):
        import fcntl
        with Case(self.case):
            pass
        before = sorted(p.name for p in self.case.iterdir())
        with (self.case / '.lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed = snapshot(self.case)
            self.assertIs(observed['busy'], True)
            self.assertIsNotNone(observed['error'])
            self.assertEqual(observed['jobs'], {})
        self.assertEqual(before, sorted(p.name for p in self.case.iterdir()))

    def test_tails_are_bounded_and_terminal_controls_removed(self):
        with Case(self.case):
            pass
        log = self.case / 'run.log'
        log.write_text('discarded' * 20000 + '\n' + '\n'.join(f'line {n}' for n in range(20)) + '\nunsafe\x1b[31m\x00text\n')
        first = snapshot(self.case)
        self.assertEqual(len(first['log_lines']), 12)
        self.assertIn('line 19', first['log_lines'])
        self.assertNotIn('\x1b', ''.join(first['log_lines']))
        self.assertEqual(first['log_mtime'], log.stat().st_mtime)
        with log.open('a') as stream:
            stream.write('new progress\n')
        self.assertEqual(snapshot(self.case)['log_lines'][-1], 'new progress')

    def test_log_remains_available_when_database_invalid(self):
        self.case.mkdir()
        (self.case / '.lock').touch()
        (self.case / 'case.sqlite3').write_text('not sqlite')
        (self.case / 'run.log').write_text('progress retained\n')
        observed = snapshot(self.case)
        self.assertIsNotNone(observed['error'])
        self.assertEqual(observed['log_lines'], ['progress retained'])

    def test_symlink_case_is_not_followed(self):
        with Case(self.case):
            pass
        alias = self.root / 'alias'
        alias.symlink_to(self.case)
        self.assertIsNotNone(snapshot(alias)['error'])

    def test_attempt_table_is_never_read(self):
        with Case(self.case) as case:
            case.result('m1', 'reconstructed', {}, kind='ordinary')
        real_connect = sqlite3.connect
        statements = []
        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection
        with patch('videorebuild.tui_model.sqlite3.connect', side_effect=connect):
            self.assertIsNone(snapshot(self.case)['error'])
        self.assertFalse(any('attempts' in query.lower() for query in statements))


if __name__ == '__main__':
    unittest.main()
