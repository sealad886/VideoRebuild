"""Progress must describe real work without altering command data or outcomes."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from videorebuild.cli import main
from videorebuild.progress import phase, reporting


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class ProgressTests(unittest.TestCase):
    def test_updates_throttled_and_finishing_does_not_fill_remaining_work(self):
        output, clock = io.StringIO(), Clock()
        with reporting(stream=output, interval=5, clock=clock):
            with phase('Scanning available ranges', total=100, unit='bytes') as task:
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                for count in range(1, 5):
                    clock.now = count
                    task.update(count)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                clock.now = 5
                task.update(50)
                self.assertEqual(len(output.getvalue().splitlines()), 2)
                clock.now = 6
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn('50/100 bytes', lines[-1])
        self.assertNotIn('100.0%', lines[-1])
        self.assertIn('ETA', lines[1])
        self.assertNotIn('\r', output.getvalue())
        self.assertNotIn('\x1b', output.getvalue())

    def test_unknown_total_and_opaque_wait_have_no_percentage_or_eta(self):
        output, clock = io.StringIO(), Clock()
        with reporting(stream=output, clock=clock):
            with phase('Finding files', unit='files') as task:
                clock.now = 5
                task.advance(3, skipped=2)
            with phase('Waiting for decoder') as task:
                clock.now = 10
                task.heartbeat()
        text = output.getvalue()
        self.assertIn('3 files', text)
        self.assertNotIn('%', text)
        self.assertNotIn('ETA', text)

    def test_failed_and_interrupted_phases_do_not_finish_successfully(self):
        for error, status in ((ValueError('bad input'), 'failed'), (KeyboardInterrupt(), 'interrupted')):
            with self.subTest(status=status):
                output = io.StringIO()
                with reporting(stream=output):
                    with self.assertRaises(type(error)):
                        with phase('Importing mappings', total=2) as task:
                            task.update(1)
                            raise error
                terminal = output.getvalue().splitlines()[-1]
                self.assertIn(status, terminal)
                self.assertIn('1/2', terminal)
                self.assertNotIn('finished', terminal)

    def test_nonexception_timeout_is_reported_as_failed(self):
        output = io.StringIO()
        with reporting(stream=output):
            with phase('Decoder', total=1, unit='jobs') as task:
                task.outcome('failed', detail='timeout')
        terminal = output.getvalue().splitlines()[-1]
        self.assertIn('failed', terminal)
        self.assertIn('timeout', terminal)
        self.assertNotIn('finished', terminal)

    def test_suppression_and_session_scope(self):
        output = io.StringIO()
        with reporting(enabled=False, stream=output):
            with phase('Hidden', total=1) as task:
                task.advance()
        with contextlib.redirect_stderr(output):
            with phase('No active command') as task:
                task.advance()
        self.assertEqual(output.getvalue(), '')

    def test_closed_feedback_pipe_does_not_fail_work(self):
        output = io.StringIO()
        output.close()
        with reporting(stream=output):
            with phase('Work', total=1) as task:
                task.advance()

    def test_cli_progress_stays_on_stderr_and_in_case_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, case = root/'source.bin', root/'case'
            original = b'generated non-media source'
            source.write_bytes(original)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main(['scan', '--input', str(source), '--all-files', '--case', str(case)])
            self.assertEqual(result, 0, stderr.getvalue())
            self.assertIn('Progress:', stderr.getvalue())
            self.assertNotIn('Progress:', stdout.getvalue())
            summary, _ = json.JSONDecoder().raw_decode(stdout.getvalue())
            logged = (case/'run.log').read_text()
            self.assertIn('Progress:', logged)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main(['--no-progress', 'report', '--case', str(case)])
            self.assertEqual(result, 0, stderr.getvalue())
            self.assertNotIn('Progress:', stderr.getvalue())
            quiet, _ = json.JSONDecoder().raw_decode(stdout.getvalue())
            summary.pop('generated_unix')
            quiet.pop('generated_unix')
            self.assertEqual(summary, quiet)
            self.assertEqual((case/'run.log').read_text().count('Progress:'), logged.count('Progress:'))
            self.assertEqual(source.read_bytes(), original)
