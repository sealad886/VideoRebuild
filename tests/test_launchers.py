"""Run the macOS launcher against isolated Homebrew installation trees."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import zipfile


class HomebrewLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Path(__file__).resolve().parents[1]
        self.prefixes = [self.root/'native', self.root/'second']
        self.fallback = self.root/'path tools'
        self.fallback.mkdir()
        self.tools(self.fallback, ('9.0.2', '9.0.2'))
        python = self.prefixes[0]/'opt/python@3.13/bin/python3.13'
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)

        # Relocate installation roots, not the selection algorithm. No real
        # Homebrew installation or host-global filesystem path is touched.
        script = (self.repo/'run-fragmented-macos.command').read_text()
        for location, fixture in zip(('/opt/homebrew', '/usr/local'), self.prefixes):
            script = script.replace(location, str(fixture))
        self.launcher = self.root/'run-fragmented-macos.command'
        self.launcher.write_text(script)
        with zipfile.ZipFile(self.root/'videorebuild.pyz', 'w') as archive:
            source = self.repo/'src'
            for module in source.rglob('*.py'):
                archive.write(module, str(module.relative_to(source)))
            # Probe the canonical doctor for candidate selection and observe
            # the selected runtime at the final application boundary.
            archive.writestr('__main__.py',
                'import json, sys\n'
                'from videorebuild.validate import doctor\n'
                'print(json.dumps({"tools": doctor(), "argv": sys.argv[1:]}))\n')

    def executable(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\n'+body)
        path.chmod(0o755)

    def tools(self, directory, versions):
        for name, version in zip(('ffmpeg', 'ffprobe'), versions):
            executable = directory/name
            if version is None:
                if executable.exists():
                    executable.unlink()
                continue
            self.executable(executable, f"printf '%s\\n' {shlex.quote(name+' version '+version)}\n")

    def homebrew(self, prefix, versions):
        directory = prefix/'opt/ffmpeg tools/bin'
        self.tools(directory, versions)
        self.executable(prefix/'bin/brew',
            'if [ "$#" -ne 2 ] || [ "$1" != --prefix ] || [ "$2" != ffmpeg ]; then exit 99; fi\n'
            f"printf '%s\\n' {shlex.quote(str(directory.parent))}\n")
        return directory

    def selected_tools(self):
        environment = os.environ.copy()
        environment['PATH'] = os.pathsep.join((str(self.fallback), '/usr/bin', '/bin'))
        completed = subprocess.run(['/bin/bash', str(self.launcher), '--help', 'argument with spaces'],
                                   env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stdout+completed.stderr)
        observed = json.loads(completed.stdout)
        self.assertEqual(observed['argv'], ['recover-fragmented', '--help', 'argument with spaces'])
        return {name: Path(tool['path']) for name, tool in observed['tools'].items()}

    def assert_selected(self, directory):
        self.assertEqual(self.selected_tools(),
                         {name: directory/name for name in ('ffmpeg', 'ffprobe')})

    def test_first_supported_pair_is_retained(self):
        first = self.homebrew(self.prefixes[0], ('9.0.2', '9.0.2'))
        for second in (('8.0.1', '8.0.1'), ('7.1.5', '7.1.5')):
            with self.subTest(second=second):
                self.homebrew(self.prefixes[1], second)
                self.assert_selected(first)

    def test_unsupported_first_pair_does_not_mask_supported_later_pair(self):
        second = self.homebrew(self.prefixes[1], ('8.0.1', '8.0.1'))
        for first in (('7.1.5', '7.1.5'), ('9.0.2', '7.1.5')):
            with self.subTest(first=first):
                self.homebrew(self.prefixes[0], first)
                self.assert_selected(second)

    def test_incomplete_pairs_do_not_borrow_tools_from_path(self):
        self.homebrew(self.prefixes[0], ('9.0.2', None))
        self.homebrew(self.prefixes[1], (None, '9.0.2'))
        self.assert_selected(self.fallback)

    def test_original_path_is_used_when_homebrew_pairs_are_unsupported(self):
        self.homebrew(self.prefixes[0], ('7.1.5', '7.1.5'))
        self.homebrew(self.prefixes[1], ('6.1.1', '6.1.1'))
        self.assert_selected(self.fallback)

    def test_original_path_is_used_without_homebrew(self):
        self.assert_selected(self.fallback)
