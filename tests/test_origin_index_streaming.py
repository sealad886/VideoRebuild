"""Whole-payload origin reuse keeps ordered alternatives and qualified coverage."""
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

import test_whole_origin_independent as origin_fixtures
from videorebuild import origin_index
from videorebuild.bmff import FormatError, valid_sample
from videorebuild.origin_index import OriginIndex
from videorebuild.storage import ReaderPool, SafetyError


class OriginIndexStreamingTests(unittest.TestCase):
    def fixture(self):
        return origin_fixtures.WholeOriginIndependentTests().fixture()

    @staticmethod
    def expected(pool, origins, relative, length):
        rows = []
        for sid, offset, header, payload in origins:
            try:
                if (pool.permits(sid, offset, header) and
                        valid_sample(pool.read(sid, offset+header+relative, length+4), b'avc1', 4)):
                    rows.append((sid, offset, header, payload))
            except FormatError:
                pass
        return sorted(rows, key=lambda row: (row[3], row[0], row[1]))

    def legacy_cache(self, case, pool):
        # A completed old negative deliberately disagrees with the actual
        # source. Migration must rebuild coverage, never adopt this receipt.
        with OriginIndex(case, pool) as index:
            index.prepare([(0, 4)])
            path = index.path
        with closing(sqlite3.connect(path)) as database:
            identity = json.loads(database.execute('SELECT value FROM identity').fetchone()[0])[0]
            with database:
                database.execute('DROP TABLE matches')
                database.execute('''CREATE TABLE matches(relative INTEGER,length INTEGER,
                    sid INTEGER,offset INTEGER,PRIMARY KEY(relative,length,sid,offset)) WITHOUT ROWID''')
                database.execute('UPDATE identity SET value=?', (identity,))
                database.execute('PRAGMA user_version=0')
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_actual_candidate_query_streams_all_ordered_origins_without_sorter(self):
        test = self

        class AuditedDatabase:
            def __init__(self, database):
                self.database = database
                self.inspected = 0

            def __getattr__(self, name):
                return getattr(self.database, name)

            def execute(self, query, parameters=()):
                if query.lstrip().upper().startswith('SELECT') and 'matches' in query:
                    instructions = self.database.execute('EXPLAIN '+query, parameters).fetchall()
                    forbidden = {'SorterOpen', 'SorterSort', 'Sort', 'OpenEphemeral', 'OpenAutoindex'}
                    bad = {row[1] for row in instructions} & forbidden
                    test.assertFalse(bad, f'origin domain materialized in memory: {bad}\n{query}')
                    self.inspected += 1
                return self.database.execute(query, parameters)

        with self.fixture() as (case, origins), ReaderPool(case.sources()) as pool:
            profiles = [(0, 4), (5, 4), (12, 6)]
            with OriginIndex(case, pool) as index:
                index.prepare(profiles)
                audited = AuditedDatabase(index.db)
                index.db = audited
                for profile in profiles:
                    self.assertEqual(list(index.candidates(*profile)), self.expected(pool, origins, *profile))
                self.assertEqual(audited.inspected, len(profiles))

    def test_cold_and_reopened_coverage_preserve_every_ordered_alternative(self):
        with self.fixture() as (case, origins):
            for visit in range(2):
                with ReaderPool(case.sources()) as pool, OriginIndex(case, pool) as index:
                    index.prepare([(0, 4)])
                    observed = list(index.candidates(0, 4))
                    self.assertEqual(observed, self.expected(pool, origins, 0, 4))
                    self.assertTrue(observed)
                    if visit:
                        self.assertEqual(index.stats['origin_starts_checked'], 0)
                        self.assertEqual(index.stats['origin_profiles_reused'], 1)

    def test_old_negative_coverage_is_backed_up_and_rebuilt(self):
        with self.fixture() as (case, origins), ReaderPool(case.sources()) as pool:
            path, old_hash = self.legacy_cache(case, pool)
            with OriginIndex(case, pool) as index:
                index.prepare([(0, 4)])
                self.assertEqual(list(index.candidates(0, 4)), self.expected(pool, origins, 0, 4))
                self.assertGreater(index.stats['origin_starts_checked'], 0)
                self.assertEqual(index.stats['origin_profiles_reused'], 0)
            backup = path.with_name(path.name+'.v1.bak')
            self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), old_hash)
            with OriginIndex(case, pool) as index:
                index.prepare([(0, 4)])
                self.assertEqual(index.stats['origin_starts_checked'], 0)

    def test_interruption_after_backup_resumes_without_overwriting_backup(self):
        with self.fixture() as (case, origins), ReaderPool(case.sources()) as pool:
            path, old_hash = self.legacy_cache(case, pool)
            original = sqlite3.connect
            calls = 0

            def interrupt_new_database(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt
                return original(*args, **kwargs)

            with patch.object(origin_index.sqlite3, 'connect', interrupt_new_database):
                with self.assertRaises(KeyboardInterrupt):
                    with OriginIndex(case, pool):
                        self.fail('new index creation should have been interrupted')
            backup = path.with_name(path.name+'.v1.bak')
            self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), old_hash)
            with OriginIndex(case, pool) as index:
                index.prepare([(0, 4)])
                self.assertEqual(list(index.candidates(0, 4)), self.expected(pool, origins, 0, 4))
            self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), old_hash)

    def test_backup_collision_preserves_both_owned_artifacts(self):
        with self.fixture() as (case, _), ReaderPool(case.sources()) as pool:
            path, old_hash = self.legacy_cache(case, pool)
            backup = path.with_name(path.name+'.v1.bak')
            backup.write_bytes(b'retained backup evidence')
            with self.assertRaises(SafetyError):
                with OriginIndex(case, pool):
                    self.fail('existing backup must not be overwritten')
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), old_hash)
            self.assertEqual(backup.read_bytes(), b'retained backup evidence')

    def test_interrupted_schema_creation_restarts_from_preserved_backup(self):
        with self.fixture() as (case, origins), ReaderPool(case.sources()) as pool:
            path, old_hash = self.legacy_cache(case, pool)
            original = sqlite3.connect

            class InterruptSchema(sqlite3.Connection):
                tables_created = 0

                def execute(self, query, *args):
                    if query.lstrip().upper().startswith('CREATE TABLE'):
                        self.tables_created += 1
                        if self.tables_created == 3:
                            raise KeyboardInterrupt
                    return super().execute(query, *args)

            def connect(*args, **kwargs):
                return original(*args, factory=InterruptSchema, **kwargs)

            with patch.object(origin_index.sqlite3, 'connect', connect):
                with self.assertRaises(KeyboardInterrupt):
                    with OriginIndex(case, pool):
                        self.fail('partial schema must not be accepted')
            backup = path.with_name(path.name+'.v1.bak')
            self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), old_hash)
            with OriginIndex(case, pool) as index:
                index.prepare([(0, 4)])
                self.assertEqual(list(index.candidates(0, 4)), self.expected(pool, origins, 0, 4))
            self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), old_hash)

    def test_unsafe_journal_is_rejected_before_sqlite_opens_it(self):
        with self.fixture() as (case, _), ReaderPool(case.sources()) as pool:
            with OriginIndex(case, pool) as index:
                path = index.path
            protected = case.root/'protected-evidence'
            protected.write_bytes(b'preserved')
            journal = path.with_name(path.name+'-journal')
            journal.symlink_to(protected)
            with self.assertRaises(SafetyError):
                with OriginIndex(case, pool):
                    self.fail('linked journal must not be opened')
            self.assertEqual(protected.read_bytes(), b'preserved')

    def test_warm_negative_coverage_rejects_changed_live_identity_in_new_reader(self):
        with self.fixture() as (case, _):
            with ReaderPool(case.sources()) as pool, OriginIndex(case, pool) as index:
                index.prepare([(0, 1000)])
                self.assertEqual(list(index.candidates(0, 1000)), [])
                path = index.path
            source = next(iter(case.sources().values()))['path']
            previous = os.stat(source)
            with open(source, 'r+b') as stream:
                data = stream.read()
                stream.seek(0)
                stream.write(data)
            os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            self.assertNotEqual(os.stat(source).st_ctime_ns, previous.st_ctime_ns)
            with ReaderPool(case.sources()) as pool, self.assertRaises(SafetyError):
                with OriginIndex(case, pool):
                    self.fail('saved negative must not certify another source lifetime')
            self.assertTrue(path.is_file())


if __name__ == '__main__':
    unittest.main()
