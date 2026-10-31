"""Fragmented tiers and ordered manifests use generated structural packets."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_fragmented_reconstruct_only import structural_movie
from videorebuild.image_recovery import recover_fragmented, verify_completed
from videorebuild.storage import Case, ReaderPool, SafetyError, scan_logical, file_sha256, write_json


class FragmentedCandidateTierTests(unittest.TestCase):
    def options(self, include=False):
        return SimpleNamespace(max_record_bytes=1024*1024, max_segments=None, timeout=30,
                               max_output_bytes=1024*1024, reconstruct_only=True,
                               include_recovered=include)

    def test_alias_demoted_and_fallback_explicit(self):
        for include in (False, True):
            with self.subTest(include=include), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, data = structural_movie()
                source, alias = root/'first.img', root/'alias.img'
                source.write_bytes(data)
                alias.hardlink_to(source)
                with Case(root/'case') as case:
                    for path in (source, alias):
                        scan_logical(case, case.register_source(path, 'image'))
                    with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix') as decoder:
                        recover_fragmented(case, pool, {}, self.options(include))
                    decoder.assert_not_called()
                    jobs = list(case.get_jobs('fragmented'))
                    self.assertEqual(len(jobs), 2)
                    self.assertTrue(jobs[0]['detail']['outputs'])
                    second = jobs[1]['detail']
                    self.assertEqual(bool(second['outputs']), include)
                    self.assertEqual(second['search_tiers']['searched'], [1, 2] if include else [1])
                    self.assertEqual(second['search_tiers']['second_tier_unsearched'], not include)
                    self.assertEqual(second['candidate_starts_considered'], 1 if include else 0)
                    # Resume retains self-owned publications and identical manifest hashes.
                    before = [(j['key'], j['detail']) for j in jobs]
                    with ReaderPool(case.sources()) as pool:
                        recover_fragmented(case, pool, {}, self.options(include))
                    self.assertEqual(before, [(j['key'], j['detail']) for j in case.get_jobs('fragmented')])

    def test_unclaimed_later_section_wins_before_recovered_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialization, movie = structural_movie()
            data = movie+b'gapbytes'+movie[len(initialization):]
            source, alias = root/'first.img', root/'alias.img'
            source.write_bytes(data)
            alias.hardlink_to(source)
            with Case(root/'case') as case:
                for path in (source, alias):
                    scan_logical(case, case.register_source(path, 'image'))
                options = self.options(True)
                options.max_segments = 1
                with ReaderPool(case.sources()) as pool:
                    recover_fragmented(case, pool, {}, options)
                jobs = list(case.get_jobs('fragmented'))
                first, second = (j['detail'] for j in jobs)
                self.assertEqual(first['segments'][0]['logical_start'], len(initialization))
                accepted = [s for s in second['segments'] if s.get('output')]
                self.assertEqual([s['logical_start'] for s in accepted], [len(movie)+8])
                self.assertEqual(second['search_tiers']['searched'], [1])
                self.assertTrue(second['search_tiers']['second_tier_unsearched'])
                self.assertEqual((case.root/accepted[0]['output']).read_bytes(), movie)

    def test_strict_manifest_and_backfill_retain_recorded_decode_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, data = structural_movie()
            source = root/'strict.img'
            source.write_bytes(data)
            with Case(root/'case') as case:
                sid = case.register_source(source, 'image')
                scan_logical(case, sid)
                options = self.options()
                options.reconstruct_only = False
                with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix',
                        return_value={'ok': True, 'reason': 'mocked_strict_gate', 'decode_checked': True}):
                    recover_fragmented(case, pool, {}, options)
                job = next(case.get_jobs('fragmented'))
                detail = job['detail']
                entry = detail['output_evidence'][0]
                sidecar = case.root/entry['manifest_path']
                manifest = json.loads(sidecar.read_text())
                self.assertTrue(manifest['decode_checked'])
                self.assertEqual(manifest['validation_scope'], 'strict_decode')
                sidecar.unlink()
                entry.pop('manifest_path')
                entry.pop('manifest_sha256')
                evidence = case.root/detail['detail_path']
                write_json(evidence, detail)
                detail['evidence_sha256'] = file_sha256(evidence)
                case.result(job['key'], job['status'], detail, kind='fragmented')
                with ReaderPool(case.sources()) as pool, \
                        patch('videorebuild.image_recovery.validate_prefix', side_effect=AssertionError('no backfill decoder')):
                    recover_fragmented(case, pool, {}, options)
                entry = case.job('fragmented', job['key'])['detail']['output_evidence'][0]
                manifest = json.loads((case.root/entry['manifest_path']).read_text())
                self.assertTrue(manifest['decode_checked'])
                self.assertEqual(manifest['validation_scope'], 'strict_decode')

    def test_failed_first_tier_candidate_does_not_collide_with_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, data = structural_movie()
            source = root/'source.img'
            source.write_bytes(data)
            with Case(root/'case') as case:
                scan_logical(case, case.register_source(source, 'image'))
                options = self.options(True)
                options.reconstruct_only = False
                outcomes = [{'ok': False, 'reason': 'synthetic_first_tier_failure'},
                            {'ok': True, 'reason': 'synthetic_strict_gate', 'decode_checked': True}]
                with ReaderPool(case.sources()) as pool, patch(
                        'videorebuild.image_recovery.validate_prefix', side_effect=outcomes) as gate:
                    recover_fragmented(case, pool, {}, options)
                self.assertEqual(gate.call_count, 2)
                detail = next(case.get_jobs('fragmented'))['detail']
                self.assertEqual(detail['search_tiers']['searched'], [1, 2])
                self.assertEqual(len(detail['outputs']), 1)
                accepted = [segment for segment in detail['segments'] if segment.get('output')]
                self.assertEqual(accepted[0]['candidate_tier'], 2)
                self.assertEqual((case.root/detail['outputs'][0]).read_bytes(), data)
                attempts = [Path(call.args[0]) for call in gate.call_args_list]
                self.assertEqual(len(set(attempts)), 2)
                self.assertTrue(all(path.read_bytes() == data for path in attempts))

    def test_completed_publication_manifest_backfilled_without_media_reads_or_recopy(self):
        for proof in (True, False):
            with self.subTest(physical_proof=proof), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, data = structural_movie()
                source = root/'source.img'
                source.write_bytes(data)
                with Case(root/'case') as case:
                    sid = case.register_source(source, 'image')
                    scan_logical(case, sid)
                    with ReaderPool(case.sources()) as pool:
                        recover_fragmented(case, pool, {}, self.options())
                    job = next(case.get_jobs('fragmented'))
                    detail = job['detail']
                    output = case.root/detail['outputs'][0]
                    original = (output.stat().st_ino, output.stat().st_mtime_ns, file_sha256(output))
                    entry = detail['output_evidence'][0]
                    (case.root/entry.pop('manifest_path')).unlink()
                    entry.pop('manifest_sha256')
                    if not proof:
                        detail['segments'][0].pop('source_ranges')
                    evidence = case.root/detail['detail_path']
                    write_json(evidence, detail)
                    detail['evidence_sha256'] = file_sha256(evidence)
                    case.result(job['key'], job['status'], detail, kind='fragmented')
                    with ReaderPool(case.sources()) as pool, \
                            patch.object(pool, 'read', side_effect=AssertionError('no source media reread')), \
                            patch('videorebuild.image_recovery.validate_prefix', side_effect=AssertionError('no decoder')):
                        recover_fragmented(case, pool, {}, self.options())
                    current = case.job('fragmented', job['key'])['detail']
                    entry = current['output_evidence'][0]
                    manifest = json.loads((case.root/entry['manifest_path']).read_text())
                    if proof:
                        self.assertTrue(manifest['ordered_fragments'])
                        self.assertEqual(manifest['status'], 'backfilled_from_existing_publication')
                    else:
                        self.assertEqual(manifest['status'], 'historical_manifest_unavailable')
                        self.assertIn('physical_proof', manifest['reason'])
                        self.assertNotIn('ordered_fragments', manifest)
                    self.assertEqual(original, (output.stat().st_ino, output.stat().st_mtime_ns, file_sha256(output)))
                    verify_completed(case)

    def test_ordered_manifest_replays_every_output_byte_and_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, data = structural_movie()
            source = root/'source.img'
            source.write_bytes(data)
            with Case(root/'case') as case:
                sid = case.register_source(source, 'image')
                scan_logical(case, sid)
                with ReaderPool(case.sources()) as pool:
                    recover_fragmented(case, pool, {}, self.options())
                job = next(case.get_jobs('fragmented'))
                evidence = job['detail']['output_evidence'][0]
                sidecar = case.root/evidence['manifest_path']
                manifest = json.loads(sidecar.read_text())
                replay = bytearray()
                kinds = []
                for ordinal, fragment in enumerate(manifest['ordered_fragments']):
                    self.assertEqual(fragment['ordinal'], ordinal)
                    self.assertEqual(fragment['output_offset'], len(replay))
                    raw = bytearray()
                    for extent in fragment['physical_extents']:
                        path = Path(extent['source_path'])
                        with path.open('rb') as stream:
                            stream.seek(extent['source_offset'])
                            raw.extend(stream.read(extent['length']))
                        self.assertEqual(extent['source_identity']['size'], len(data))
                    self.assertEqual(len(raw), fragment['length'])
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), fragment['sha256'])
                    replay.extend(raw)
                    kinds.append(fragment['kind'])
                self.assertEqual(bytes(replay), data)
                self.assertEqual(bytes(replay), (case.root/evidence['path']).read_bytes())
                self.assertEqual(set(kinds), {'initialization', 'media'})
                media = manifest['ordered_fragments'][1]
                self.assertEqual(media['content'], 'moof_mdat_container_and_encoded_media')
                self.assertEqual(media['sequence'], 1)
                self.assertEqual(media['sample_counts'], {'1': 1})
                self.assertEqual(manifest['output_sha256'], hashlib.sha256(replay).hexdigest())
                self.assertFalse(manifest['decode_checked'])
                sidecar.write_text('{}')
                with self.assertRaisesRegex(SafetyError, 'fragmented manifest'):
                    verify_completed(case)
