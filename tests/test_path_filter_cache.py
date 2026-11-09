"""Caching immutable path classification never caches mutable source evidence."""
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile
import unittest

from videorebuild.paths import is_ignored_path, is_ignored_source
from videorebuild.storage import ReaderPool, SafetyError


class PathFilterCacheTests(unittest.TestCase):
    def setUp(self):
        is_ignored_path.cache_clear()
        self.addCleanup(is_ignored_path.cache_clear)

    def test_path_types_and_separators_keep_basename_classification(self):
        examples = {
            '.DS_Store': True, '.ds_store': True, '/root/.DS_Store': True,
            '/root/.DS_Store/': True, r'C:\Users\Andrew\.DS_Store': True,
            r'folder\subfolder\.dS_sToRe': True, '.DS_Store.mp4': False,
            '/root/.DS_Store/clip.mp4': False, r'C:\folder\.DS_Store\clip.mp4': False,
            'clip.mp4': False, '.DS_Store.backup': False, ' .DS_Store': False,
            '.DS_Store ': False, '': False, '.': False,
        }
        for value, expected in examples.items():
            for constructor in (str, Path, PurePosixPath):
                with self.subTest(value=value, path_type=constructor.__name__):
                    path = constructor(value)
                    self.assertEqual(is_ignored_path(path), expected)
                    self.assertEqual(is_ignored_path(path), expected)
        self.assertTrue(is_ignored_path(PureWindowsPath(r'C:\folder\.DS_Store')))
        self.assertFalse(is_ignored_path(PureWindowsPath(r'C:\folder\clip.mp4')))

    def test_classification_retains_at_most_4096_keys(self):
        for index in range(5000):
            self.assertFalse(is_ignored_path(f'/bounded-cache/{index}/clip.mp4'))
        state = is_ignored_path.cache_info()
        self.assertLessEqual(state.maxsize, 4096)
        self.assertLessEqual(state.currsize, 4096)
        self.assertTrue(is_ignored_path('/bounded-cache/.DS_Store'))
        self.assertFalse(is_ignored_path('/bounded-cache/.DS_Store/clip.mp4'))

    def test_mutable_logical_source_binding_is_reclassified(self):
        row = {'path': 'logical:source', 'kind': 'logical', 'binding': {'name': 'clip.mp4'}}
        self.assertFalse(is_ignored_source(row))
        row['binding']['name'] = '.DS_Store'
        self.assertTrue(is_ignored_source(row))
        row['binding'] = '{"name":"clip.mp4"}'
        self.assertFalse(is_ignored_source(row))
        row['binding'] = '{"name":"folder\\\\.DS_Store"}'
        self.assertTrue(is_ignored_source(row))
        row['binding'] = {'name': 'clip.mp4'}
        row['path'] = '/source/.DS_Store'
        self.assertTrue(is_ignored_source(row))

    def test_cached_path_result_does_not_hide_changed_source_ctime(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'source.bin'
            source.write_bytes(b'original')
            before = source.stat()
            row = dict(path=str(source), kind='file', size=before.st_size,
                       mtime_ns=before.st_mtime_ns, ino=before.st_ino, dev=before.st_dev)
            with ReaderPool({1: row}) as pool:
                self.assertEqual(pool.read(1, 0, 8), b'original')
                source.write_bytes(b'changed!')
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
                self.assertNotEqual(source.stat().st_ctime_ns, before.st_ctime_ns)
                with self.assertRaises(SafetyError):
                    pool.read(1, 0, 8)

    def test_changed_source_path_to_ignored_metadata_is_rejected_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.bin'
            metadata = root/'.DS_Store'
            source.write_bytes(b'original')
            metadata.write_bytes(b'preserve Finder metadata')
            before = source.stat()
            row = dict(path=str(source), kind='file', size=before.st_size,
                       mtime_ns=before.st_mtime_ns, ino=before.st_ino, dev=before.st_dev)
            self.assertTrue(is_ignored_path(metadata))
            with ReaderPool({1: row}) as pool:
                self.assertEqual(pool.read(1, 0, 8), b'original')
                row['path'] = str(metadata)
                with self.assertRaises(SafetyError):
                    pool.read(1, 0, 8)
            self.assertEqual(metadata.read_bytes(), b'preserve Finder metadata')
            self.assertEqual(source.read_bytes(), b'original')


if __name__ == '__main__':
    unittest.main()
