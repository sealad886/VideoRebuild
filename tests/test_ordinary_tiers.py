"""Ordinary tier fallback preserves bytes and publishes ordered reconstruction evidence."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import test_candidate_tiers as tier_fixtures
from test_deep_witnesses import structural_movie
from videorebuild import engine
from videorebuild.storage import Case, ReaderPool, scan_logical


class OrdinaryTierTests(unittest.TestCase):
    def fixture(self, root, extra=False):
        token = b'\0\0\0\x04\x65ABC'
        movie = structural_movie([1000], [8])
        source = root/'source.bin'
        data = movie.data + bytes(992-len(movie.data)) + struct.pack('>I4s', 16, b'mdat') + token
        if extra:
            data += struct.pack('>I4s', 16, b'mdat') + token
        source.write_bytes(data)
        case = Case(root/'case').__enter__()
        self.addCleanup(case.__exit__, None, None, None)
        sid = case.register_source(source)
        scan_logical(case, sid)
        with ReaderPool(case.sources()) as pool:
            tier_fixtures.CandidateTierTests.publish(self, case, pool, sid, 1000, 8, 'other')
        return case, movie, source

    def test_default_does_not_attempt_demoted_reference_and_fallback_recovers(self):
        with tempfile.TemporaryDirectory() as d:
            case, movie, source = self.fixture(Path(d))
            original = source.read_bytes()
            with patch.object(engine, 'attempt', wraps=engine.attempt) as attempted:
                engine.recover(case, engine.Options(deep=True, reconstruct_only=True))
            self.assertEqual(attempted.call_count, 0)
            job = case.job('ordinary', movie.digest)
            self.assertFalse(job['detail'].get('outputs'))
            self.assertEqual(job['detail']['search_tiers']['searched'], [1])
            self.assertTrue(job['detail']['search_tiers']['second_tier_unsearched'])
            engine.recover(case, engine.Options(deep=True, reconstruct_only=True, include_recovered=True))
            job = case.job('ordinary', movie.digest)
            self.assertEqual(job['status'], 'reconstructed')
            self.assertEqual(job['detail']['search_tiers']['searched'], [1, 2])
            output = case.root/job['detail']['outputs'][0]
            manifest = json.loads(output.with_suffix('.json').read_text())
            raw = output.read_bytes()
            end = 0
            media = bytearray()
            for ordinal, part in enumerate(manifest['ordered_fragments']):
                self.assertEqual(part['ordinal'], ordinal)
                self.assertEqual(part['output_offset'], end)
                end += part['length']
                chunk = raw[part['output_offset']:end]
                self.assertEqual(hashlib.sha256(chunk).hexdigest(), part['sha256'])
                if part['kind'] == 'media':
                    replay = source.read_bytes()[part['source_offset']:part['source_offset']+part['length']]
                    self.assertEqual(chunk, replay)
                    self.assertIn('source_identity', part['physical_extents'][0])
                    media.extend(replay)
                else:
                    self.assertTrue(part['generated'])
            self.assertEqual(end, len(raw))
            self.assertEqual(media, b'\0\0\0\x04\x65ABC')
            self.assertEqual(source.read_bytes(), original)
            with patch.object(engine, 'whole_payload', side_effect=AssertionError('completed output rerun')):
                engine.recover(case, engine.Options(deep=True, reconstruct_only=True, include_recovered=True), retry=True)

    def test_completed_resume_backfills_legacy_manifest_without_touching_media(self):
        with tempfile.TemporaryDirectory() as d:
            case, movie, _ = self.fixture(Path(d), extra=True)
            options = engine.Options(reconstruct_only=True, include_recovered=True)
            engine.recover(case, options)
            job = case.job('ordinary', movie.digest)
            output = case.root/job['detail']['outputs'][0]
            candidate = case.root/job['detail']['candidates'][0]['path']
            before = (output.stat().st_ino, output.stat().st_mtime_ns, hashlib.sha256(output.read_bytes()).hexdigest())
            for path in (output, candidate):
                sidecar = path.with_suffix('.json')
                manifest = json.loads(sidecar.read_text())
                manifest.pop('ordered_fragments', None)
                manifest.get('provenance', {}).pop('ordered_fragments', None)
                sidecar.write_text(json.dumps(manifest))
            with patch.object(engine, 'whole_payload', side_effect=AssertionError('resume recopy')):
                engine.recover(case, options)
            manifest = json.loads(output.with_suffix('.json').read_text())
            self.assertTrue(manifest['ordered_fragments'])
            self.assertEqual((output.stat().st_ino, output.stat().st_mtime_ns, hashlib.sha256(output.read_bytes()).hexdigest()), before)

    def test_deep_resume_revalidates_own_ranges_and_retains_publication_on_miss(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            movie = structural_movie([100, 200], [8, 8])
            source = root/'deep.bin'
            source.write_bytes(movie.data + b'\0\0\0\x04\x65ABC' + b'Z'*8 + b'\0\0\0\x04\x65ABC')
            with Case(root/'case') as case:
                scan_logical(case, case.register_source(source))
                engine.recover(case, engine.Options(deep=True, reconstruct_only=True))
                first = case.job('ordinary', movie.digest)
                self.assertEqual(first['status'], 'reconstructed')
                output = case.root/first['detail']['outputs'][0]
                before = (output.stat().st_ino, output.stat().st_mtime_ns, output.read_bytes())
                with patch.object(engine, 'doctor', return_value={}), patch.object(engine, 'validate', return_value={'ok': True, 'reason': 'fixture'}) as checked:
                    engine.recover(case, engine.Options(deep=True))
                self.assertGreater(checked.call_count, 0)
                self.assertEqual(case.job('ordinary', movie.digest)['status'], 'matched')
                self.assertEqual((output.stat().st_ino, output.stat().st_mtime_ns, output.read_bytes()), before)
                # Simulate a narrower policy yielding no candidates in either stage.
                with patch.object(engine, 'check_samples', return_value=False):
                    engine.recover(case, engine.Options(deep=True, reconstruct_only=True, max_groups=1))
                last = case.job('ordinary', movie.digest)
                self.assertFalse(last['detail']['outputs'])
                self.assertIn(str(output.relative_to(case.root)), last['detail']['historical_outputs'])
                self.assertTrue(last['detail']['historical_publications'])
                self.assertEqual((output.stat().st_ino, output.stat().st_mtime_ns, output.read_bytes()), before)

    def test_first_tier_success_never_checks_second_tier(self):
        with tempfile.TemporaryDirectory() as d:
            case, movie, _ = self.fixture(Path(d), extra=True)
            with patch.object(engine, 'attempt', wraps=engine.attempt) as attempted:
                engine.recover(case, engine.Options(deep=True, reconstruct_only=True, include_recovered=True))
            self.assertEqual(attempted.call_count, 1)
            job = case.job('ordinary', movie.digest)
            self.assertEqual(job['status'], 'reconstructed')
            self.assertEqual(job['detail']['search_tiers']['searched'], [1])
            self.assertTrue(job['detail']['search_tiers']['second_tier_unsearched'])
            candidate = json.loads((case.root/job['detail']['candidates'][0]['path']).with_suffix('.json').read_text())
            self.assertEqual(candidate['provenance']['chunks'][0]['source_offset'], 1016)
