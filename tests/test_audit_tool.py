"""Generated-media checks for the read-only existing-prefix audit tool."""
import contextlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from fragmented_helpers import generate, repacketize, write_case_inputs
from videorebuild.cli import main


class RecoveredPrefixAuditTests(unittest.TestCase):
    def test_missing_publication_is_reported_and_other_samples_are_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, prefix = generate(root/'generated.mp4', 'avc')
            movie = repacketize(data, prefix)
            recovery, image, locations = write_case_inputs(root, [movie, movie], len(prefix.initialization.data))
            case = root/'case'
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = main(['run', '--image', str(image), '--input', str(recovery),
                               '--report', str(recovery/'recup_dir.1/report.xml'), '--case', str(case)])
            self.assertEqual(status, 0)
            missing = next((case/'reconstructed').glob(f'fragment-*-{locations[0][0]}-*.mp4'))
            missing.unlink()
            before = {path: path.read_bytes() for path in case.rglob('*') if path.is_file()}
            source_before = image.read_bytes()
            report = root/'missing.json'
            tool = Path(__file__).resolve().parents[1]/'tools/check_recovered_prefixes.py'
            completed = subprocess.run([sys.executable, str(tool), str(case), str(image), str(report)],
                                       capture_output=True, text=True, timeout=45)
            self.assertEqual(completed.returncode, 1, completed.stdout+completed.stderr)
            result = json.loads(report.read_text())
            self.assertFalse(result['ok'])
            rows = {row['image_offset']: row for row in result['results']}
            self.assertEqual(set(rows), {offset for offset, _ in locations})
            self.assertFalse(rows[locations[0][0]]['ok'])
            self.assertTrue(rows[locations[1][0]]['ok'])
            self.assertFalse(missing.exists())
            self.assertEqual(image.read_bytes(), source_before)
            for path, original in before.items():
                self.assertEqual(path.read_bytes(), original, str(path))

    def test_audit_preserves_inputs_and_reports_changed_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, prefix = generate(root/'generated.mp4', 'avc')
            movie = repacketize(data, prefix)
            recovery, image, _ = write_case_inputs(root, [movie], len(prefix.initialization.data))
            case = root/'case'
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = main(['run', '--image', str(image), '--input', str(recovery),
                               '--report', str(recovery/'recup_dir.1/report.xml'), '--case', str(case)])
            self.assertEqual(status, 0)
            publication = next((case/'reconstructed').glob('*.mp4'))
            database = case/'case.sqlite3'
            before = {path: path.read_bytes() for path in (image, publication, database)}
            tool = Path(__file__).resolve().parents[1]/'tools/check_recovered_prefixes.py'
            report = root/'passed.json'
            command = [sys.executable, str(tool), str(case), str(image)]
            completed = subprocess.run(command+[str(report)], capture_output=True, text=True, timeout=45,
                                       umask=0)
            self.assertEqual(completed.returncode, 0, completed.stdout+completed.stderr)
            self.assertEqual(stat.S_IMODE(report.stat().st_mode) & 0o077, 0,
                             'A report with private source paths must not grant group/other access')
            result = json.loads(report.read_text())
            self.assertTrue(result['ok'])
            self.assertEqual(len(result['results']), 1)
            report_before = report.read_bytes()
            repeated = subprocess.run(command+[str(report)], capture_output=True, text=True, timeout=45)
            self.assertEqual(repeated.returncode, 2, repeated.stdout+repeated.stderr)
            self.assertEqual(report.read_bytes(), report_before)
            for path, original in before.items():
                self.assertEqual(path.read_bytes(), original, str(path))

            # A one-byte publication change must fail even if the case still
            # labels the original attempt as a successfully decoded prefix.
            changed = bytearray(before[publication]); changed[-1] ^= 1
            publication.write_bytes(changed)
            report = root/'failed.json'
            completed = subprocess.run(command+[str(report)], capture_output=True, text=True, timeout=45)
            self.assertEqual(completed.returncode, 1, completed.stdout+completed.stderr)
            result = json.loads(report.read_text())
            self.assertFalse(result['ok'])
            self.assertFalse(result['results'][0]['ok'])
            self.assertEqual(publication.read_bytes(), changed)
            self.assertEqual(image.read_bytes(), before[image])
            self.assertEqual(database.read_bytes(), before[database])
