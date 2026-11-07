"""Every case invocation records its actual runtime before workflow work begins."""
import contextlib
from datetime import datetime, timedelta
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from videorebuild import cli, workflow
from videorebuild.storage import SafetyError


def startup_records(path):
    records = []
    for line in path.read_text().splitlines():
        message = line.partition(' INFO ')[2]
        if message.startswith('{'):
            record = json.loads(message)
            if record.get('event') == 'startup':
                records.append(record)
    return records


class StartupLoggingTests(unittest.TestCase):
    def test_effective_arguments_and_runtime_are_logged_before_workflow(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, case = root/'source.bin', root/'case'
                source.write_bytes(b'generated non-media fixture')
                arguments = ['--no-progress', 'scan', '--all-files', '--input', str(source), '--case', str(case)]
                observed = []
                intake = workflow.intake

                def observe(case_object, args):
                    observed.extend(startup_records(case/'run.log'))
                    return intake(case_object, args)

                with patch.object(sys, 'argv', ['test-launcher', *(['doctor'] if explicit else arguments)]), \
                     patch.object(workflow, 'intake', side_effect=observe), \
                     patch.object(cli.logging, 'basicConfig'), \
                     patch.object(logging.getLogger(), 'level', logging.CRITICAL), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(arguments if explicit else None), 0)
                self.assertEqual(len(observed), 1)
                record = observed[0]
                self.assertEqual(record['argv'], arguments)
                self.assertEqual(record['tool'], 'VideoRebuild')
                self.assertEqual(record['version'], cli.__version__)
                self.assertEqual(record['pid'], os.getpid())
                self.assertEqual(record['command'], 'scan')
                self.assertEqual(record['python_executable'], sys.executable)
                self.assertEqual(record['python_version'], sys.version)
                self.assertEqual(record['cwd'], str(Path.cwd()))
                self.assertEqual(record['case'], str(case.resolve()))
                self.assertEqual(record['cli_origin'], cli.__file__)
                self.assertEqual(record['entrypoint'], 'test-launcher')
                self.assertEqual(datetime.fromisoformat(record['utc']).utcoffset(), timedelta())
                self.assertEqual(startup_records(case/'run.log'), observed)

    def test_each_invocation_appends_one_header_without_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, case = root/'source.bin', root/'case'
            source.write_bytes(b'generated non-media fixture')
            scan = ['--no-progress', 'scan', '--all-files', '--input', str(source), '--case', str(case)]
            report = ['--no-progress', 'report', '--case', str(case)]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(scan), 0)
                previous = (case/'run.log').read_text()
                self.assertEqual(cli.main(report), 0)
            self.assertTrue((case/'run.log').read_text().startswith(previous))
            self.assertEqual([record['argv'] for record in startup_records(case/'run.log')], [scan, report])
            self.assertNotIn('Progress:', (case/'run.log').read_text())

    def test_header_survives_failure_and_interruption_and_handler_closes(self):
        for error, expected in ((SafetyError('fixture failure'), 2), (KeyboardInterrupt(), 130)):
            with self.subTest(error=type(error)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, case = root/'source.bin', root/'case'
                source.write_bytes(b'generated non-media fixture')
                arguments = ['--no-progress', 'scan', '--all-files', '--input', str(source), '--case', str(case)]
                handlers, streams = [], []
                file_handler = logging.FileHandler

                def capture_handler(*args, **kwargs):
                    handler = file_handler(*args, **kwargs)
                    handlers.append(handler)
                    streams.append(handler.stream)
                    return handler

                before = logging.getLogger().handlers[:]
                with patch.object(cli.logging, 'basicConfig'), \
                     patch.object(cli.logging, 'FileHandler', side_effect=capture_handler), \
                     patch.object(workflow, 'intake', side_effect=error), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(arguments), expected)
                self.assertEqual(startup_records(case/'run.log')[0]['argv'], arguments)
                self.assertEqual(len(handlers), 1)
                self.assertTrue(streams[0].closed)
                self.assertEqual(logging.getLogger().handlers, before)
