"""Association checkpoints retain exact witnesses without trusting partial coverage."""
from contextlib import closing, contextmanager
from dataclasses import replace
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_association_io_parity import TOKEN, exhaustive_hits, movie_with_audio
from videorebuild import engine
from videorebuild import association_resume
from videorebuild.candidate_tiers import CandidateTiers
from videorebuild.ranges import AllocationScope
from videorebuild.storage import Case, ReaderPool, SafetyError, file_sha256, scan_logical, write_json


class AssociationResumeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root/'source.bin'
        self.data = (b'abc'+TOKEN+b'pq'+b'ZZZ')*24
        self.source.write_bytes(self.data)
        self.case_path = self.root/'case'
        self.movies = [movie_with_audio('one', 100), movie_with_audio('alias', 200)]
        self.options = engine.Options(deep=True, reconstruct_only=True)
        with Case(self.case_path) as case:
            self.sid = case.register_source(self.source, 'image')
            scan_logical(case, self.sid)

    @contextmanager
    def opened(self, **pool_options):
        with Case(self.case_path) as case, ReaderPool(case.sources(), **pool_options) as pool:
            yield case, pool

    def associate(self, case, pool, movies=None, *, run_key='resume-test', options=None, retain=False):
        rows = []

        def capture(search, groups, *args):
            keys = {group.key for group in groups}
            rows.extend(tuple(row) for row in search.execute(
                'SELECT movie,g,fingerprint,sid,start FROM hits ORDER BY movie,g,fingerprint,sid,start')
                        if retain or (row[0], row[1]) in keys)
            if retain:
                raise KeyboardInterrupt
            yield from ()

        with patch.object(engine, '_content_assemblies', capture):
            if retain:
                with self.assertRaises(KeyboardInterrupt):
                    engine.deep_search(case, movies or self.movies, pool, {}, options or self.options, run_key)
            else:
                engine.deep_search(case, movies or self.movies, pool, {}, options or self.options, run_key)
        return sorted(rows)

    def expected(self, movies, pool):
        return [row[:5] for row in exhaustive_hits(movies, pool, {self.sid: self.data})]

    def test_graceful_interrupt_resumes_exact_hits_without_rehashing_saved_prefix(self):
        original = engine._group_hash
        before, after = [], []

        def interrupt(group, mapping, pool, **kwargs):
            if len(before) == 9:
                raise KeyboardInterrupt
            value = original(group, mapping, pool, **kwargs)
            before.append(tuple(mapping.values()))
            return value

        with self.opened() as (case, pool), patch.object(engine, '_group_hash', interrupt), \
                patch.object(association_resume, 'PAGE_ROWS', 4):
            with self.assertRaises(KeyboardInterrupt):
                self.associate(case, pool)
            self.assertTrue(all(case.job('ordinary', movie.digest) is None for movie in self.movies))

        def resumed(group, mapping, pool, **kwargs):
            after.append(tuple(mapping.values()))
            return original(group, mapping, pool, **kwargs)

        with self.opened() as (case, pool), patch.object(engine, '_group_hash', resumed):
            observed = self.associate(case, pool)
            self.assertEqual(observed, self.expected(self.movies, pool))
            self.assertLess(len(after), 24)
            self.assertFalse(set(before[:8]) & set(after), 'committed prefix was hashed again')
            self.assertIn(before[8], after, 'uncommitted candidate was not replayed')
            for movie in self.movies:
                self.assertEqual(case.job('ordinary', movie.digest)['detail']['hit_counts'], [24])
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_completed_associations_survive_finished_movie_leaving_batch(self):
        calls = []

        def stop_before_second_movie(search, groups, *args):
            calls.append(groups[0].movie.digest)
            if len(calls) == 2:
                raise KeyboardInterrupt
            yield from ()

        with self.opened() as (case, pool), patch.object(engine, '_content_assemblies', stop_before_second_movie):
            with self.assertRaises(KeyboardInterrupt):
                engine.deep_search(case, self.movies, pool, {}, self.options, 'resume-test')
            self.assertIsNotNone(case.job('ordinary', self.movies[0].digest))
            self.assertIsNone(case.job('ordinary', self.movies[1].digest))
        with self.opened() as (case, pool), patch.object(
                engine, '_group_hash', side_effect=AssertionError('completed association was hashed again')):
            remaining = self.movies[1:]
            self.assertEqual(self.associate(case, pool, remaining), self.expected(remaining, pool))
        self.assertFalse((self.case_path/'work'/'association-checkpoint.sqlite3').exists())

    def test_completed_batch_reclaims_geometry_of_finished_movie_absent_on_resume(self):
        self.movies[0] = movie_with_audio('different-geometry', 100, gap=2)
        calls = []

        def stop_before_second_movie(search, groups, *args):
            calls.append(groups[0].movie.digest)
            if len(calls) == 2:
                raise KeyboardInterrupt
            yield from ()

        with self.opened() as (case, pool), patch.object(engine, '_content_assemblies', stop_before_second_movie):
            with self.assertRaises(KeyboardInterrupt):
                engine.deep_search(case, self.movies, pool, {}, self.options, 'resume-test')
        with self.opened() as (case, pool), patch.object(
                engine, '_group_hash', side_effect=AssertionError('remaining completed geometry was hashed again')):
            remaining = self.movies[1:]
            self.assertEqual(self.associate(case, pool, remaining), self.expected(remaining, pool))
        self.assertFalse((self.case_path/'work'/'association-checkpoint.sqlite3').exists(),
                         'finished old geometry retained disk space after the batch completed')

    def test_linked_checkpoint_journal_cannot_write_outside_case(self):
        with self.opened() as (case, pool):
            self.associate(case, pool, retain=True)
        checkpoint = self.case_path/'work'/'association-checkpoint.sqlite3'
        outside = self.root/'outside.bin'
        outside.write_bytes(b'preserve unrelated bytes')
        checkpoint.with_name(checkpoint.name+'-journal').symlink_to(outside)
        with self.opened() as (case, pool):
            with self.assertRaises(SafetyError):
                self.associate(case, pool)
        self.assertEqual(outside.read_bytes(), b'preserve unrelated bytes')

    def test_completed_batch_cleanup_needs_no_checkpoint_writes_or_free_space(self):
        original_save = association_resume.AssociationCheckpoint.save
        original_finish = engine.finish
        checkpoints, finished = [], []
        space = {'available': True}

        def save(checkpoint):
            original_save(checkpoint)
            checkpoints.append(checkpoint)

        def no_writes(action, *args):
            return sqlite3.SQLITE_DENY if action in {
                sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
            } else sqlite3.SQLITE_OK

        def finish(*args, **kwargs):
            result = original_finish(*args, **kwargs)
            finished.append(args[1].digest)
            if len(finished) == len(self.movies):
                self.assertTrue(checkpoints)
                space['available'] = False
                checkpoints[-1].db.set_authorizer(no_writes)
            return result

        def require_space(*args):
            if not space['available']:
                raise SafetyError('synthetic full volume after durable results')

        with self.opened() as (case, pool), \
                patch.object(association_resume.AssociationCheckpoint, 'save', save), \
                patch.object(association_resume, 'require_search_space', require_space), \
                patch.object(engine, 'finish', finish):
            observed = self.associate(case, pool)
            self.assertEqual(observed, self.expected(self.movies, pool))
        self.assertFalse((self.case_path/'work'/'association-checkpoint.sqlite3').exists())

    def test_changed_run_key_recomputes_associations(self):
        with self.opened() as (case, pool):
            self.associate(case, pool, retain=True)
        with self.opened() as (case, pool), patch.object(engine, '_group_hash', wraps=engine._group_hash) as hashed:
            observed = self.associate(case, pool, run_key='changed-options')
            self.assertEqual(observed, self.expected(self.movies, pool))
            self.assertTrue(hashed.call_count)

    def test_scope_replacement_does_not_rewrite_old_checkpoint(self):
        with self.opened() as (case, pool):
            original = self.associate(case, pool, retain=True)
        checkpoint = self.case_path/'work'/'association-checkpoint.sqlite3'
        # Pin a read transaction on the old file: replacing a disposable
        # generation needs no bulk write/journal against that old database.
        with closing(sqlite3.connect(checkpoint.as_uri()+'?mode=ro', uri=True)) as old:
            old.execute('BEGIN')
            old.execute('SELECT name FROM sqlite_schema').fetchall()
            with self.opened() as (case, pool):
                observed = self.associate(case, pool, run_key='new-scope', retain=True)
                self.assertEqual(observed, original)
                self.assertEqual(observed, self.expected(self.movies, pool))

    def test_sqlite_full_preserves_last_atomic_hit_and_cursor_page(self):
        count = 512
        self.data = (b'abc'+TOKEN+b'pq'+b'ZZZ')*count
        self.source.write_bytes(self.data)
        self.case_path = self.root/'full-case'
        with Case(self.case_path) as case:
            self.sid = case.register_source(self.source, 'image')
            scan_logical(case, self.sid)
        original = association_resume.AssociationCheckpoint.save
        saved = []

        def limit_after_save(checkpoint):
            original(checkpoint)
            saved.append(sum(state[2] for state in checkpoint.states.values()))
            if len(saved) == 1:
                pages = checkpoint.db.execute('PRAGMA page_count').fetchone()[0]
                checkpoint.db.execute(f'PRAGMA max_page_count={pages}')

        with self.opened() as (case, pool), patch.object(association_resume, 'PAGE_ROWS', 16), \
                patch.object(association_resume.AssociationCheckpoint, 'save', limit_after_save):
            with self.assertRaises(SafetyError) as full:
                self.associate(case, pool)
            self.assertEqual(getattr(full.exception.__cause__, 'sqlite_errorcode', None), sqlite3.SQLITE_FULL)
            self.assertTrue(saved)
            self.assertLess(saved[-1], count)
            self.assertTrue(all(case.job('ordinary', movie.digest) is None for movie in self.movies))
        with self.opened() as (case, pool), patch.object(engine, '_group_hash', wraps=engine._group_hash) as hashed:
            observed = self.associate(case, pool)
            self.assertEqual(observed, self.expected(self.movies, pool))
            self.assertEqual(hashed.call_count, count-saved[-1])
            offsets = [next(iter(call.args[1].values()))[1] for call in hashed.call_args_list]
            self.assertEqual(min(offsets), saved[-1]*16)

    def test_hard_process_death_replays_only_uncommitted_page(self):
        script = '''
import sys
from pathlib import Path
from unittest.mock import patch
from test_association_io_parity import movie_with_audio
from videorebuild import association_resume, engine
from videorebuild.storage import Case, ReaderPool
association_resume.PAGE_ROWS = 4
original = engine._group_hash
calls = 0
def fingerprint(group, mapping, pool, **kwargs):
    global calls
    calls += 1
    if calls == 7:
        print('uncommitted-page-ready', flush=True)
        sys.stdin.readline()
        raise AssertionError('parent should terminate this process')
    return original(group, mapping, pool, **kwargs)
def no_assemblies(*args):
    yield from ()
with Case(Path(sys.argv[1])) as case, ReaderPool(case.sources()) as pool, \\
        patch.object(engine, '_group_hash', fingerprint), \\
        patch.object(engine, '_content_assemblies', no_assemblies):
    engine.deep_search(case, [movie_with_audio('one', 100), movie_with_audio('alias', 200)],
                       pool, {}, engine.Options(deep=True, reconstruct_only=True), 'resume-test')
'''
        repository = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment['PYTHONPATH'] = os.pathsep.join((str(repository/'src'), str(repository/'tests')))
        child = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.case_path)],
                                 cwd=repository, env=environment, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                ready = selector.select(timeout=20)
            self.assertTrue(ready, 'child did not reach the synchronized partial-page checkpoint')
            line = child.stdout.readline()
            if line != 'uncommitted-page-ready\n':
                _, stderr = child.communicate(timeout=10)
                self.fail(f'child failed before synchronized checkpoint: {line!r}\n{stderr}')
            child.kill()
            _, stderr = child.communicate(timeout=10)
            self.assertEqual(child.returncode, -signal.SIGKILL, stderr)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()
        visited = []
        original = engine._group_hash

        def fingerprint(group, mapping, pool, **kwargs):
            visited.append(next(iter(mapping.values()))[1])
            return original(group, mapping, pool, **kwargs)

        with self.opened() as (case, pool), patch.object(engine, '_group_hash', fingerprint):
            self.assertEqual(self.associate(case, pool), self.expected(self.movies, pool))
            self.assertEqual(len(visited), 20)
            self.assertEqual(min(visited), 64)

    def claim_range(self, case, pool, offset, length):
        candidate = case.root/'candidates'/'claimed.bin'
        output = case.root/'reconstructed'/'claimed.bin'
        candidate.write_bytes(self.data[offset:offset+length])
        output.write_bytes(self.data[offset:offset+length])
        write_json(candidate.with_suffix('.json'), {'provenance': {'chunks': [
            {'physical_extents': pool.provenance(self.sid, offset, length)}]}})
        case.result('claimed', 'reconstructed', {'outputs': [str(output.relative_to(case.root))],
            'candidates': [{'path': str(candidate.relative_to(case.root)), 'sha256': file_sha256(candidate)}]})

    def claim_first_location(self, case, pool):
        self.claim_range(case, pool, 3, len(TOKEN))

    def test_zero_hit_page_is_durable_without_claiming_incomplete_coverage(self):
        original = association_resume.AssociationCheckpoint.save

        def interrupt_after_commit(checkpoint):
            original(checkpoint)
            raise KeyboardInterrupt

        with self.opened() as (case, pool):
            self.claim_range(case, pool, 0, len(self.data))
            view = CandidateTiers(case, pool).view()
            with patch.object(association_resume, 'PAGE_ROWS', 4), patch.object(
                    association_resume.AssociationCheckpoint, 'save', interrupt_after_commit):
                with self.assertRaises(KeyboardInterrupt):
                    self.associate(case, view)
            self.assertTrue(all(case.job('ordinary', movie.digest) is None for movie in self.movies))
        with self.opened() as (case, pool):
            view = CandidateTiers(case, pool).view()
            with patch.object(view, 'permits', wraps=view.permits) as permitted, patch.object(
                    engine, '_group_hash', side_effect=AssertionError('blocked bytes were hashed')):
                self.assertEqual(self.associate(case, view), [])
            self.assertTrue(permitted.call_count)
            self.assertTrue(all(call.args[1] >= 64 for call in permitted.call_args_list))
            for movie in self.movies:
                job = case.job('ordinary', movie.digest)
                self.assertEqual(job['detail']['hit_counts'], [0])
                self.assertFalse(job['detail']['search_limited'])

    def test_complete_zero_hit_checkpoint_rechecks_sources_before_negative_result(self):
        with self.opened() as (case, pool):
            self.claim_range(case, pool, 0, len(self.data))
            view = CandidateTiers(case, pool).view()
            with patch.object(engine, 'finish', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.associate(case, view)
        before = self.source.stat()
        self.source.write_bytes(self.data.replace(b'ABC', b'DEF'))
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.opened() as (case, pool):
            with self.assertRaises(SafetyError):
                view = CandidateTiers(case, pool).view()
                self.associate(case, view)
            self.assertTrue(all(case.job('ordinary', movie.digest) is None for movie in self.movies))

    def test_changed_owner_eligibility_does_not_reuse_blocked_or_missing_hits(self):
        with self.opened() as (case, pool):
            self.claim_first_location(case, pool)
            tiers = CandidateTiers(case, pool)
            unclaimed = tiers.view()
            blocked = self.associate(case, unclaimed, retain=True)
            self.assertEqual(blocked, self.expected(self.movies, unclaimed))
            owner = tiers.view(('ordinary', 'claimed'))
            permitted = self.associate(case, owner, retain=True)
            self.assertEqual(permitted, self.expected(self.movies, owner))
            self.assertGreater(len(permitted), len(blocked))
            self.assertEqual(self.associate(case, tiers.view()), blocked)

    def test_new_publication_invalidates_prior_unclaimed_scope(self):
        with self.opened() as (case, pool):
            original_view = CandidateTiers(case, pool).view()
            original = self.associate(case, original_view, retain=True)
            self.claim_first_location(case, pool)
            changed = CandidateTiers(case, pool).view()
            with patch.object(engine, '_group_hash', wraps=engine._group_hash) as hashed:
                observed = self.associate(case, changed)
            self.assertEqual(observed, self.expected(self.movies, changed))
            self.assertLess(len(observed), len(original))
            self.assertTrue(hashed.call_count, 'changed eligibility reused stale completeness')

    def test_changed_allocation_scope_cannot_keep_out_of_scope_hits(self):
        with self.opened() as (case, pool):
            self.associate(case, pool, retain=True)
        allocation = AllocationScope({self.sid: ((16, len(self.data)-16),)}, 'restricted-allocation')
        with self.opened(allocation_scope=allocation) as (case, pool):
            observed = self.associate(case, pool)
            self.assertEqual(observed, self.expected(self.movies, pool))
            self.assertTrue(all(row[4] >= 16 for row in observed))

    def test_same_mtime_source_rewrite_cannot_reuse_saved_associations(self):
        with self.opened() as (case, pool):
            self.associate(case, pool, retain=True)
        before = self.source.stat()
        changed = self.data.replace(b'ABC', b'DEF')
        self.source.write_bytes(changed)
        os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.opened() as (case, pool):
            with self.assertRaises(SafetyError):
                self.associate(case, pool)
        self.assertEqual(self.source.read_bytes(), changed)

    def test_capped_association_keeps_explicit_search_limited_semantics(self):
        with self.opened() as (case, pool):
            self.associate(case, pool, retain=True)
        options = replace(self.options, max_anchor_checks=1)
        with self.opened() as (case, pool):
            self.associate(case, pool, options=options, run_key='capped')
            for movie in self.movies:
                job = case.job('ordinary', movie.digest)
                self.assertEqual(job['status'], 'search_limited')
                self.assertFalse(job['detail']['outputs'])


if __name__ == '__main__':
    unittest.main()
