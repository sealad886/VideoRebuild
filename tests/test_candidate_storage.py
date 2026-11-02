"""Accepted ordinary artifacts never need a second publication allocation."""
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
from videorebuild.storage import Case, SafetyError, scan_logical


class CandidateStorageTests(unittest.TestCase):
    def fixture(self, root, *, second=False):
        token = b'\0\0\0\x04\x65ABC'
        prototype = structural_movie([0], [len(token)])
        movie = structural_movie([len(prototype.data)+8], [len(token)])
        source = root/'source.bin'
        source.write_bytes(movie.data+make_box(b'mdat', token) +
                           (make_box(b'mdat', b'\0\0\0\x04\x65ABD') if second else b''))
        case = Case(root/'case').__enter__()
        self.addCleanup(case.__exit__, None, None, None)
        sid = case.register_source(source, 'image' if second else 'file')
        scan_logical(case, sid)
        return case, movie, source, sid

    def test_unsupported_links_publish_existing_candidate_and_complete_manifest(self):
        for code in sorted({errno.EPERM, errno.EXDEV, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}):
            with self.subTest(errno=code), tempfile.TemporaryDirectory() as directory:
                case, movie, source, _ = self.fixture(Path(directory))
                source_before = source.read_bytes()
                with patch.object(engine.os, 'link', side_effect=OSError(code, 'links unavailable')):
                    engine.recover(case, engine.Options(reconstruct_only=True))
                job = case.job('ordinary', movie.digest)
                self.assertEqual(job['status'], 'reconstructed')
                self.assertEqual(job['detail']['outputs'], [job['detail']['candidates'][0]['path']])
                output = case.root/job['detail']['outputs'][0]
                self.assertEqual(list((case.root/'candidates').glob('*.mp4')), [output])
                self.assertFalse(list((case.root/'reconstructed').glob('*.mp4')))
                manifest = json.loads(output.with_suffix('.json').read_text())
                self.assertTrue(manifest['ordered_fragments'])
                self.assertTrue(manifest['provenance']['chunks'])
                self.assertFalse(manifest['validation']['decode_checked'])
                self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), job['detail']['candidates'][0]['sha256'])
                self.assertEqual(source.read_bytes(), source_before)

    def test_canonical_candidate_output_resumes_without_reconstruction_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case, movie, _, _ = self.fixture(root)
            options = engine.Options(reconstruct_only=True)
            with patch.object(engine.os, 'link', side_effect=OSError(errno.ENOTSUP, 'links unavailable')):
                engine.recover(case, options)
            job = case.job('ordinary', movie.digest)
            output = case.root/job['detail']['outputs'][0]
            original = output.read_bytes()
            before = (output.stat().st_ino, output.stat().st_mtime_ns)
            with patch.object(engine, 'reconstruct', side_effect=AssertionError('completed output recopied')):
                engine.recover(case, options)
            self.assertEqual(case.job('ordinary', movie.digest)['detail']['outputs'], job['detail']['outputs'])
            self.assertEqual((output.stat().st_ino, output.stat().st_mtime_ns), before)
            output.write_bytes(original+b'changed')
            with self.assertRaises(SafetyError):
                engine.recover(case, options)

    def test_interruption_before_link_can_resume_from_one_validated_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            case, movie, _, _ = self.fixture(Path(directory))
            options = engine.Options(reconstruct_only=True)
            with patch.object(engine.os, 'link', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    engine.recover(case, options)
            self.assertEqual(len(list((case.root/'candidates').glob('*.mp4'))), 1)
            self.assertFalse(list((case.root/'reconstructed').glob('*.mp4')))
            with patch.object(engine.os, 'link', side_effect=OSError(errno.ENOTSUP, 'links unavailable')), \
                    patch.object(engine, 'reconstruct', side_effect=AssertionError('validated candidate recopied')):
                engine.recover(case, options)
            job = case.job('ordinary', movie.digest)
            self.assertEqual(job['status'], 'reconstructed')
            self.assertEqual(job['detail']['outputs'], [job['detail']['candidates'][0]['path']])

    def test_new_candidate_preserves_different_historical_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            case, movie, _, sid = self.fixture(Path(directory), second=True)
            options = engine.Options(reconstruct_only=True)
            first_end = len(movie.data)+16
            first_scope = AllocationScope({sid: ((0, first_end),)}, 'first-only')
            engine.recover(case, options, allocation_scope=first_scope)
            first = case.job('ordinary', movie.digest)
            output = case.root/first['detail']['outputs'][0]
            before = (output.stat().st_ino, output.stat().st_mtime_ns, output.read_bytes())
            second_scope = AllocationScope({sid: ((0, len(movie.data)), (first_end, 16))}, 'second-only')
            with patch.object(engine.os, 'link', side_effect=OSError(errno.ENOTSUP, 'links unavailable')):
                engine.recover(case, options, allocation_scope=second_scope)
            current = case.job('ordinary', movie.digest)
            self.assertEqual(current['status'], 'reconstructed')
            self.assertNotEqual(current['detail']['outputs'], first['detail']['outputs'])
            self.assertEqual(current['detail']['outputs'], [current['detail']['candidates'][0]['path']])
            self.assertIn(first['detail']['outputs'][0], current['detail']['historical_outputs'])
            self.assertEqual((output.stat().st_ino, output.stat().st_mtime_ns, output.read_bytes()), before)
            self.assertEqual(len(list((case.root/'reconstructed').glob('*.mp4'))), 1)

    def test_non_link_related_failure_is_not_hidden_by_candidate_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            case, _, _, _ = self.fixture(Path(directory))
            with patch.object(engine.os, 'link', side_effect=OSError(errno.EIO, 'I/O failure')):
                with self.assertRaises(OSError) as raised:
                    engine.recover(case, engine.Options(reconstruct_only=True))
            self.assertEqual(raised.exception.errno, errno.EIO)
            self.assertFalse(list((case.root/'reconstructed').glob('*.mp4')))


if __name__ == '__main__':
    unittest.main()
