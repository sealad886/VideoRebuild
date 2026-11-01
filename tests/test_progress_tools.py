"""Progress remains separate from tool results, errors, and deterministic builds."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from fragmented_helpers import generate, repacketize, write_case_inputs


class ToolProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixture.cleanup)
        raw, prefix = generate(Path(cls.fixture.name)/'generated.mp4', 'avc')
        cls.initialization = prefix.initialization.data
        cls.movie = repacketize(raw, prefix)
        cls.repo = Path(__file__).resolve().parents[1]

    def invoke(self, script, *args):
        environment = os.environ.copy()
        environment['PYTHONPATH'] = str(self.repo/'src')
        return subprocess.run([sys.executable, str(script), *map(str, args)],
                              capture_output=True, text=True, timeout=60, env=environment)

    def archive(self, root, *, corrupt=False):
        archive = root/'diagnostic.zip'
        manifest = {'samples': [{'path': 'header.mp4', 'size_bytes': len(self.initialization),
                                'sha256': 'incorrect' if corrupt else hashlib.sha256(self.initialization).hexdigest()}]}
        with zipfile.ZipFile(archive, 'w') as stream:
            stream.writestr('manifest.json', json.dumps(manifest))
            stream.writestr('samples/header.mp4', self.initialization)
            stream.writestr('photorec/report.xml', '<dfxml/>')
        return archive

    def assert_progress(self, completed):
        self.assertIn('Progress:', completed.stderr)
        self.assertNotIn('Progress:', completed.stdout)
        self.assertNotIn('\r', completed.stderr)
        self.assertNotIn('\x1b', completed.stderr)

    def test_build_progress_preserves_archive_bytes_and_stdout_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(self.repo/'src', root/'src', ignore=shutil.ignore_patterns('__pycache__'))
            (root/'tools').mkdir()
            script = root/'tools/build_zipapp.py'
            shutil.copy2(self.repo/'tools/build_zipapp.py', script)
            enabled = self.invoke(script)
            self.assertEqual(enabled.returncode, 0, enabled.stderr)
            self.assert_progress(enabled)
            target = root/'videorebuild.pyz'
            before = target.read_bytes()
            disabled = self.invoke(script, '--no-progress')
            self.assertEqual(disabled.returncode, 0, disabled.stderr)
            self.assertEqual(disabled.stderr, '')
            self.assertEqual(enabled.stdout, disabled.stdout)
            self.assertEqual(enabled.stdout.strip(), str(target.resolve()))
            self.assertEqual(target.read_bytes(), before)
            self.assertTrue(before.startswith(b'#!/usr/bin/env python3\n'))
            with zipfile.ZipFile(target) as archive:
                self.assertIn('videorebuild/cli.py', archive.namelist())

    def test_diagnostic_progress_does_not_change_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self.archive(root)
            script = self.repo/'tools/inspect_diagnostic.py'
            reports = [root/'enabled.json', root/'disabled.json']
            enabled = self.invoke(script, archive, '--output', reports[0])
            disabled = self.invoke(script, archive, '--output', reports[1], '--no-progress')
            self.assertEqual(enabled.returncode, 0, enabled.stderr)
            self.assertEqual(disabled.returncode, 0, disabled.stderr)
            self.assert_progress(enabled)
            self.assertEqual(disabled.stderr, '')
            self.assertEqual(json.loads(reports[0].read_text()), json.loads(reports[1].read_text()))
            self.assertIn('Verified 1 samples.', enabled.stdout)
            self.assertIn('Verified 1 samples.', disabled.stdout)

    def test_failed_diagnostic_phase_keeps_error_when_progress_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self.archive(root, corrupt=True)
            for options in ((), ('--no-progress',)):
                with self.subTest(options=options):
                    report = root/'report.json'
                    completed = self.invoke(self.repo/'tools/inspect_diagnostic.py', archive,
                                            '--output', report, *options)
                    self.assertEqual(completed.returncode, 2)
                    self.assertEqual(completed.stdout, '')
                    self.assertIn('Hash/size mismatch', completed.stderr)
                    self.assertFalse(report.exists())
                    if options:
                        self.assertNotIn('Progress:', completed.stderr)
                    else:
                        self.assertIn(' | failed | ', completed.stderr)
                        self.assertNotIn(' | finished | ', completed.stderr)

    def test_prefix_audit_progress_keeps_validation_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recovery, image, _ = write_case_inputs(root, [self.movie], len(self.initialization))
            case = root/'case'
            recovered = self.invoke('-m', 'videorebuild', 'run',
                                    '--image', image, '--input', recovery, '--report',
                                    recovery/'recup_dir.1/report.xml', '--case', case)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            results = []
            for name, options in (('enabled', ()), ('disabled', ('--no-progress',))):
                report = root/(name+'.json')
                completed = self.invoke(self.repo/'tools/check_recovered_prefixes.py', case, image, report, *options)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn('Audit passed.', completed.stdout)
                if options:
                    self.assertEqual(completed.stderr, '')
                else:
                    self.assert_progress(completed)
                    self.assertIn('1/1 samples', completed.stderr)
                result = json.loads(report.read_text())
                self.assertTrue(result['ok'])
                self.assertEqual(len(result['results']), 1)
                results.append(result['results'])
            self.assertEqual(results[0], results[1])
