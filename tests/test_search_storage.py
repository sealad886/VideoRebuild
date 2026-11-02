"""Search scratch belongs to the selected case, never the ambient temp disk."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_deep_witnesses import structural_movie
from videorebuild import engine
from videorebuild.storage import Case, SafetyError, scan_logical


class SearchStorageTests(unittest.TestCase):
    def test_case_query_temporary_state_is_connection_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / 'unrelated.sqlite3')) as unrelated:
                prior = unrelated.execute('PRAGMA temp_store').fetchone()[0]
                with Case(root / 'case') as case:
                    self.assertEqual(case.db.execute('PRAGMA temp_store').fetchone()[0], 2)
                    main = Path(case.db.execute('PRAGMA database_list').fetchone()[2])
                    self.assertEqual(main, case.root / 'case.sqlite3')
                    case.event('scratch_fixture', {'retained': True})
                    case.db.execute('CREATE TEMP TABLE transient(value)')
                    case.db.execute('INSERT INTO transient VALUES(1)')
                    temp = next(row for row in case.db.execute('PRAGMA database_list') if row[1] == 'temp')
                    self.assertEqual(temp[2], '')
                self.assertEqual(unrelated.execute('PRAGMA temp_store').fetchone()[0], prior)
            with closing(sqlite3.connect(main)) as reopened:
                self.assertEqual(reopened.execute("SELECT count(*) FROM events WHERE kind='scratch_fixture'").fetchone()[0], 1)
                self.assertEqual(reopened.execute("SELECT count(*) FROM sqlite_temp_master WHERE name='transient'").fetchone()[0], 0)

    def test_case_disk_storage_ignores_ambient_temp_and_keeps_main_tables_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ambient = root / 'ambient'
            ambient.mkdir()
            with Case(root / 'case') as case, \
                    patch.dict(os.environ, {'TMPDIR': str(ambient), 'SQLITE_TMPDIR': str(ambient)}), \
                    patch.object(tempfile, 'tempdir', str(ambient)):
                with engine._search_store(case) as db:
                    database = Path(db.execute('PRAGMA database_list').fetchone()[2])
                    self.assertEqual(database.parent.parent, case.root / 'work')
                    self.assertEqual(database.stat().st_dev, case.root.stat().st_dev)
                    # Search cardinality remains disk-backed; only auxiliary
                    # SQLite TEMP structures use memory to avoid host spill.
                    db.execute('CREATE TABLE candidates(payload BLOB)')
                    db.executemany('INSERT INTO candidates VALUES(?)', [(b'x' * 1024,)] * 5000)
                    db.commit()
                    self.assertGreater(database.stat().st_size, 4 * 1024 * 1024)
                    self.assertEqual(db.execute('PRAGMA temp_store').fetchone()[0], 2)
                    self.assertEqual(os.environ['TMPDIR'], str(ambient))
                    self.assertEqual(os.environ['SQLITE_TMPDIR'], str(ambient))
                    self.assertEqual(tempfile.tempdir, str(ambient))
                    self.assertEqual(list(ambient.iterdir()), [])
                self.assertFalse(database.parent.exists())

    def test_separate_cases_have_separate_search_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Case(root / 'first') as first, Case(root / 'second') as second:
                with engine._search_store(first) as a, engine._search_store(second) as b:
                    paths = [Path(db.execute('PRAGMA database_list').fetchone()[2]) for db in (a, b)]
                    self.assertEqual(paths[0].parent.parent, first.root / 'work')
                    self.assertEqual(paths[1].parent.parent, second.root / 'work')
                    a.execute('CREATE TABLE candidates(value)')
                    self.assertEqual(b.execute("SELECT count(*) FROM sqlite_master WHERE name='candidates'").fetchone()[0], 0)
                self.assertTrue(all(not path.parent.exists() for path in paths))

    def test_exception_closes_database_and_removes_only_its_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with Case(Path(directory) / 'case') as case:
                retained = case.root / 'work' / 'retained'
                retained.write_text('keep')
                with self.assertRaisesRegex(RuntimeError, 'interrupted search'):
                    with engine._search_store(case) as db:
                        database = Path(db.execute('PRAGMA database_list').fetchone()[2])
                        db.execute('CREATE TABLE candidates(value)')
                        raise RuntimeError('interrupted search')
                self.assertFalse(database.parent.exists())
                self.assertEqual(retained.read_text(), 'keep')
                with self.assertRaises(sqlite3.ProgrammingError):
                    db.execute('SELECT 1')

    def test_unsafe_work_path_is_rejected_without_writing_elsewhere(self):
        for symlink in (True, False):
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                outside = root / 'outside'
                outside.mkdir()
                case = Case(root / 'case')
                case.root.mkdir()
                work = case.root / 'work'
                if symlink:
                    work.symlink_to(outside, target_is_directory=True)
                else:
                    work.write_text('keep')
                with self.assertRaises(SafetyError):
                    with engine._search_store(case):
                        self.fail('Unsafe work directory accepted')
                self.assertEqual(list(outside.iterdir()), [])
                if not symlink:
                    self.assertEqual(work.read_text(), 'keep')

    def test_recovery_whole_and_batched_deep_search_use_case_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            targets = [structural_movie([100+i, 200+i], [8, 8]) for i in range(2)]
            source = root / 'tokens.bin'
            source.write_bytes(b''.join(movie.data for movie in targets) +
                               b'\0\0\0\x04\x65ABC' + b'Z' * 8 + b'\0\0\0\x04\x65ABC')
            with Case(root / 'case') as case:
                scan_logical(case, case.register_source(source))
                paths = []
                connect = sqlite3.connect
                def observed_connect(*args, **kwargs):
                    db = connect(*args, **kwargs)
                    paths.append(Path(db.execute('PRAGMA database_list').fetchone()[2]))
                    return db
                with patch.object(engine.sqlite3, 'connect', side_effect=observed_connect):
                    engine.recover(case, engine.Options(deep=True, reconstruct_only=True, max_groups=1))
                self.assertGreaterEqual(len(paths), 5)
                self.assertTrue(all(path.parent.parent == case.root / 'work' for path in paths))
                self.assertTrue(all(not path.parent.exists() for path in paths))
                self.assertEqual(len(list(case.get_jobs('ordinary'))), len(targets))


if __name__ == '__main__':
    unittest.main()
