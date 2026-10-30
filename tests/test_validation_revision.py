"""Validator changes revisit cached decisions without touching acquisition indexes."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from helpers import generate, movie_from, payload
from videorebuild import engine
from videorebuild.storage import Case, ReaderPool, scan_logical


class ValidationRevisionTests(unittest.TestCase):
    def test_stale_valid_and_rejected_attempts_are_revalidated_without_reindex(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original.mp4'
            generate(original, audio=False, seconds=0.25)
            movie = movie_from(original.read_bytes())
            with Case(root / 'case') as case:
                sid = case.register_source(original)
                scan_logical(case, sid)
                indexed = [tuple(r) for r in case.db.execute('SELECT * FROM sources ORDER BY id')]
                atoms = [tuple(r) for r in case.db.execute('SELECT * FROM atoms ORDER BY id')]
                mapping = {(c.track, c.index): (sid, c.offset) for c in movie.chunks}
                with ReaderPool(case.sources()) as pool:
                    result = engine.attempt(case, movie, mapping, pool, engine.doctor(), engine.Options(), 'original_offsets')
                    self.assertEqual(result['status'], 'valid')
                    candidate = case.root / result['path']
                    before = hashlib.sha256(candidate.read_bytes()).hexdigest()
                    for status in ('valid', 'rejected'):
                        for revision in (None, engine.VALIDATION_REVISION-1):
                            with self.subTest(status=status, revision=revision):
                                stale = dict(result, status=status, reason='obsolete_validator_decision')
                                if revision is None:
                                    stale.pop('validation_revision', None)
                                else:
                                    stale['validation_revision'] = revision
                                case.db.execute('UPDATE attempts SET status=?,detail=?', (status, json.dumps(stale)))
                                case.db.commit()
                                with patch.object(engine, 'validate', wraps=engine.validate) as checked:
                                    updated = engine.attempt(case, movie, mapping, pool, result['tools'], engine.Options(), 'original_offsets')
                                self.assertEqual(checked.call_count, 1)
                                self.assertEqual(updated['status'], 'valid')
                                self.assertEqual(updated['validation_revision'], engine.VALIDATION_REVISION)
                                self.assertEqual(hashlib.sha256(candidate.read_bytes()).hexdigest(), before)
                    with patch.object(engine, 'validate', side_effect=AssertionError('current result should be reused')):
                        self.assertEqual(engine.attempt(case, movie, mapping, pool, result['tools'], engine.Options(), 'original_offsets')['status'], 'valid')
                self.assertEqual([tuple(r) for r in case.db.execute('SELECT * FROM sources ORDER BY id')], indexed)
                self.assertEqual([tuple(r) for r in case.db.execute('SELECT * FROM atoms ORDER BY id')], atoms)

    def test_validator_revision_reopens_finished_job_without_intake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original.mp4'
            generate(original, audio=False, seconds=0.25)
            with Case(root / 'case') as case:
                scan_logical(case, case.register_source(original))
                engine.recover(case, engine.Options())
                first = case.db.execute("SELECT detail FROM jobs WHERE kind='ordinary'").fetchone()[0]
                indexed = [tuple(r) for r in case.db.execute('SELECT * FROM sources ORDER BY id')]
                with patch.object(engine, 'VALIDATION_REVISION', engine.VALIDATION_REVISION+1), \
                        patch.object(engine, 'validate', wraps=engine.validate) as checked:
                    engine.recover(case, engine.Options())
                second = case.db.execute("SELECT detail FROM jobs WHERE kind='ordinary'").fetchone()[0]
                self.assertNotEqual(json.loads(first)['run_key'], json.loads(second)['run_key'])
                self.assertGreater(checked.call_count, 0)
                outputs = list((case.root / 'reconstructed').glob('*.mp4'))
                self.assertEqual(len(outputs), 1)
                self.assertEqual(payload(outputs[0]), payload(original))
                self.assertEqual([tuple(r) for r in case.db.execute('SELECT * FROM sources ORDER BY id')], indexed)

    def test_structural_candidate_cannot_be_reused_as_decode_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.mp4'
            generate(source, audio=False, seconds=0.25)
            movie = movie_from(source.read_bytes())
            with Case(root/'case') as case:
                sid = case.register_source(source)
                scan_logical(case, sid)
                mapping = {(c.track, c.index): (sid, c.offset) for c in movie.chunks}
                with ReaderPool(case.sources()) as pool:
                    reconstructed = engine.attempt(case, movie, mapping, pool, {}, engine.Options(reconstruct_only=True), 'original_offsets')
                    self.assertEqual(reconstructed['status'], 'valid')
                    self.assertFalse(reconstructed['validation']['decode_checked'])
                    before = (case.root/reconstructed['path']).read_bytes()
                    with patch.object(engine, 'validate', return_value={'ok': False, 'reason': 'synthetic_codec_rejection'}) as checked:
                        strict = engine.attempt(case, movie, mapping, pool, {}, engine.Options(), 'original_offsets')
                    checked.assert_called_once()
                    self.assertEqual(strict['status'], 'rejected')
                    self.assertFalse(strict['reconstruct_only'])
                    self.assertEqual((case.root/reconstructed['path']).read_bytes(), before)
