"""Release artifacts must preserve source identity and the one canonical version."""
import hashlib
import importlib.util
import io
import json
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
        self.wheel_name = 'videorebuild-0.4.0-cp313-abi3-macosx_11_0_arm64.whl'
        self.zipapp_name = self.wheel_name.removesuffix('.whl') + '.pyz'
        self.native_path = 'videorebuild/_native.abi3.so'
        self.native = b'synthetic native artifact identity'
        (self.root / 'native/src').mkdir(parents=True)
        (self.root / 'tools').mkdir()
        (self.root / 'native/Cargo.toml').write_bytes(b'[package]\nname="videorebuild-core"\n')
        (self.root / 'native/Cargo.lock').write_bytes(b'version = 4\n')
        (self.root / 'native/src/lib.rs').write_bytes(b'// synthetic build input\n')
        (self.root / 'setup.cfg').write_bytes(b'[bdist_wheel]\npy_limited_api = cp313\n')
        (self.root / 'tools/zipapp_main.py').write_bytes(b'# synthetic bootstrap\n')
        (self.root / 'LICENSE').write_bytes(b'synthetic project license\n')
        (self.root / 'native/THIRD_PARTY_NOTICES.md').write_bytes(b'synthetic dependency licenses\n')
        self.write_artifacts()

    def write_artifacts(self, wheel_metadata=None, sdist_metadata=None, package_source=None):
        code = self.source if package_source is None else package_source
        with zipfile.ZipFile(self.dist / self.wheel_name, 'w') as archive:
            archive.writestr('videorebuild/__init__.py', code)
            archive.writestr('videorebuild-0.4.0.dist-info/METADATA',
                             self.metadata if wheel_metadata is None else wheel_metadata)
            archive.writestr('videorebuild-0.4.0.dist-info/WHEEL',
                             b'Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp313-abi3-macosx_11_0_arm64\n')
            archive.writestr(self.native_path, self.native)
            for name in ('LICENSE', 'native/THIRD_PARTY_NOTICES.md'):
                archive.writestr('videorebuild-0.4.0.dist-info/licenses/' + name, (self.root / name).read_bytes())
        with zipfile.ZipFile(self.dist / self.zipapp_name, 'w') as archive:
            archive.writestr('videorebuild/__init__.py', self.source)
            archive.writestr(self.native_path, self.native)
            for name in ('LICENSE', 'native/THIRD_PARTY_NOTICES.md'):
                archive.writestr('_licenses/' + name, (self.root / name).read_bytes())
            archive.writestr('__main__.py', (self.root / 'tools/zipapp_main.py').read_bytes())
            archive.writestr('_videorebuild_native.json', json.dumps({
                'path': self.native_path, 'sha256': hashlib.sha256(self.native).hexdigest(),
                'platform': 'macosx_11_0_arm64', 'python': 'cp313', 'abi': 'abi3'}))
        with tarfile.open(self.dist / 'videorebuild-0.4.0.tar.gz', 'w:gz') as archive:
            content = {
                    'videorebuild-0.4.0/PKG-INFO': self.metadata if sdist_metadata is None else sdist_metadata,
                    'videorebuild-0.4.0/src/videorebuild/__init__.py': self.source}
            for name in ('native/Cargo.toml', 'native/Cargo.lock', 'native/src/lib.rs', 'setup.cfg',
                         'LICENSE', 'native/THIRD_PARTY_NOTICES.md'):
                content['videorebuild-0.4.0/' + name] = (self.root / name).read_bytes()
            for name, data in content.items():
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
        (self.dist / self.zipapp_name).unlink()
        with self.assertRaisesRegex(ValueError, 'matching native wheel/zipapp pairs'):
            release.check_release(self.root, self.dist, '0.4.0')
        self.write_artifacts()
        (self.dist / 'videorebuild-0.3.0.tar.gz').write_bytes(b'stale build')
        with self.assertRaisesRegex(ValueError, 'matching native wheel/zipapp pairs'):
            release.check_release(self.root, self.dist, '0.4.0')

    def test_duplicate_metadata_fields_and_malformed_archives_are_rejected(self):
        self.write_artifacts(wheel_metadata=self.metadata + b'Version: 0.4.0\n')
        with self.assertRaisesRegex(ValueError, 'name/version differs'):
            release.check_release(self.root, self.dist, '0.4.0')
        self.write_artifacts()
        (self.dist / self.zipapp_name).write_bytes(b'not a zipapp')
        with self.assertRaises(zipfile.BadZipFile):
            release.check_release(self.root, self.dist, '0.4.0')

    def test_standalone_native_tampering_and_stale_rust_sources_are_rejected(self):
        path = self.dist / self.zipapp_name
        content = release.zip_contents(path)
        content[self.native_path] = b'different native implementation'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in content.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(ValueError, 'native module differs'):
            release.check_release(self.root, self.dist, '0.4.0')
        self.write_artifacts()
        (self.root / 'native/src/lib.rs').write_bytes(b'// changed implementation\n')
        with self.assertRaisesRegex(ValueError, 'native build source differs'):
            release.check_release(self.root, self.dist, '0.4.0')

    def test_missing_native_extension_and_incorrect_wheel_tag_are_rejected(self):
        path = self.dist / self.wheel_name
        content = release.zip_contents(path)
        del content[self.native_path]
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in content.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(ValueError, 'native extension required'):
            release.check_release(self.root, self.dist, '0.4.0')
        self.write_artifacts()
        content = release.zip_contents(path)
        content['videorebuild-0.4.0.dist-info/WHEEL'] = b'Root-Is-Purelib: true\nTag: py3-none-any\n'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in content.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(ValueError, 'native wheel tag differs'):
            release.check_release(self.root, self.dist, '0.4.0')

    def test_dependency_license_omission_is_rejected(self):
        path = self.dist / self.wheel_name
        content = release.zip_contents(path)
        del content['videorebuild-0.4.0.dist-info/licenses/native/THIRD_PARTY_NOTICES.md']
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in content.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(ValueError, 'native license notices differ'):
            release.check_release(self.root, self.dist, '0.4.0')

    def test_compressed_manylinux_filename_tags_match_expanded_metadata(self):
        old_wheel, old_zipapp = self.dist / self.wheel_name, self.dist / self.zipapp_name
        wheel, zipapp = release.zip_contents(old_wheel), release.zip_contents(old_zipapp)
        platform = 'manylinux_2_17_x86_64.manylinux2014_x86_64'
        self.wheel_name = f'videorebuild-0.4.0-cp313-abi3-{platform}.whl'
        self.zipapp_name = self.wheel_name.removesuffix('.whl') + '.pyz'
        wheel['videorebuild-0.4.0.dist-info/WHEEL'] = (
            b'Root-Is-Purelib: false\nTag: cp313-abi3-manylinux_2_17_x86_64\n'
            b'Tag: cp313-abi3-manylinux2014_x86_64\n')
        metadata = json.loads(zipapp['_videorebuild_native.json'])
        metadata['platform'] = platform
        zipapp['_videorebuild_native.json'] = json.dumps(metadata).encode()
        old_wheel.unlink()
        old_zipapp.unlink()
        for name, content in ((self.wheel_name, wheel), (self.zipapp_name, zipapp)):
            with zipfile.ZipFile(self.dist / name, 'w') as archive:
                for entry, data in content.items():
                    archive.writestr(entry, data)
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
