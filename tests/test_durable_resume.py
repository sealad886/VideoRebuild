"""Durable recovery resumes skip finished stages without losing pending work."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from videorebuild import engine
from videorebuild.storage import Case, SafetyError, scan_logical
from videorebuild.ranges import AllocationScope
from test_deep_witnesses import structural_movie
import test_ordinary_tiers as tier_cases


class DurableResumeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.case_path = self.root / 'case'
        self.movies = [structural_movie([1000 + index * 100], [8]) for index in range(3)]
        self.source = self.root / 'metadata.bin'
        self.source.write_bytes(b''.join(movie.data for movie in self.movies) + b'Z' * 32)
        with Case(self.case_path) as case:
            scan_logical(case, case.register_source(self.source))
        self.options = engine.Options(deep=True, reconstruct_only=True)

    @staticmethod
    def whole_miss(case, movie, pool, tools, options, run_key, *, limited=False):
        status = engine.finish(case, movie, [], limited, 'whole_payload', run_key,
                               {'tested_hypotheses': 0})
        return status, [], limited

    @staticmethod
    def complete_deep(case, movies, pool, tools, options, run_key, incomplete, **kwargs):
        for movie in movies:
            engine.finish(case, movie, [], movie.digest in incomplete,
                          'scattered_extents', run_key, {'tested_assemblies': 0})

    def test_interrupted_preliminary_pass_skips_completed_misses_with_retry(self):
        first_calls = []

        def interrupted(case, movie, *args):
            first_calls.append(movie.digest)
            if len(first_calls) == 2:
                raise KeyboardInterrupt
            return self.whole_miss(case, movie, *args)

        with Case(self.case_path) as case, patch.object(engine, 'whole_payload', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                engine.recover(case, self.options, retry=True)
        resumed_calls = []

        def resumed(case, movie, *args):
            resumed_calls.append(movie.digest)
            return self.whole_miss(case, movie, *args)

        with Case(self.case_path) as case, patch.object(engine, 'whole_payload', side_effect=resumed), \
                patch.object(engine, '_deep_batch', side_effect=self.complete_deep):
            engine.recover(case, self.options, retry=True)
            self.assertEqual({case.job('ordinary', m.digest)['detail']['stage'] for m in self.movies},
                             {'scattered_extents'})
        self.assertNotIn(first_calls[0], resumed_calls)
        self.assertEqual(set(resumed_calls), {m.digest for m in self.movies} - {first_calls[0]})
        self.assertEqual(len(resumed_calls), 2)

    def test_interruption_after_waiting_result_commit_does_not_lose_deep_work(self):
        with Case(self.case_path) as case:
            original = case.result
            completed = []

            def stop_after_commit(key, status, detail, **kwargs):
                original(key, status, detail, **kwargs)
                if status == 'awaiting_deep':
                    completed.append(key)
                    raise KeyboardInterrupt

            with patch.object(engine, 'whole_payload', side_effect=self.whole_miss), \
                    patch.object(case, 'result', side_effect=stop_after_commit):
                with self.assertRaises(KeyboardInterrupt):
                    engine.recover(case, self.options, retry=True)
        whole_calls = []
        deep_calls = []

        def whole(case, movie, *args):
            whole_calls.append(movie.digest)
            return self.whole_miss(case, movie, *args)

        def deep(case, movies, *args, **kwargs):
            deep_calls.extend(movie.digest for movie in movies)
            return self.complete_deep(case, movies, *args, **kwargs)

        with Case(self.case_path) as case, patch.object(engine, 'whole_payload', side_effect=whole), \
                patch.object(engine, '_deep_batch', side_effect=deep):
            engine.recover(case, self.options, retry=True)
        self.assertEqual(len(completed), 1)
        self.assertNotIn(completed[0], whole_calls)
        self.assertEqual(Counter(deep_calls), Counter(movie.digest for movie in self.movies))

    def test_same_scope_legacy_waiting_job_is_adopted_without_preliminary_replay(self):
        with Case(self.case_path) as case:
            engine.recover(case, self.options, limit=0)
            detail = json.loads(case.db.execute(
                "SELECT detail FROM events WHERE kind='recovery_started' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])
            target = min(self.movies, key=lambda m: m.digest)
            case.result(target.digest, 'awaiting_deep', {
                'run_key': detail['run_key'], 'stage': 'whole_payload',
                'reason': 'whole-payload search finished; deep pass not yet completed',
                'candidates': [], 'outputs': [], 'search_limited': False,
                'search_tiers': {'policy': 'unclaimed_first', 'searched': [1],
                                 'include_recovered': False, 'second_tier_unsearched': False,
                                 'uniqueness_scope': 'searched_tiers_only'},
            })
        calls = []

        def whole(case, movie, *args):
            calls.append(movie.digest)
            return self.whole_miss(case, movie, *args)

        with Case(self.case_path) as case, patch.object(engine, 'whole_payload', side_effect=whole), \
                patch.object(engine, '_deep_batch', side_effect=self.complete_deep):
            engine.recover(case, self.options, retry=True)
            self.assertEqual(case.job('ordinary', target.digest)['detail']['stage'], 'scattered_extents')
        self.assertNotIn(target.digest, calls)
        self.assertEqual(len(calls), 2)

    def test_interrupted_deep_pass_retries_only_unfinished_deep_jobs(self):
        deep_before = []

        def partial(case, movies, pool, tools, options, run_key, incomplete, **kwargs):
            for movie in movies:
                deep_before.append(movie.digest)
                if len(deep_before) == 2:
                    raise KeyboardInterrupt
                self.complete_deep(case, [movie], pool, tools, options, run_key, incomplete)

        with Case(self.case_path) as case, \
                patch.object(engine, 'whole_payload', side_effect=self.whole_miss), \
                patch.object(engine, '_deep_batch', side_effect=partial):
            with self.assertRaises(KeyboardInterrupt):
                engine.recover(case, self.options, retry=True)
        deep_after = []

        def remaining(case, movies, pool, tools, options, run_key, incomplete, **kwargs):
            deep_after.extend(movie.digest for movie in movies)
            return self.complete_deep(case, movies, pool, tools, options, run_key, incomplete)

        with Case(self.case_path) as case, \
                patch.object(engine, 'whole_payload', side_effect=AssertionError('completed preliminary stage replayed')), \
                patch.object(engine, '_deep_batch', side_effect=remaining):
            engine.recover(case, self.options, retry=True)
        self.assertNotIn(deep_before[0], deep_after)
        self.assertIn(deep_before[1], deep_after)
        self.assertEqual(set(deep_after), {m.digest for m in self.movies} - {deep_before[0]})
        self.assertEqual(len(deep_after), 2)

    def test_limited_preliminary_coverage_survives_deep_queue_resume(self):
        def limited(case, movie, *args):
            return self.whole_miss(case, movie, *args, limited=True)

        with Case(self.case_path) as case, patch.object(engine, 'whole_payload', side_effect=limited), \
                patch.object(engine, 'deep_search', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.recover(case, self.options)
        with Case(self.case_path) as case, \
                patch.object(engine, 'whole_payload', side_effect=AssertionError('completed limited preliminary stage replayed')), \
                patch.object(engine, '_deep_batch', side_effect=self.complete_deep):
            engine.recover(case, self.options)
            for movie in self.movies:
                job = case.job('ordinary', movie.digest)
                self.assertEqual(job['status'], 'search_limited')
                self.assertTrue(job['detail']['search_limited'])
                self.assertFalse(job['detail']['outputs'])

    def test_explicit_retry_of_completed_run_retries_deep_without_preliminary(self):
        with Case(self.case_path) as case, \
                patch.object(engine, 'whole_payload', side_effect=self.whole_miss), \
                patch.object(engine, '_deep_batch', side_effect=self.complete_deep):
            engine.recover(case, self.options)
        retried = []

        def deep(case, movies, *args, **kwargs):
            retried.extend(movie.digest for movie in movies)
            return self.complete_deep(case, movies, *args, **kwargs)

        with Case(self.case_path) as case, \
                patch.object(engine, 'whole_payload', side_effect=AssertionError('completed preliminary stage replayed')), \
                patch.object(engine, '_deep_batch', side_effect=deep):
            engine.recover(case, self.options, retry=True)
        self.assertEqual(Counter(retried), Counter(movie.digest for movie in self.movies))

    def test_limited_explicit_retry_resumes_remaining_deep_jobs_once(self):
        with Case(self.case_path) as case, \
                patch.object(engine, 'whole_payload', side_effect=self.whole_miss), \
                patch.object(engine, '_deep_batch', side_effect=self.complete_deep):
            engine.recover(case, self.options)
        first_retry = []

        def first_deep(case, movies, *args, **kwargs):
            first_retry.extend(movie.digest for movie in movies)
            return self.complete_deep(case, movies, *args, **kwargs)

        with Case(self.case_path) as case, \
                patch.object(engine, 'whole_payload', side_effect=AssertionError('completed preliminary stage replayed')), \
                patch.object(engine, '_deep_batch', side_effect=first_deep):
            engine.recover(case, self.options, retry=True, limit=1)
        remaining = []

        def remaining_deep(case, movies, *args, **kwargs):
            remaining.extend(movie.digest for movie in movies)
            return self.complete_deep(case, movies, *args, **kwargs)

        with Case(self.case_path) as case, \
                patch.object(engine, 'whole_payload', side_effect=AssertionError('completed preliminary stage replayed')), \
                patch.object(engine, '_deep_batch', side_effect=remaining_deep):
            engine.recover(case, self.options, retry=True)
        self.assertEqual(len(first_retry), 1)
        self.assertEqual(len(remaining), 2)
        self.assertTrue(set(first_retry).isdisjoint(remaining))
        self.assertEqual(Counter(first_retry + remaining), Counter(movie.digest for movie in self.movies))

    def test_returning_to_previous_scope_restores_current_job_evidence(self):
        other = engine.Options(deep=True, reconstruct_only=True, max_groups=1)
        with Case(self.case_path) as case, \
                patch.object(engine, 'whole_payload', side_effect=self.whole_miss), \
                patch.object(engine, '_deep_batch', side_effect=self.complete_deep):
            engine.recover(case, self.options)
            first_key = json.loads(case.db.execute(
                "SELECT detail FROM events WHERE kind='recovery_started' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])['run_key']
            engine.recover(case, other)
            other_key = case.job('ordinary', self.movies[0].digest)['detail']['run_key']
            self.assertNotEqual(first_key, other_key)
        with Case(self.case_path) as case, \
                patch.object(engine, 'whole_payload', side_effect=self.whole_miss) as recomputed, \
                patch.object(engine, '_deep_batch', side_effect=self.complete_deep):
            engine.recover(case, self.options)
            self.assertGreater(recomputed.call_count, 0)
            latest_key = json.loads(case.db.execute(
                "SELECT detail FROM events WHERE kind='recovery_started' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0])['run_key']
            self.assertEqual(latest_key, first_key)
            for movie in self.movies:
                job = case.job('ordinary', movie.digest)
                self.assertEqual(job['detail']['run_key'], latest_key)
                self.assertEqual(job['detail']['stage'], 'scattered_extents')

    def test_changed_allocation_policy_invalidates_preliminary_scope(self):
        with Case(self.case_path) as case:
            sid = next(iter(case.sources()))
            ranges = {sid: [(0, self.source.stat().st_size)]}
            with patch.object(engine, 'whole_payload', side_effect=self.whole_miss), \
                    patch.object(engine, 'deep_search', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    engine.recover(case, self.options, allocation_scope=AllocationScope(ranges, 'policy-one'))
        calls = []

        def whole(case, movie, *args):
            calls.append(movie.digest)
            return self.whole_miss(case, movie, *args)

        with Case(self.case_path) as case, patch.object(engine, 'whole_payload', side_effect=whole), \
                patch.object(engine, '_deep_batch', side_effect=self.complete_deep):
            engine.recover(case, self.options, allocation_scope=AllocationScope(ranges, 'policy-two'))
        self.assertEqual(Counter(calls), Counter(movie.digest for movie in self.movies))

    def test_changed_search_options_invalidate_completed_preliminary_scope(self):
        with Case(self.case_path) as case, \
                patch.object(engine, 'whole_payload', side_effect=self.whole_miss), \
                patch.object(engine, 'deep_search', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.recover(case, self.options)
        calls = []

        def whole(case, movie, *args):
            calls.append(movie.digest)
            return self.whole_miss(case, movie, *args)

        with Case(self.case_path) as case, patch.object(engine, 'whole_payload', side_effect=whole), \
                patch.object(engine, '_deep_batch', side_effect=self.complete_deep):
            engine.recover(case, engine.Options(deep=True, reconstruct_only=True, max_groups=1))
        self.assertEqual(Counter(calls), Counter(m.digest for m in self.movies))

    def test_new_source_invalidates_completed_preliminary_scope(self):
        with Case(self.case_path) as case, \
                patch.object(engine, 'whole_payload', side_effect=self.whole_miss), \
                patch.object(engine, 'deep_search', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine.recover(case, self.options)
        extra = self.root / 'additional.bin'
        extra.write_bytes(b'additional source evidence')
        calls = []

        def whole(case, movie, *args):
            calls.append(movie.digest)
            return self.whole_miss(case, movie, *args)

        with Case(self.case_path) as case:
            scan_logical(case, case.register_source(extra))
            with patch.object(engine, 'whole_payload', side_effect=whole), \
                    patch.object(engine, '_deep_batch', side_effect=self.complete_deep):
                engine.recover(case, self.options)
        self.assertEqual(Counter(calls), Counter(m.digest for m in self.movies))

    def test_pending_owner_revalidation_preserves_accepted_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            movie = structural_movie([100, 200], [8, 8])
            source = root / 'deep.bin'
            token = b'\0\0\0\x04\x65ABC'
            source.write_bytes(movie.data + token + b'Z' * 8 + token)
            path = root / 'case'
            with Case(path) as case:
                scan_logical(case, case.register_source(source))
                engine.recover(case, engine.Options(deep=True, reconstruct_only=True))
                output = case.root / case.job('ordinary', movie.digest)['detail']['outputs'][0]
                before = (output.stat().st_ino, output.stat().st_mtime_ns, output.read_bytes())
                options = engine.Options(deep=True, reconstruct_only=True, max_groups=1)
                with patch.object(engine, 'deep_search', side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        engine.recover(case, options, retry=True)
            with Case(path) as case, patch.object(engine, 'whole_payload',
                    side_effect=AssertionError('completed owner preliminary stage replayed')):
                engine.recover(case, options, retry=True)
                job = case.job('ordinary', movie.digest)
                self.assertEqual(job['status'], 'reconstructed')
                self.assertTrue(job['detail']['outputs'])
                self.assertEqual((output.stat().st_ino, output.stat().st_mtime_ns, output.read_bytes()), before)

    def test_interrupted_first_tier_completion_retains_fallback_work(self):
        with tempfile.TemporaryDirectory() as directory:
            case, movie, _ = tier_cases.OrdinaryTierTests.fixture(self, Path(directory))
            options = engine.Options(deep=True, reconstruct_only=True, include_recovered=True)
            original = engine._deep_batch

            def stop_before_fallback(*args, **kwargs):
                original(*args, **kwargs)
                raise KeyboardInterrupt

            with patch.object(engine, '_deep_batch', side_effect=stop_before_fallback):
                with self.assertRaises(KeyboardInterrupt):
                    engine.recover(case, options, retry=True)
            root = case.root
            case.__exit__(None, None, None)
            scopes = []
            original_whole = engine.whole_payload

            def fallback_only(case, movie, *args):
                searched = list(engine._search_scope(case)['searched'])
                scopes.append(searched)
                self.assertEqual(searched, [1, 2], 'completed first-tier preliminary stage replayed')
                return original_whole(case, movie, *args)

            with Case(root) as resumed, patch.object(engine, 'whole_payload', side_effect=fallback_only):
                engine.recover(resumed, options, retry=True)
                job = resumed.job('ordinary', movie.digest)
                self.assertEqual(job['status'], 'reconstructed')
                self.assertEqual(job['detail']['search_tiers']['searched'], [1, 2])
                self.assertTrue(job['detail']['outputs'])
            self.assertEqual(scopes, [[1, 2]])

    def test_completed_ambiguous_resume_rechecks_canonical_candidate_integrity(self):
        for change in ('missing', 'changed_bytes', 'symlink'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                movie = structural_movie([1000], [8])
                source = root / 'ambiguous.bin'
                token = b'\0\0\0\x04\x65ABC'
                other = b'\0\0\0\x04\x65BBB'
                source.write_bytes(movie.data + bytes(992 - len(movie.data)) +
                                   struct.pack('>I4s', 16, b'mdat') + token +
                                   struct.pack('>I4s', 16, b'mdat') + other)
                original = source.read_bytes()
                path = root / 'case'
                options = engine.Options(reconstruct_only=True)
                with Case(path) as case:
                    scan_logical(case, case.register_source(source))
                    engine.recover(case, options)
                    job = case.job('ordinary', movie.digest)
                    self.assertEqual(job['status'], 'ambiguous')
                    self.assertEqual(len(job['detail']['candidates']), 2)
                    self.assertFalse(job['detail']['outputs'])
                    candidate = case.root / job['detail']['candidates'][0]['path']
                if change == 'missing':
                    candidate.unlink()
                elif change == 'changed_bytes':
                    changed = bytearray(candidate.read_bytes())
                    changed[-1] ^= 1
                    candidate.write_bytes(changed)
                else:
                    substitute = root / 'substitute.mp4'
                    substitute.write_bytes(candidate.read_bytes())
                    candidate.unlink()
                    candidate.symlink_to(substitute)
                with Case(path) as resumed:
                    with self.assertRaises(SafetyError):
                        engine.recover(resumed, options)
                self.assertEqual(source.read_bytes(), original)

    def test_limit_excluded_pending_deep_job_cannot_enter_recovered_fallback(self):
        alternative = b'\0\0\0\x04\x65BBB'
        with tempfile.TemporaryDirectory() as directory:
            case, movie, source = tier_cases.OrdinaryTierTests.fixture(
                self, Path(directory), tail=b'Z' * 16 + alternative)
            original_source = source.read_bytes()
            options = engine.Options(deep=True, reconstruct_only=True, include_recovered=True)
            with patch.object(engine, 'deep_search', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    engine.recover(case, options)
            self.assertEqual(case.job('ordinary', movie.digest)['status'], 'awaiting_deep')
            path = case.root
            case.__exit__(None, None, None)
            scopes = []
            original_whole = engine.whole_payload

            def tracked_whole(case, movie, *args):
                scopes.append(list(engine._search_scope(case)['searched']))
                return original_whole(case, movie, *args)

            with Case(path) as resumed, patch.object(engine, 'whole_payload', side_effect=tracked_whole), \
                    patch.object(engine, 'deep_search', wraps=engine.deep_search) as deep:
                engine.recover(resumed, options, limit=0)
                self.assertEqual(deep.call_count, 0)
                self.assertNotIn([1, 2], scopes)
                pending = resumed.job('ordinary', movie.digest)
                self.assertEqual(pending['status'], 'awaiting_deep')
                self.assertFalse(pending['detail']['outputs'])
                self.assertEqual(pending['detail']['search_tiers']['searched'], [1])
                self.assertEqual(resumed.db.execute(
                    'SELECT state FROM ordinary_resume WHERE run_key=? AND tier=1 AND digest=?',
                    (pending['detail']['run_key'], movie.digest)).fetchone()[0], 'deep_pending')
            with Case(path) as resumed, patch.object(engine, 'deep_search', wraps=engine.deep_search) as deep:
                engine.recover(resumed, options)
                self.assertGreater(deep.call_count, 0)
                completed = resumed.job('ordinary', movie.digest)
                self.assertEqual(completed['status'], 'reconstructed')
                self.assertEqual(completed['detail']['search_tiers']['searched'], [1])
                self.assertEqual(len(completed['detail']['outputs']), 1)
                self.assertEqual(completed['detail']['candidates'][0]['media_sha256'],
                                 hashlib.sha256(alternative).hexdigest())
            self.assertEqual(source.read_bytes(), original_source)


if __name__ == '__main__':
    unittest.main()
