"""Enforce the runtime requirement for source and standalone imports."""
import json
import importlib.util
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
import zipfile


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(__file__).resolve().parents[1]/'src'
        self.archive = Path(self.temp.name)/'videorebuild.pyz'
        spec = importlib.util.spec_from_file_location(
            'build_zipapp', self.source.parent / 'tools/build_zipapp.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        builder.build_archive(self.archive)

    def run_isolated(self, script):
        return subprocess.run([sys.executable, '-I', '-c', script],
                              capture_output=True, text=True, timeout=15)

    def test_import_requires_python_313_for_source_and_archive(self):
        for location in (self.source, self.archive):
            for version in ((3, 11), (3, 12), (3, 13), (3, 14), (4, 0)):
                with self.subTest(location=location, version=version):
                    result = self.run_isolated(
                        f'import sys, json\nsys.path.insert(0, {str(location)!r})\n'
                        f'sys.version_info = {version!r}\n'
                        'import videorebuild\nprint(json.dumps(videorebuild.__version__))\n')
                    if version < (3, 13):
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('RuntimeError', result.stderr)
                        self.assertIn('Python 3.13', result.stderr)
                        self.assertEqual(result.stdout, '')
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIsInstance(json.loads(result.stdout), str)
                        self.assertTrue(json.loads(result.stdout))

    def test_version_command_works_for_source_and_archive(self):
        from videorebuild import __version__
        source = self.run_isolated(
            f'import sys, runpy\nsys.path.insert(0, {str(self.source)!r})\n'
            "sys.argv = ['videorebuild', '--version']\n"
            "runpy.run_module('videorebuild', run_name='__main__')\n")
        archive = subprocess.run([sys.executable, str(self.archive), '--version'],
                                 capture_output=True, text=True, timeout=15)
        for result in (source, archive):
            with self.subTest(command=result.args):
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(__version__, result.stdout)

    def test_standalone_rejects_unsafe_paths_and_platform_mismatch_before_extraction(self):
        with zipfile.ZipFile(self.archive) as archive:
            original = {name: archive.read(name) for name in archive.namelist()}
        for change, message in (({'path': 'videorebuild/_native.bad/../../escaped.so'}, 'module path'),
                                ({'platform': 'unsupported_architecture'}, 'platform')):
            with self.subTest(change=change):
                content = dict(original)
                metadata = json.loads(content['_videorebuild_native.json'])
                metadata.update(change)
                content['_videorebuild_native.json'] = json.dumps(metadata).encode()
                with zipfile.ZipFile(self.archive, 'w') as archive:
                    for name, data in content.items():
                        archive.writestr(name, data)
                temporary = Path(self.temp.name) / 'native-temp'
                temporary.mkdir(exist_ok=True)
                result = subprocess.run([sys.executable, '-I', str(self.archive), '--version'],
                                        env=dict(os.environ, TMPDIR=str(temporary)),
                                        capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(list(temporary.iterdir()), [])

    def test_standalone_removes_private_native_after_normal_exit(self):
        temporary = Path(self.temp.name) / 'native-temp'
        temporary.mkdir()
        result = subprocess.run([sys.executable, '-I', str(self.archive), '--version'],
                                env=dict(os.environ, TMPDIR=str(temporary)),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(temporary.iterdir()), [])

    def test_standalone_cancellation_removes_its_private_native_directory(self):
        with zipfile.ZipFile(self.archive) as archive:
            content = {name: archive.read(name) for name in archive.namelist()}
        content['videorebuild/cli.py'] = (
            b'def main():\n    import time\n    print("ready", flush=True)\n    time.sleep(30)\n')
        with zipfile.ZipFile(self.archive, 'w') as archive:
            for name, data in content.items():
                archive.writestr(name, data)
        temporary = Path(self.temp.name) / 'native-temp'
        temporary.mkdir()
        process = subprocess.Popen([sys.executable, '-I', str(self.archive)],
                                   env=dict(os.environ, TMPDIR=str(temporary)),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(ready, 'synthetic standalone did not reach its waiting CLI')
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            self.assertTrue(list(temporary.glob('videorebuild-native-*')))
            process.terminate()
            _, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143, error)
            self.assertEqual(list(temporary.iterdir()), [])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()
