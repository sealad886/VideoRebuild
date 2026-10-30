import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.cli import main
from videorebuild.storage import Case, SafetyError, atomic_text, create_private_json, file_sha256, write_json


class StorageTests(unittest.TestCase):
    def test_report_formats_preserve_source_hardlinks(self):
        for report_name in ('summary.json', 'results.csv', 'index.html'):
            with self.subTest(report=report_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'original.bin'
                original = b'synthetic immutable source'
                source.write_bytes(original)
                case = root / 'case'
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['scan', '--case', str(case), '--input', str(source), '--all-files']), 0)
                    alias = case / 'reports' / 'summary.json.tmp'
                    os.link(source, alias)
                    target = case / 'reports' / report_name
                    target.unlink()
                    os.link(source, target)
                    self.assertEqual(main(['report', '--case', str(case)]), 0)
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(alias.read_bytes(), original)
                self.assertEqual(json.loads((case / 'reports' / 'summary.json').read_text())['sources'], 1)
                self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)

    def test_atomic_text_failure_preserves_old_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.txt'
            target.write_bytes(b'old report')
            with self.assertRaises(ValueError):
                with atomic_text(target) as stream:
                    stream.write('partial replacement')
                    raise ValueError('report generation failed')
            self.assertEqual(target.read_bytes(), b'old report')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_json_rejects_symlink_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.write_bytes(b'original')
            target = root / 'report.json'
            target.symlink_to(source)
            with self.assertRaises(SafetyError):
                write_json(target, {'new': True})
            with self.assertRaises(FileExistsError):
                create_private_json(target, {'new': True})
            self.assertTrue(target.is_symlink())
            self.assertEqual(source.read_bytes(), b'original')

    def test_atomic_json_failure_preserves_old_target_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.json'
            target.write_bytes(b'old report')
            with patch('videorebuild.storage.os.fsync', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    write_json(target, {'new': True})
            self.assertEqual(target.read_bytes(), b'old report')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_json_outputs_private_and_fresh_output_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous_umask = os.umask(0)
            try:
                for writer in (write_json, create_private_json):
                    with self.subTest(writer=writer.__name__):
                        target = root / (writer.__name__ + '.json')
                        value = {'unicode': 'café', 'items': [1, None]}
                        writer(target, value)
                        self.assertEqual(json.loads(target.read_text()), value)
                        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                fresh = root / 'create_private_json.json'
                previous = fresh.read_bytes()
                with self.assertRaises(FileExistsError):
                    create_private_json(fresh, {'replacement': True})
                self.assertEqual(fresh.read_bytes(), previous)
            finally:
                os.umask(previous_umask)

    def test_hash_empty_and_chunk_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'data'
            for size in (0, 1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 1, 2 * 1024 * 1024 + 7):
                with self.subTest(size=size):
                    data = (bytes(range(256)) * (size // 256 + 1))[:size]
                    target.write_bytes(data)
                    self.assertEqual(file_sha256(target), hashlib.sha256(data).hexdigest())

    def test_new_case_private_existing_case_permissions_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / 'existing'
            existing.mkdir(mode=0o750)
            existing.chmod(0o750)
            previous_umask = os.umask(0)
            try:
                for path, expected_mode in ((root / 'new', 0o700), (existing, 0o750)):
                    with self.subTest(path=path.name), Case(path):
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected_mode)
            finally:
                os.umask(previous_umask)


if __name__ == '__main__':
    unittest.main()
