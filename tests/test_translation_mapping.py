"""Uniform translations retain canonical cache identity and full proof gates."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_deep_witnesses import structural_movie
from videorebuild import engine
from videorebuild.bmff import FormatError
from videorebuild.storage import Case, ReaderPool, SafetyError


class TranslationMappingTests(unittest.TestCase):
    def test_translation_digest_matches_independent_legacy_encoding(self):
        movie = structural_movie([32, 8, 80], [8, 8, 8])
        plan = engine._TranslationPlan(movie)
        for sid in (1, 200):
            for delta in (-40, 0, 123456789):
                mapping = plan.mapping(sid, delta)
                eager = {(c.track, c.index): (sid, delta+c.offset) for c in movie.chunks}
                expected = hashlib.sha256(json.dumps([movie.digest, sorted(
                    (t, c, s, o) for (t, c), (s, o) in eager.items())],
                    sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
                self.assertEqual(dict(mapping), eager)
                self.assertEqual(engine.mapping_digest(movie, mapping), expected)

    def test_negative_and_warm_cache_equal_eager_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'negative.bin'
            source.write_bytes(b'\0'*104)
            movie = structural_movie(list(range(0, 104, 8)), [8]*13)
            plan = engine._TranslationPlan(movie)
            with Case(root/'case') as case:
                sid = case.register_source(source)
                sources = {r['id']:dict(r) for r in case.db.execute('SELECT * FROM sources')}
                with ReaderPool(sources) as pool:
                    opts = engine.Options(reconstruct_only=True)
                    eager = dict(plan.mapping(sid, 0))
                    first = engine.attempt(case, movie, eager, pool, {}, opts, 'whole_mdat')
                    with patch.object(pool, 'read', side_effect=AssertionError('warm negative read')):
                        self.assertEqual(engine.attempt(case, movie, plan.mapping(sid, 0), pool, {}, opts, 'whole_mdat'), first)
                    source.write_bytes(b'x'*105)
                    with self.assertRaises(SafetyError):
                        engine.attempt(case, movie, plan.mapping(sid, 0), pool, {}, opts, 'whole_mdat')

    def test_valid_translation_reconstructs_same_encoded_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'valid.bin'
            source.write_bytes(b'Z'*64 + b'\0\0\0\x04\x65ABC'*2)
            movie = structural_movie([0, 8], [8, 8])
            plan = engine._TranslationPlan(movie)
            results = []
            for lazy in (False, True):
                with Case(root/str(lazy)) as case:
                    sid = case.register_source(source)
                    sources = {r['id']:dict(r) for r in case.db.execute('SELECT * FROM sources')}
                    with ReaderPool(sources) as pool:
                        mapping = plan.mapping(sid, 64)
                        result = engine.attempt(case, movie, mapping if lazy else dict(mapping), pool, {},
                                                engine.Options(reconstruct_only=True), 'whole_mdat')
                        self.assertEqual(result['status'], 'valid')
                        results.append((result, (case.root/result['path']).read_bytes()))
            self.assertEqual(results[0], results[1])

    def test_translation_source_bounds_and_survivor_full_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'tokens.bin'
            source.write_bytes(b'\0\0\0\x04\x65ABC'*2)
            movie = structural_movie([0, 8], [8, 8])
            plan = engine._TranslationPlan(movie)
            with Case(root/'case') as case:
                sid = case.register_source(source)
                sources = {r['id']:dict(r) for r in case.db.execute('SELECT * FROM sources')}
                with ReaderPool(sources) as pool:
                    for delta in (-1, 1):
                        result = engine.attempt(case, movie, plan.mapping(sid, delta), pool, {}, engine.Options(), 'whole_mdat')
                        self.assertEqual(result['reason'], 'chunk outside source')
                    with patch.object(pool, 'provenance', side_effect=FormatError('allocation exclusion')), \
                            patch.object(engine, 'reconstruct', side_effect=AssertionError('must not copy')):
                        result = engine.attempt(case, movie, plan.mapping(sid, 0), pool, {}, engine.Options(reconstruct_only=True), 'whole_mdat')
                        self.assertEqual(result['status'], 'rejected')
                        self.assertIn('allocation', result['reason'])
                        self.assertFalse(result.get('negative_screen', False))
