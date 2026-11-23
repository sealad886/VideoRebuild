from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.visual_comparison import find_visual_candidates


FFMPEG = shutil.which('ffmpeg')


@unittest.skipUnless(FFMPEG, 'FFmpeg is required for visual comparison integration tests')
class VisualComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / 'cache'
        self.cache.mkdir()

    def encode(self, name, *, source=None, duration=6, offset=None, pattern=None):
        target = self.root / name
        args = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin']
        if source is None:
            args += ['-f', 'lavfi', '-i', pattern or f'testsrc2=size=160x96:rate=12:duration={duration}']
        else:
            if offset is not None:
                args += ['-ss', str(offset)]
            args += ['-i', str(source), '-t', str(duration), '-vf', 'scale=128:72']
        args += ['-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1', '-crf', '30', '-y', str(target)]
        subprocess.run(args, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=30)
        return target

    def test_reencoded_video_is_review_suggestion_and_uses_cache(self):
        archive = self.encode('archive.mkv')
        recovered = self.encode('recovered.mp4', source=archive)
        original_bytes = recovered.read_bytes(), archive.read_bytes()
        first = find_visual_candidates([recovered], [archive], cache_dir=self.cache, timeout=30)
        candidates = first[str(recovered)]
        self.assertTrue(candidates, first.errors)
        self.assertEqual(candidates[0]['archive_path'], str(archive))
        self.assertGreater(candidates[0]['score'], .8)
        self.assertIn('Review required', candidates[0]['limitations'])
        self.assertTrue(first.complete)
        self.assertEqual((recovered.read_bytes(), archive.read_bytes()), original_bytes)
        with patch('videorebuild.visual_comparison._decode', side_effect=AssertionError('cache not used')):
            cached = find_visual_candidates([recovered], [archive], cache_dir=self.cache, timeout=30)
        self.assertEqual(dict(cached), dict(first))

    def test_trimmed_reencoded_video_nominates_longer_archive(self):
        archive = self.encode('archive.mp4', duration=10)
        recovered = self.encode('segment.mp4', source=archive, offset=3, duration=4)
        result = find_visual_candidates([recovered], [archive], cache_dir=self.cache, timeout=30)
        self.assertTrue(result[str(recovered)], result.errors)
        self.assertAlmostEqual(result[str(recovered)][0]['offset_seconds'], 3, delta=1)

    def test_static_scene_does_not_nominate_a_duplicate(self):
        archive = self.encode('archive.mp4', pattern='color=c=gray:size=160x96:rate=12:duration=6')
        recovered = self.encode('recovered.mp4', source=archive)
        result = find_visual_candidates([recovered], [archive], cache_dir=self.cache, timeout=30)
        self.assertEqual(result[str(recovered)], [])
        self.assertTrue(any('insufficient temporal variation' in item for item in result.limitations))

    def test_unreadable_archive_remains_visible(self):
        recovered = self.encode('recovered.mp4')
        archive = self.root / 'broken.mp4'
        archive.write_bytes(b'not video')
        result = find_visual_candidates([recovered], [archive], cache_dir=self.cache, timeout=30)
        self.assertEqual(result[str(recovered)], [])
        self.assertFalse(result.complete)
        self.assertEqual(result.errors[0]['path'], str(archive))

    def test_unadvertised_container_is_rejected_even_with_a_video_extension(self):
        source = self.encode('source.mp4')
        other = self.root / 'unadvertised.mp4'
        subprocess.run([FFMPEG, '-v', 'error', '-nostdin', '-i', str(source),
                        '-c', 'copy', '-f', 'nut', str(other)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        result = find_visual_candidates([other], [], cache_dir=self.cache, timeout=30)
        self.assertFalse(result.complete)
        self.assertEqual(result.errors[0]['path'], str(other))

    def test_changed_bytes_invalidate_cache_even_when_mtime_is_restored(self):
        recovered = self.encode('recovered.mp4')
        archive = self.encode('archive.mp4', source=recovered)
        find_visual_candidates([recovered], [archive], cache_dir=self.cache, timeout=30)
        original = archive.stat()
        changed = bytearray(archive.read_bytes())
        changed[-1] ^= 1
        archive.write_bytes(changed)
        os.utime(archive, ns=(original.st_atime_ns, original.st_mtime_ns))
        from videorebuild.visual_comparison import _decode
        calls = []

        def decode(path, executable, timeout):
            calls.append(path)
            return _decode(path, executable, timeout)

        with patch('videorebuild.visual_comparison._decode', side_effect=decode):
            find_visual_candidates([recovered], [archive], cache_dir=self.cache, timeout=30)
        self.assertEqual(calls, [archive])

    def test_sampling_limit_is_explicit(self):
        recovered = self.encode('long.mp4', pattern='testsrc2=size=32x24:rate=1:duration=303')
        result = find_visual_candidates([recovered], [], cache_dir=self.cache, timeout=30)
        self.assertFalse(result.complete)
        self.assertTrue(any('sampling limit' in item for item in result.limitations))

    def test_symlink_cache_is_rejected_without_modifying_target(self):
        recovered = self.encode('recovered.mp4')
        external = self.root / 'existing'
        external.write_bytes(b'keep')
        for suffix in ('', '-journal', '-wal', '-shm'):
            with self.subTest(suffix=suffix):
                link = self.cache / ('visual-cache.sqlite3' + suffix)
                link.symlink_to(external)
                try:
                    with self.assertRaises(ValueError):
                        find_visual_candidates([recovered], [], cache_dir=self.cache, timeout=30)
                    self.assertEqual(external.read_bytes(), b'keep')
                finally:
                    link.unlink(missing_ok=True)

    def test_ds_store_is_ignored_before_decode(self):
        finder = self.root / '.DS_Store'
        finder.write_bytes(b'finder')
        with patch('videorebuild.visual_comparison._decode', side_effect=AssertionError('Finder metadata decoded')):
            result = find_visual_candidates([finder], [finder], cache_dir=self.cache, timeout=30)
        self.assertEqual(result, {})


if __name__ == '__main__':
    unittest.main()
