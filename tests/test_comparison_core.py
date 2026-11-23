"""Behavioral proof that comparison never hides additional recovered footage."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from helpers import generate
from videorebuild import comparison
from videorebuild.storage import SafetyError


class ComparisonCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('ffmpeg'):
            raise unittest.SkipTest('FFmpeg fixture generation is unavailable')
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.base = Path(cls.fixtures.name).resolve()
        cls.full = cls.base/'full.mp4'
        generate(cls.full, audio=False, seconds=3)
        cls.prefix = cls.base/'prefix.mp4'
        cls.remux = cls.base/'remux.mp4'
        cls.fragmented = cls.base/'fragmented.mp4'
        for destination, extra in [(cls.prefix, ['-t', '1']),
                                   (cls.remux, ['-movflags', '+faststart']),
                                   (cls.fragmented, ['-movflags', 'empty_moov+default_base_moof+frag_keyframe'])]:
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(cls.full),
                            *extra, '-c', 'copy', '-y', str(destination)],
                           check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.a, self.b, self.output = self.root/'A', self.root/'B', self.root/'report'
        self.a.mkdir()
        self.b.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def compare(self, **kwargs):
        summary = comparison.compare_directories(self.a, self.b, self.output, **kwargs)
        document = json.loads((self.output/'comparison.json').read_text())
        return summary, document['results']

    def test_complete_recovered_prefix_inside_longer_archive_is_represented(self):
        shutil.copyfile(self.prefix, self.a/'recovered.mp4')
        nested = self.b/'year'/'camera'
        nested.mkdir(parents=True)
        shutil.copyfile(self.full, nested/'transferred.mp4')
        before = {p: p.read_bytes() for p in [self.a/'recovered.mp4', nested/'transferred.mp4']}
        summary, rows = self.compare()
        self.assertTrue(summary['complete'])
        self.assertEqual(rows[0]['status'], 'represented_encoded')
        self.assertEqual(summary['attention_files'], 0)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in before.items()))

    def test_archive_prefix_cannot_hide_longer_recovered_video(self):
        shutil.copyfile(self.full, self.a/'long.mp4')
        shutil.copyfile(self.prefix, self.b/'short.mp4')
        summary, rows = self.compare()
        self.assertTrue(summary['complete'])
        self.assertEqual(rows[0]['status'], 'no_match')
        self.assertIn(str(self.a/'long.mp4'), (self.output/'missing.csv').read_text())

    def test_fragmented_and_ordinary_containers_match_complete_video_payload(self):
        shutil.copyfile(self.fragmented, self.a/'fragmented.mp4')
        shutil.copyfile(self.full, self.b/'ordinary.mp4')
        summary, rows = self.compare()
        self.assertTrue(summary['complete'])
        self.assertEqual(rows[0]['status'], 'represented_encoded')

    def test_same_sample_sizes_and_stale_timestamps_do_not_prove_payload_match(self):
        shutil.copyfile(self.full, self.a/'recovered.mp4')
        target = self.b/'archive.mp4'
        shutil.copyfile(self.remux, target)
        first, rows = self.compare()
        self.assertEqual(rows[0]['status'], 'represented_encoded')
        self.assertEqual(first['archive_cache_hits'], 0)
        warm, _ = self.compare()
        self.assertEqual(warm['archive_cache_hits'], 1)
        media = comparison._media(target)
        stamp = target.stat()
        offset, size = media['tracks'][0]['samples'][-1]
        with target.open('r+b') as handle:
            handle.seek(offset+size-1)
            value = handle.read(1)
            handle.seek(offset+size-1)
            handle.write(bytes([value[0] ^ 1]))
        os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        summary, rows = self.compare()
        self.assertEqual(rows[0]['status'], 'no_match')
        self.assertEqual(summary['attention_files'], 1)

    def test_unreadable_media_remains_in_attention_and_finder_metadata_is_ignored(self):
        (self.a/'broken.mp4').write_bytes(b'not an MP4')
        (self.a/'.DS_Store').write_bytes(b'Finder metadata')
        hidden = self.a/'.DS_Store'
        hidden.unlink()
        hidden.mkdir()
        (hidden/'not-evidence.mp4').write_bytes(b'ignored')
        summary, rows = self.compare()
        self.assertFalse(summary['complete'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'unreadable')
        self.assertIn('broken.mp4', (self.output/'missing.csv').read_text())
        self.assertNotIn('not-evidence', (self.output/'missing.csv').read_text())

    def test_arbitrary_container_byte_copy_is_detected_even_if_unsupported(self):
        (self.a/'movie.avi').write_bytes(b'example unsupported video bytes')
        (self.b/'renamed.avi').write_bytes(b'example unsupported video bytes')
        _, rows = self.compare()
        self.assertEqual(rows[0]['status'], 'represented_exact')

    def test_growing_recovered_folder_has_explicit_snapshot_drift(self):
        shutil.copyfile(self.full, self.a/'first.mp4')
        shutil.copyfile(self.full, self.b/'copy.mp4')
        same = comparison._same_file

        def publish(*args):
            shutil.copyfile(self.full, self.a/'newly-published.mp4')
            return same(*args)

        with patch.object(comparison, '_same_file', side_effect=publish):
            summary, rows = self.compare()
        self.assertFalse(summary['complete'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(summary['recovered_snapshot']['added'],
                         [str(self.a/'newly-published.mp4')])

    def test_archive_mutation_after_match_invalidates_positive_proof(self):
        shutil.copyfile(self.full, self.a/'first.mp4')
        target = self.b/'copy.mp4'
        shutil.copyfile(self.full, target)
        same = comparison._same_file

        def mutate_after_proof(*args):
            result = same(*args)
            with target.open('ab') as handle:
                handle.write(b'changed after proof')
            return result

        with patch.object(comparison, '_same_file', side_effect=mutate_after_proof):
            summary, rows = self.compare()
        self.assertFalse(summary['complete'])
        self.assertEqual(rows[0]['status'], 'unreadable')
        self.assertEqual(summary['attention_files'], 1)
        self.assertEqual(summary['archive_snapshot']['changed'], [str(target)])
        self.assertIn('first.mp4', (self.output/'missing.csv').read_text())

    def test_output_cannot_write_into_source_or_existing_hardlink(self):
        with self.assertRaises(SafetyError):
            comparison.compare_directories(self.a, self.b, self.a/'report')
        alias = self.root/'a'
        if alias.is_dir():
            with self.assertRaises(SafetyError):
                comparison.compare_directories(self.a, self.b, alias/'report')
            self.assertFalse((self.a/'report').exists())
        self.output.mkdir()
        source = self.a/'private.mp4'
        source.write_bytes(b'private source')
        os.link(source, self.output/'comparison.json')
        with self.assertRaises(SafetyError):
            self.compare()
        self.assertEqual(source.read_bytes(), b'private source')

    def test_same_output_rejects_other_comparison_while_a_run_is_active(self):
        shutil.copyfile(self.full, self.a/'recovered.mp4')
        shutil.copyfile(self.full, self.b/'archive.mp4')
        empty_archive = self.root/'empty-archive'
        empty_archive.mkdir()
        same = comparison._same_file

        def overlap(*args):
            with self.assertRaisesRegex(SafetyError, 'Another comparison'):
                comparison.compare_directories(self.a, empty_archive, self.output)
            return same(*args)

        with patch.object(comparison, '_same_file', side_effect=overlap) as proof:
            summary, rows = self.compare()
        self.assertTrue(proof.called)
        self.assertTrue(summary['complete'])
        self.assertEqual(rows[0]['status'], 'represented_exact')
        self.assertTrue(self.compare()[0]['complete'])
