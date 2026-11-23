"""The comparison command preserves unresolved inputs and supports remuxed copies."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from videorebuild.cli import main, parser
from videorebuild.storage import SafetyError

from helpers import generate


class ComparisonCommandTests(unittest.TestCase):
    def test_paths_visual_and_timeout_are_configurable_without_case(self):
        compare = Mock(return_value={'complete': True})
        module = types.ModuleType('videorebuild.comparison')
        module.compare_directories = compare
        with patch.dict('sys.modules', {'videorebuild.comparison': module}), \
                redirect_stdout(io.StringIO()) as output:
            status = main(['compare', '--recovered', '/input/a', '--archive', '/input/b',
                           '--output', '/output/review', '--visual', '--timeout', '12.5',
                           '--no-progress'])
        self.assertEqual(status, 0)
        compare.assert_called_once_with(Path('/input/a'), Path('/input/b'), Path('/output/review'),
                                        visual=True, timeout=12.5)
        self.assertIn('/output/review/missing.csv', output.getvalue())

    def test_comparison_errors_have_distinct_exit_status(self):
        module = types.ModuleType('videorebuild.comparison')
        module.compare_directories = Mock(return_value={'complete': False})
        command = ['compare', '--recovered', '/input/a', '--archive', '/input/b',
                   '--output', '/output/review', '--no-progress']
        with patch.dict('sys.modules', {'videorebuild.comparison': module}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(command), 1)
            module.compare_directories.side_effect = SafetyError('unsafe comparison output')
            with self.assertLogs('videorebuild.cli', level='ERROR'):
                self.assertEqual(main(command), 2)

    def test_timeout_must_be_finite_and_positive(self):
        command = ['compare', '--recovered', '/input/a', '--archive', '/input/b',
                   '--output', '/output/review']
        for value in ('0', '-1', 'nan', 'inf', '-inf'):
            with self.subTest(timeout=value), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                parser().parse_args([*command, f'--timeout={value}'])
            self.assertEqual(error.exception.code, 2)

    def test_interruption_returns_standard_exit_status(self):
        module = types.ModuleType('videorebuild.comparison')
        module.compare_directories = Mock(side_effect=KeyboardInterrupt)
        with patch.dict('sys.modules', {'videorebuild.comparison': module}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = main(['compare', '--recovered', '/input/a', '--archive', '/input/b',
                           '--output', '/output/review', '--no-progress'])
        self.assertEqual(status, 130)

    def test_no_progress_works_before_or_after_command(self):
        command = ['compare', '--recovered', '/input/a', '--archive', '/input/b',
                   '--output', '/output/review']
        self.assertTrue(parser().parse_args(['--no-progress', *command]).no_progress)
        self.assertTrue(parser().parse_args([*command, '--no-progress']).no_progress)

    def test_overlapping_inputs_are_rejected_before_output_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            parent, output = root/'Input', root/'reports'
            child = parent/'nested'
            child.mkdir(parents=True)
            pairs = [(parent, parent), (parent, child), (child, parent)]
            alias = root/'input'
            if alias.is_dir():
                pairs.extend([(parent, alias), (child, alias), (alias, child)])
            for recovered, archive in pairs:
                with self.subTest(recovered=recovered, archive=archive), \
                        self.assertLogs('videorebuild.cli', level='ERROR'):
                    self.assertEqual(main(['compare', '--recovered', str(recovered),
                                          '--archive', str(archive), '--output', str(output),
                                          '--no-progress']), 2)
                self.assertFalse(output.exists())


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required for comparison integration')
class ComparisonCommandIntegrationTests(unittest.TestCase):
    def test_visual_reencoded_suggestion_remains_a_missing_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            recovered, archive, output = (root / name for name in ('A', 'B', 'reports'))
            recovered.mkdir()
            archive.mkdir()
            original = recovered / 'recovered.mp4'
            generate(original, audio=False, seconds=6.0)
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(original),
                            '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '30',
                            '-threads', '1', str(archive / 'reencoded.mp4')],
                           check=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = main(['compare', '--recovered', str(recovered), '--archive', str(archive),
                               '--output', str(output), '--visual', '--no-progress'])
            self.assertEqual(status, 0)
            report = json.loads((output / 'comparison.json').read_text())
            self.assertEqual(report['results'][0]['status'], 'visual_candidate')
            self.assertTrue(report['results'][0]['visual_candidates'])
            self.assertIn(str(original), (output / 'missing.csv').read_text())

    def test_remuxed_copy_is_represented_and_unresolved_files_stay_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            recovered, archive, output = (root / name for name in ('A', 'B', 'reports'))
            recovered.mkdir()
            (archive / 'nested').mkdir(parents=True)
            original = recovered / 'recovered.mp4'
            generate(original, audio=False)
            transferred = archive / 'nested' / 'renamed.mov'
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(original),
                            '-map', '0', '-c', 'copy', '-metadata', 'title=transferred copy',
                            str(transferred)], check=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            missing = recovered / 'different.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi',
                            '-i', 'color=blue:size=160x96:rate=12:duration=2',
                            '-c:v', 'libx264', '-threads', '1', str(missing)],
                           check=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            damaged = recovered / 'damaged.mp4'
            damaged.write_bytes(b'not a video')
            (recovered / '.DS_Store').write_bytes(b'finder metadata')
            paths = (original, transferred, missing, damaged, recovered / '.DS_Store')
            before = {path: hashlib.sha256(path.read_bytes()).digest() for path in paths}
            self.assertNotEqual(before[original], before[transferred])

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = main(['compare', '--recovered', str(recovered), '--archive', str(archive),
                               '--output', str(output), '--no-progress'])

            self.assertEqual(status, 1)
            missing_report = (output / 'missing.csv').read_text()
            self.assertNotIn(str(original), missing_report)
            self.assertIn(str(missing), missing_report)
            self.assertIn(str(damaged), missing_report)
            self.assertNotIn('.DS_Store', missing_report)
            self.assertIn(str(original), (output / 'comparison.csv').read_text())
            self.assertTrue((output / 'comparison.json').is_file())
            self.assertTrue((output / 'summary.md').is_file())
            for path, digest in before.items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest)
