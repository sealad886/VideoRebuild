import errno
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from videorebuild.artifacts import ensure_generated_file, safe_case_path
from videorebuild.storage import SafetyError


class GeneratedFileRestoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.case = SimpleNamespace(root=self.root / 'case')
        (self.case.root / 'reconstructed').mkdir(parents=True)
        (self.case.root / 'candidates').mkdir()
        self.data = b'exact previously validated generated media'
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.relative = 'reconstructed/output.mp4'
        self.output = self.case.root / self.relative
        self.peer = self.case.root / 'candidates/candidate.mp4'

    def assert_no_staging(self):
        self.assertEqual(list(self.case.root.rglob('.videorebuild-restore-*')), [])

    def test_existing_healthy_file_preserves_inode_and_skips_rebuild(self):
        self.output.write_bytes(self.data)
        inode = self.output.stat().st_ino
        def forbidden(_):
            self.fail('healthy output must not be rebuilt')
        result = ensure_generated_file(self.case, self.relative, self.digest,
                                       expected_size=len(self.data), rebuild=forbidden)
        self.assertEqual(result, self.output)
        self.assertEqual(self.output.stat().st_ino, inode)

    def test_missing_output_reuses_verified_peer_with_hardlink(self):
        self.peer.write_bytes(self.data)
        ensure_generated_file(self.case, self.relative, self.digest,
                              peers=('candidates/missing.mp4', self.peer.relative_to(self.case.root)))
        self.assertEqual(self.output.read_bytes(), self.data)
        self.assertEqual(self.output.stat().st_ino, self.peer.stat().st_ino)
        self.assert_no_staging()

    def test_peer_copy_fallback_preserves_bytes_without_hardlinks(self):
        self.peer.write_bytes(self.data)
        with patch('videorebuild.artifacts.os.link', side_effect=OSError(errno.EOPNOTSUPP, 'unsupported')):
            ensure_generated_file(self.case, self.relative, self.digest,
                                  peers=('candidates/candidate.mp4',))
        self.assertEqual(self.output.read_bytes(), self.data)
        self.assertNotEqual(self.output.stat().st_ino, self.peer.stat().st_ino)

    def test_missing_file_is_rebuilt_in_private_same_volume_staging(self):
        def rebuild(path):
            self.assertFalse(path.exists())
            self.assertEqual(path.parent.parent, self.output.parent)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            path.write_bytes(self.data)
        ensure_generated_file(self.case, self.relative, self.digest,
                              expected_size=len(self.data), rebuild=rebuild)
        self.assertEqual(self.output.read_bytes(), self.data)
        self.assert_no_staging()

    def test_missing_file_without_evidence_fails_without_creation(self):
        with self.assertRaises(SafetyError):
            ensure_generated_file(self.case, self.relative, self.digest)
        self.assertFalse(self.output.exists())

    def test_changed_existing_file_is_preserved_even_with_healthy_peer(self):
        self.output.write_bytes(b'user changed file')
        self.peer.write_bytes(self.data)
        with self.assertRaises(SafetyError):
            ensure_generated_file(self.case, self.relative, self.digest,
                                  peers=('candidates/candidate.mp4',))
        self.assertEqual(self.output.read_bytes(), b'user changed file')

    def test_rebuilt_hash_or_size_mismatch_never_publishes(self):
        for wrong_bytes, expected_size in ((b'wrong bytes', None), (self.data, len(self.data)+1)):
            with self.subTest(size=expected_size), self.assertRaises(SafetyError):
                ensure_generated_file(self.case, self.relative, self.digest,
                                      rebuild=lambda path: path.write_bytes(wrong_bytes),
                                      expected_size=expected_size)
            self.assertFalse(self.output.exists())
            self.assert_no_staging()

    def test_changed_peer_is_rejected_without_rebuild_or_output(self):
        self.peer.write_bytes(b'changed peer')
        def forbidden(_):
            self.fail('changed peer must remain an error')
        with self.assertRaises(SafetyError):
            ensure_generated_file(self.case, self.relative, self.digest,
                                  peers=('candidates/candidate.mp4',), rebuild=forbidden)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.peer.read_bytes(), b'changed peer')

    def test_unsafe_paths_and_parent_symlinks_preserve_external_files(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'file.mp4').write_bytes(self.data)
        (self.case.root / 'alias').symlink_to(outside, target_is_directory=True)
        for name in ('../outside/file.mp4', str(outside / 'file.mp4'), 'alias/file.mp4', '.'):
            with self.subTest(name=name), self.assertRaises(SafetyError):
                ensure_generated_file(self.case, name, self.digest,
                                      rebuild=lambda path: path.write_bytes(self.data))
        self.assertEqual((outside / 'file.mp4').read_bytes(), self.data)
        self.assert_no_staging()

    def test_existing_and_dangling_symlinks_remain_errors(self):
        for target in (self.peer, self.root / 'absent'):
            self.output.symlink_to(target)
            with self.assertRaises(SafetyError):
                ensure_generated_file(self.case, self.relative, self.digest,
                                      rebuild=lambda path: path.write_bytes(self.data))
            self.assertTrue(self.output.is_symlink())
            self.output.unlink()

    def test_directory_destination_is_preserved(self):
        self.output.mkdir()
        with self.assertRaises(SafetyError):
            ensure_generated_file(self.case, self.relative, self.digest)
        self.assertTrue(self.output.is_dir())

    def test_interrupted_or_failed_source_callback_cleans_staging(self):
        for error in (KeyboardInterrupt(), SafetyError('source changed')):
            def rebuild(path):
                path.write_bytes(self.data[:3])
                raise error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                ensure_generated_file(self.case, self.relative, self.digest, rebuild=rebuild)
            self.assertFalse(self.output.exists())
            self.assert_no_staging()

    def test_interrupted_copy_cleans_only_its_partial_destination(self):
        self.peer.write_bytes(self.data)
        def interrupted(source, destination, **_):
            destination.write(source.read(3))
            raise KeyboardInterrupt
        with patch('videorebuild.artifacts.os.link', side_effect=OSError(errno.EOPNOTSUPP, 'unsupported')), \
                patch('videorebuild.artifacts.shutil.copyfileobj', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                ensure_generated_file(self.case, self.relative, self.digest,
                                      peers=('candidates/candidate.mp4',))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.peer.read_bytes(), self.data)

    def test_newly_appearing_destination_is_never_overwritten(self):
        for hardlinks in (True, False):
            def rebuild(path):
                path.write_bytes(self.data)
                self.output.write_bytes(b'appeared independently')
            with self.subTest(hardlinks=hardlinks):
                link = os.link
                def install(source, destination):
                    if not hardlinks:
                        raise OSError(errno.EOPNOTSUPP, 'unsupported')
                    return link(source, destination)
                with patch('videorebuild.artifacts.os.link', side_effect=install), self.assertRaises(SafetyError):
                    ensure_generated_file(self.case, self.relative, self.digest, rebuild=rebuild)
                self.assertEqual(self.output.read_bytes(), b'appeared independently')
                self.output.unlink()
                self.assert_no_staging()

    def test_missing_parent_is_created_and_safe_path_does_not_create_it(self):
        relative = 'reconstructed/nested/output.mp4'
        path = safe_case_path(self.case, relative)
        self.assertFalse(path.parent.exists())
        ensure_generated_file(self.case, relative, self.digest,
                              rebuild=lambda staged: staged.write_bytes(self.data))
        self.assertEqual(path.read_bytes(), self.data)


if __name__ == '__main__':
    unittest.main()
