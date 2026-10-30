"""Enforce the runtime requirement for source and standalone imports."""
import json
from pathlib import Path
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
        with zipfile.ZipFile(self.archive, 'w') as archive:
            for module in (self.source/'videorebuild').rglob('*.py'):
                archive.write(module, str(module.relative_to(self.source)))
            archive.writestr('__main__.py',
                             'from videorebuild.cli import main\nraise SystemExit(main())\n')

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
