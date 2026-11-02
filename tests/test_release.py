"""Release artifacts must preserve source identity and the one canonical version."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile


spec = importlib.util.spec_from_file_location(
    'check_release', Path(__file__).resolve().parents[1] / 'tools/check_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dist = self.root / 'dist'
        self.dist.mkdir()
        package = self.root / 'src/videorebuild'
        package.mkdir(parents=True)
        self.source = b'__version__ = "0.4.0"\n'
        (package / '__init__.py').write_bytes(self.source)
        (self.root / 'pyproject.toml').write_text(
            '[project]\nname = "videorebuild"\ndynamic = ["version"]\n'
            '[tool.setuptools.dynamic]\nversion = {attr = "videorebuild.__version__"}\n')
        self.metadata = b'Name: videorebuild\nVersion: 0.4.0\n'
        self.write_artifacts()

    def write_artifacts(self, wheel_metadata=None, sdist_metadata=None, package_source=None):
        code = self.source if package_source is None else package_source
        with zipfile.ZipFile(self.dist / 'videorebuild-0.4.0-py3-none-any.whl', 'w') as archive:
            archive.writestr('videorebuild/__init__.py', code)
            archive.writestr('videorebuild-0.4.0.dist-info/METADATA',
                             self.metadata if wheel_metadata is None else wheel_metadata)
        with zipfile.ZipFile(self.dist / 'videorebuild.pyz', 'w') as archive:
            archive.writestr('videorebuild/__init__.py', self.source)
        with tarfile.open(self.dist / 'videorebuild-0.4.0.tar.gz', 'w:gz') as archive:
            for name, data in {
                    'videorebuild-0.4.0/PKG-INFO': self.metadata if sdist_metadata is None else sdist_metadata,
                    'videorebuild-0.4.0/src/videorebuild/__init__.py': self.source}.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def test_matching_artifacts_write_verifiable_revision_and_checksums(self):
        revision = 'a' * 40
        names = release.check_release(self.root, self.dist, '0.4.0', revision, True)
        self.assertEqual((self.dist / 'source-revision.txt').read_text(), revision + '\n')
        checksums = dict(line.split('  ', 1)[::-1]
                         for line in (self.dist / 'SHA256SUMS.txt').read_text().splitlines())
        self.assertEqual(set(checksums), set(names) | {'source-revision.txt'})
        for name, digest in checksums.items():
            self.assertEqual(hashlib.sha256((self.dist / name).read_bytes()).hexdigest(), digest)

    def test_wheel_and_sdist_metadata_must_match_source(self):
        for artifact in ('wheel', 'sdist'):
            with self.subTest(artifact=artifact):
                kwargs = {artifact + '_metadata': b'Name: videorebuild\nVersion: 0.3.0\n'}
                self.write_artifacts(**kwargs)
                with self.assertRaisesRegex(ValueError, 'name/version differs'):
                    release.check_release(self.root, self.dist, '0.4.0')

    def test_code_drift_is_rejected_even_when_versions_match(self):
        self.write_artifacts(package_source=self.source + b'# stale package code\n')
        with self.assertRaisesRegex(ValueError, 'package source differs'):
            release.check_release(self.root, self.dist, '0.4.0')

    def test_missing_or_additional_artifacts_are_rejected(self):
        (self.dist / 'videorebuild.pyz').unlink()
        with self.assertRaisesRegex(ValueError, 'exactly one wheel, sdist and zipapp'):
            release.check_release(self.root, self.dist, '0.4.0')
        self.write_artifacts()
        (self.dist / 'videorebuild-0.3.0.tar.gz').write_bytes(b'stale build')
        with self.assertRaisesRegex(ValueError, 'exactly one wheel, sdist and zipapp'):
            release.check_release(self.root, self.dist, '0.4.0')

    def test_duplicate_metadata_fields_and_malformed_archives_are_rejected(self):
        self.write_artifacts(wheel_metadata=self.metadata + b'Version: 0.4.0\n')
        with self.assertRaisesRegex(ValueError, 'name/version differs'):
            release.check_release(self.root, self.dist, '0.4.0')
        self.write_artifacts()
        (self.dist / 'videorebuild.pyz').write_bytes(b'not a zipapp')
        with self.assertRaises(zipfile.BadZipFile):
            release.check_release(self.root, self.dist, '0.4.0')

    def test_only_stable_versions_and_exact_revision_evidence_are_accepted(self):
        for version in ('0.4', '00.4.0', '0.4.0rc1', '0.4.0+local', '0.4.1'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                release.check_release(self.root, self.dist, version)
        for revision in ('main', 'abc1234', 'A' * 40, None):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                release.check_release(self.root, self.dist, '0.4.0', revision, True)

    def test_duplicate_active_version_source_is_rejected(self):
        with (self.root / 'pyproject.toml').open('a') as stream:
            stream.write('\n[project.extra]\n')
        config = self.root / 'pyproject.toml'
        config.write_text(config.read_text().replace('name = "videorebuild"',
                                                     'name = "videorebuild"\nversion = "0.4.0"'))
        with self.assertRaisesRegex(ValueError, 'derive version only'):
            release.check_release(self.root, self.dist, '0.4.0')
