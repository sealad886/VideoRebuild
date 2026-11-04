"""Missing fragmented exports are rebuilt from committed source proof only."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_fragmented_reconstruct_only import structural_movie
from videorebuild import engine, workflow
from videorebuild.cli import parser
from videorebuild.image_recovery import recover_fragmented, restore_fragmented_job, verify_completed
from videorebuild.storage import Case, ReaderPool, SafetyError, file_sha256, scan_logical


class FragmentedRestorationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.initialization, self.movie = structural_movie()
        self.source = self.root/'source.img'
        self.source.write_bytes(self.movie)
        self.options = SimpleNamespace(max_record_bytes=1024*1024, max_segments=None, timeout=30,
                                       max_output_bytes=1024*1024, reconstruct_only=True)

    def create_job(self, case, strict=False):
        scan_logical(case, case.register_source(self.source, 'image'))
        self.options.reconstruct_only = not strict
        with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix',
                return_value={'ok': True, 'decode_checked': True, 'reason': 'synthetic_strict_gate'}):
            recover_fragmented(case, pool, {}, self.options)
        return next(case.get_jobs('fragmented'))

    def remove_artifacts(self, case, job, names):
        detail = job['detail']
        entry = detail['output_evidence'][0]
        paths = {'output': entry['path'], 'manifest': entry['manifest_path'],
                 'evidence': detail['detail_path']}
        for name in names:
            (case.root/paths[name]).unlink()
        return paths

    def test_resume_recreates_missing_generated_artifacts_without_new_search_or_decode(self):
        for strict in (False, True):
            for missing in (('output',), ('manifest',), ('evidence',), ('output', 'manifest', 'evidence')):
                with self.subTest(strict=strict, missing=missing), Case(self.root/f'case-{strict}-{"-".join(missing)}') as case:
                    job = self.create_job(case, strict)
                    before = job['detail']
                    self.remove_artifacts(case, job, missing)
                    with ReaderPool(case.sources()) as pool, \
                            patch('videorebuild.image_recovery._read_initialization', side_effect=AssertionError('new discovery')), \
                            patch('videorebuild.image_recovery.validate_prefix', side_effect=AssertionError('new decoder')):
                        self.assertEqual(recover_fragmented(case, pool, {}, self.options), 0)
                    current = case.job('fragmented', job['key'])
                    self.assertEqual(current['status'], job['status'])
                    detail = current['detail']
                    entry = detail['output_evidence'][0]
                    self.assertEqual((case.root/entry['path']).read_bytes(), self.movie)
                    self.assertEqual(file_sha256(case.root/entry['path']), before['output_evidence'][0]['sha256'])
                    self.assertEqual(detail['segments'], before['segments'])
                    self.assertEqual(detail['decode_checked'], strict)
                    manifest = json.loads((case.root/entry['manifest_path']).read_text())
                    self.assertEqual(manifest['decode_checked'], strict)
                    verify_completed(case)

    def test_workflow_repairs_fragmented_output_before_ordinary_tier_loading(self):
        with Case(self.root/'case') as case:
            scan_logical(case, case.register_source(self.source, 'image'))
            args = parser().parse_args(['recover', '--case', str(case.root), '--reconstruct-only'])
            workflow.reconstruct(case, args)
            job = next(case.get_jobs('fragmented'))
            paths = self.remove_artifacts(case, job, ('output', 'manifest', 'evidence'))
            visited = []
            original = engine.recover
            def ordinary(*args, **kwargs):
                original(*args, **kwargs)
                visited.append(True)
                self.assertEqual((case.root/paths['output']).read_bytes(), self.movie)
                verify_completed(case)
            with patch('videorebuild.workflow.recover', side_effect=ordinary), \
                    patch('videorebuild.image_recovery.validate_prefix', side_effect=AssertionError('new decoder')):
                workflow.reconstruct(case, args)
            self.assertEqual(visited, [True])

    def test_missing_original_source_prevents_recreation(self):
        with Case(self.root/'case') as case:
            job = self.create_job(case)
            paths = self.remove_artifacts(case, job, ('output',))
            self.source.unlink()
            with ReaderPool(case.sources()) as pool:
                with self.assertRaises((SafetyError, FileNotFoundError)):
                    restore_fragmented_job(case, pool, job)
            self.assertFalse((case.root/paths['output']).exists())

    def test_changed_original_source_prevents_recreation(self):
        with Case(self.root/'case') as case:
            job = self.create_job(case)
            paths = self.remove_artifacts(case, job, ('output',))
            self.source.write_bytes(self.movie+b'changed')
            with ReaderPool(case.sources()) as pool:
                with self.assertRaises(SafetyError):
                    restore_fragmented_job(case, pool, job)
            self.assertFalse((case.root/paths['output']).exists())

    def test_changed_surviving_evidence_prevents_output_recreation(self):
        with Case(self.root/'case') as case:
            job = self.create_job(case)
            paths = self.remove_artifacts(case, job, ('output',))
            evidence = case.root/paths['evidence']
            evidence.write_bytes(b'changed evidence')
            with ReaderPool(case.sources()) as pool:
                with self.assertRaises(SafetyError):
                    restore_fragmented_job(case, pool, job)
            self.assertFalse((case.root/paths['output']).exists())
            self.assertEqual(evidence.read_bytes(), b'changed evidence')

    def test_interrupted_output_restore_leaves_committed_job_retryable(self):
        with Case(self.root/'case') as case:
            job = self.create_job(case)
            paths = self.remove_artifacts(case, job, ('output', 'manifest', 'evidence'))
            with ReaderPool(case.sources()) as pool, patch.object(pool, 'read', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    restore_fragmented_job(case, pool, job)
            self.assertFalse((case.root/paths['output']).exists())
            self.assertEqual(case.job('fragmented', job['key'])['detail'], job['detail'])
            with ReaderPool(case.sources()) as pool:
                restore_fragmented_job(case, pool, job)
            self.assertEqual((case.root/paths['output']).read_bytes(), self.movie)
            verify_completed(case)

    def test_changed_surviving_manifest_prevents_output_recreation(self):
        with Case(self.root/'case') as case:
            job = self.create_job(case)
            paths = self.remove_artifacts(case, job, ('output',))
            manifest = case.root/paths['manifest']
            manifest.write_bytes(b'changed manifest')
            with ReaderPool(case.sources()) as pool:
                with self.assertRaises(SafetyError):
                    restore_fragmented_job(case, pool, job)
            self.assertFalse((case.root/paths['output']).exists())
            self.assertEqual(manifest.read_bytes(), b'changed manifest')

    def test_committed_range_mismatch_cannot_recreate_output(self):
        with Case(self.root/'case') as case:
            job = self.create_job(case)
            paths = self.remove_artifacts(case, job, ('output', 'evidence'))
            job['detail']['segments'][0]['source_ranges'][0]['source_offset'] += 1
            with ReaderPool(case.sources()) as pool:
                with self.assertRaises(SafetyError):
                    restore_fragmented_job(case, pool, job)
            self.assertFalse((case.root/paths['output']).exists())

    def test_interrupted_metadata_commit_remains_retryable(self):
        with Case(self.root/'case') as case:
            job = self.create_job(case)
            paths = self.remove_artifacts(case, job, ('output', 'manifest', 'evidence'))
            before = case.job('fragmented', job['key'])['detail']
            with ReaderPool(case.sources()) as pool, patch.object(case, 'result', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    restore_fragmented_job(case, pool, job)
            self.assertEqual(case.job('fragmented', job['key'])['detail'], before)
            with ReaderPool(case.sources()) as pool:
                restore_fragmented_job(case, pool, job)
            self.assertEqual((case.root/paths['output']).read_bytes(), self.movie)
            verify_completed(case)

    def test_symlink_destination_is_not_replaced(self):
        with Case(self.root/'case') as case:
            job = self.create_job(case)
            paths = self.remove_artifacts(case, job, ('output',))
            target = self.root/'private-output.mp4'
            target.write_bytes(self.movie)
            output = case.root/paths['output']
            output.symlink_to(target)
            with ReaderPool(case.sources()) as pool:
                with self.assertRaises(SafetyError):
                    restore_fragmented_job(case, pool, job)
            self.assertTrue(output.is_symlink())
            self.assertEqual(target.read_bytes(), self.movie)
