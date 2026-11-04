"""Absent ordinary generated media is restored from retained exact evidence."""
import errno
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_deep_witnesses import structural_movie
from videorebuild import engine
from videorebuild.bmff import make_box
from videorebuild.ranges import AllocationScope
from videorebuild.storage import Case, ReaderPool, SafetyError, scan_logical


class OrdinaryArtifactRestoreTests(unittest.TestCase):
    def fixture(self, root, *, no_links=False, alternative=False):
        token = b'\0\0\0\x04\x65ABC'
        prototype = structural_movie([0], [len(token)])
        movie = structural_movie([len(prototype.data) + 8], [len(token)])
        source = root / 'source.bin'
        source.write_bytes(movie.data + make_box(b'mdat', token) +
                           (make_box(b'mdat', b'\0\0\0\x04\x65ABD') if alternative else b''))
        case = Case(root / 'case').__enter__()
        self.addCleanup(case.__exit__, None, None, None)
        sid = case.register_source(source, 'image' if alternative else 'file')
        scan_logical(case, sid)
        options = engine.Options(reconstruct_only=True)
        if no_links:
            with patch.object(engine.os, 'link', side_effect=OSError(errno.ENOTSUP, 'links unavailable')):
                engine.recover(case, options)
        elif not alternative:
            engine.recover(case, options)
        return case, movie, source, sid, options

    def test_missing_candidate_publication_and_both_restore_without_replaying_search(self):
        for missing in ('candidate', 'publication', 'both'):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                case, movie, source, _, options = self.fixture(Path(directory))
                job = case.job('ordinary', movie.digest)
                candidate = case.root / job['detail']['candidates'][0]['path']
                output = case.root / job['detail']['outputs'][0]
                original = output.read_bytes()
                original_source = source.read_bytes()
                attempts = list(case.db.execute('SELECT * FROM attempts'))
                checkpoints = list(case.db.execute('SELECT * FROM ordinary_resume'))
                if missing in ('candidate', 'both'):
                    candidate.unlink()
                if missing in ('publication', 'both'):
                    output.unlink()
                with patch.object(engine, 'whole_payload', side_effect=AssertionError('completed search repeated')), \
                        patch.object(engine, 'deep_search', side_effect=AssertionError('deep search repeated')), \
                        patch.object(engine, 'validate', side_effect=AssertionError('validation repeated')):
                    engine.recover(case, options)
                self.assertEqual(candidate.read_bytes(), original)
                self.assertEqual(output.read_bytes(), original)
                self.assertEqual(case.job('ordinary', movie.digest), job)
                self.assertEqual(list(case.db.execute('SELECT * FROM attempts')), attempts)
                self.assertEqual(list(case.db.execute('SELECT * FROM ordinary_resume')), checkpoints)
                self.assertEqual(source.read_bytes(), original_source)

    def test_candidate_as_publication_is_recreated_when_links_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            case, movie, _, _, options = self.fixture(Path(directory), no_links=True)
            job = case.job('ordinary', movie.digest)
            candidate = case.root / job['detail']['candidates'][0]['path']
            self.assertEqual(job['detail']['outputs'], [str(candidate.relative_to(case.root))])
            original = candidate.read_bytes()
            candidate.unlink()
            with patch.object(engine.os, 'link', side_effect=OSError(errno.ENOTSUP, 'links unavailable')), \
                    patch.object(engine, 'whole_payload', side_effect=AssertionError('search repeated')):
                engine.recover(case, options)
            self.assertEqual(candidate.read_bytes(), original)
            self.assertEqual(case.job('ordinary', movie.digest), job)

    def test_publication_provenance_can_restore_missing_candidate_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            case, movie, _, _, options = self.fixture(Path(directory))
            job = case.job('ordinary', movie.digest)
            candidate = case.root / job['detail']['candidates'][0]['path']
            output = case.root / job['detail']['outputs'][0]
            original = output.read_bytes()
            candidate.unlink()
            candidate.with_suffix('.json').unlink()
            output.unlink()
            engine.recover(case, options)
            self.assertEqual(candidate.read_bytes(), original)
            self.assertEqual(output.read_bytes(), original)
            manifest = json.loads(candidate.with_suffix('.json').read_text())
            self.assertTrue(manifest['provenance']['chunks'])
            self.assertEqual(case.job('ordinary', movie.digest), job)

    def test_missing_mapping_provenance_refuses_to_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            case, movie, _, _, options = self.fixture(Path(directory))
            job = case.job('ordinary', movie.digest)
            for name in (job['detail']['candidates'][0]['path'], job['detail']['outputs'][0]):
                path = case.root / name
                path.unlink()
                path.with_suffix('.json').unlink()
            with self.assertRaisesRegex(SafetyError, 'Missing source mapping provenance'):
                engine.recover(case, options)
            self.assertEqual(case.job('ordinary', movie.digest), job)

    def test_missing_or_changed_source_blocks_restoration(self):
        for change in ('missing', 'changed'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                case, movie, source, _, options = self.fixture(Path(directory))
                job = case.job('ordinary', movie.digest)
                candidate = case.root / job['detail']['candidates'][0]['path']
                output = case.root / job['detail']['outputs'][0]
                candidate.unlink()
                output.unlink()
                if change == 'missing':
                    source.unlink()
                else:
                    source.write_bytes(source.read_bytes() + b'changed')
                with self.assertRaises((SafetyError, OSError)):
                    engine.recover(case, options)
                self.assertFalse(candidate.exists())
                self.assertFalse(output.exists())
                self.assertEqual(case.job('ordinary', movie.digest), job)

    def test_bad_chunk_provenance_does_not_publish_regenerated_bytes(self):
        for change in ('missing_chunk', 'wrong_hash', 'wrong_mapping'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                case, movie, _, _, options = self.fixture(Path(directory))
                job = case.job('ordinary', movie.digest)
                candidate = case.root / job['detail']['candidates'][0]['path']
                output = case.root / job['detail']['outputs'][0]
                manifest_path = candidate.with_suffix('.json')
                manifest = json.loads(manifest_path.read_text())
                if change == 'missing_chunk':
                    del manifest['provenance']['chunks'][0]['source_offset']
                elif change == 'wrong_hash':
                    manifest['provenance']['chunks'][0]['sha256'] = '0' * 64
                else:
                    manifest['provenance']['chunks'][0]['source_offset'] += 1
                manifest_path.write_text(json.dumps(manifest))
                candidate.unlink()
                output.unlink()
                with self.assertRaises(SafetyError):
                    engine.recover(case, options)
                self.assertFalse(candidate.exists())
                self.assertFalse(output.exists())

    def test_cached_orphan_attempt_recreates_absent_candidate_without_new_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            case, movie, _, sid, options = self.fixture(Path(directory))
            job = case.job('ordinary', movie.digest)
            accepted = job['detail']['candidates'][0]
            candidate = case.root / accepted['path']
            original = candidate.read_bytes()
            candidate.unlink()
            (case.root / job['detail']['outputs'][0]).unlink()
            case.db.execute('DELETE FROM jobs')
            case.db.commit()
            before = list(case.db.execute('SELECT * FROM attempts'))
            mapping = {(movie.chunks[0].track, movie.chunks[0].index): (sid, movie.chunks[0].offset)}
            with ReaderPool(case.sources(), physical_sources=case.physical_sources()) as pool, \
                    patch.object(engine, 'validate', side_effect=AssertionError('validation repeated')):
                restored = engine.attempt(case, movie, mapping, pool, accepted['tools'], options, accepted['stage'])
            self.assertEqual(restored, accepted)
            self.assertEqual(candidate.read_bytes(), original)
            self.assertEqual(list(case.db.execute('SELECT * FROM attempts')), before)
            self.assertFalse(list(case.get_jobs()))

    def test_historical_publication_pair_is_restored_without_changing_current_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            case, movie, _, sid, options = self.fixture(Path(directory), alternative=True)
            first_end = len(movie.data) + 16
            engine.recover(case, options, allocation_scope=AllocationScope({sid: ((0, first_end),)}, 'first'))
            first = case.job('ordinary', movie.digest)
            old_candidate = case.root / first['detail']['candidates'][0]['path']
            old_output = case.root / first['detail']['outputs'][0]
            original = old_output.read_bytes()
            engine.recover(case, options, allocation_scope=AllocationScope(
                {sid: ((0, len(movie.data)), (first_end, 16))}, 'second'))
            current = case.job('ordinary', movie.digest)
            self.assertTrue(current['detail']['historical_publications'])
            old_candidate.unlink()
            old_output.unlink()
            # Direct restoration uses acquisition-bound source evidence, without
            # changing either saved decision or the active search generation.
            with ReaderPool(case.sources(), physical_sources=case.physical_sources()) as pool:
                engine.restore_ordinary_job(case, pool, current)
            self.assertEqual(old_candidate.read_bytes(), original)
            self.assertEqual(old_output.read_bytes(), original)
            self.assertEqual(case.job('ordinary', movie.digest), current)

    def test_repair_checks_only_referenced_sources_and_ignores_historical_mount_device(self):
        with tempfile.TemporaryDirectory() as directory:
            case, movie, source, sid, _ = self.fixture(Path(directory))
            job = case.job('ordinary', movie.digest)
            candidate = case.root / job['detail']['candidates'][0]['path']
            output = case.root / job['detail']['outputs'][0]
            expected = candidate.read_bytes()
            sidecar = candidate.with_suffix('.json')
            manifest = json.loads(sidecar.read_text())
            for chunk in manifest['provenance']['chunks']:
                for part in chunk['physical_extents']:
                    part['source_identity']['dev'] += 1
            sidecar.write_text(json.dumps(manifest))
            candidate.unlink()
            output.unlink()
            sources = case.sources()
            for extra in range(2, 102):
                sources[extra] = dict(sources[sid], id=extra, path=str(source.parent / f'unrelated-{extra}.bin'))
            with ReaderPool(sources, physical_sources=sources) as pool, \
                    patch.object(pool, 'check', wraps=pool.check) as checks:
                engine.restore_ordinary_job(case, pool, job)
            self.assertEqual({call.args[0] for call in checks.call_args_list}, {sid})
            self.assertEqual(candidate.read_bytes(), expected)
            self.assertEqual(output.read_bytes(), expected)

    def test_complete_job_fastpath_does_not_add_media_hashing(self):
        with tempfile.TemporaryDirectory() as directory:
            case, movie, _, _, _ = self.fixture(Path(directory))
            job = case.job('ordinary', movie.digest)
            with ReaderPool(case.sources(), physical_sources=case.physical_sources()) as pool, \
                    patch('videorebuild.artifacts.file_sha256', side_effect=AssertionError('extra media hash')):
                engine.restore_ordinary_job(case, pool, job)


if __name__ == '__main__':
    unittest.main()
