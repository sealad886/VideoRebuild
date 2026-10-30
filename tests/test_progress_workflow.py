"""Progress describes actual available work without changing intake or reports."""
from contextlib import ExitStack
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.cli import parser
from videorebuild.progress import Phase, reporting
from videorebuild.reports import report
from videorebuild.storage import Case, scan_logical
from videorebuild.workflow import intake
from videorebuild.bmff import FormatError


class RecordingPhase(Phase):
    """Capture semantic phase events independently of terminal formatting."""
    events = None

    def _emit(self, status, now):
        self.events.append((self.label, status, self.completed, self.total, self.unit))
        self.last_emitted = now


class WorkflowProgressTests(unittest.TestCase):
    def observe(self, stack):
        events = []
        def factory(label, **options):
            task = RecordingPhase(label, **options)
            task.events = events
            return task
        stack.enter_context(reporting(stream=io.StringIO(), interval=0))
        for module in ('storage', 'ranges', 'workflow', 'reports'):
            stack.enter_context(patch('videorebuild.'+module+'.phase', side_effect=factory))
        return events

    def test_image_scan_counts_only_available_bytes(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            source = root/'image'; source.write_bytes(b'AAAAXXXXBBBB')
            rescue = root/'map'; rescue.write_text('0 + 1\n0 4 +\n4 4 -\n8 4 +\n')
            with Case(root/'case') as case:
                sid = case.register_source(source, 'image', mapfile=rescue)
                events = self.observe(stack)
                scan_logical(case, sid)
                finished = [event for event in events if event[1] == 'finished' and event[3] == 8]
                self.assertEqual(len(finished), 1)
                self.assertEqual(finished[0][2:5], (8, 8, 'bytes'))
                self.assertTrue(all(done <= total for _, _, done, total, _ in events if total is not None))
                self.assertEqual(case.sources()[sid]['hash_scope'], 'available-ranges')

    def test_recovered_input_only_never_registers_or_scans_an_image(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            recovered = root/'recovered'; recovered.mkdir()
            (recovered/'clip.mp4').write_bytes(b'synthetic recovered bytes')
            args = parser().parse_args(['scan', '--case', str(root/'case'), '--input', str(recovered)])
            events = self.observe(stack)
            with Case(args.case) as case:
                intake(case, args)
                self.assertEqual({row['kind'] for row in case.sources().values()}, {'file'})
            byte_work = [event for event in events if event[1] == 'finished' and event[4] == 'bytes']
            self.assertEqual([event[2] for event in byte_work], [len(b'synthetic recovered bytes')])
            self.assertTrue(any(event[1] == 'finished' and event[2:5] == (1, None, 'files') for event in events))

    def test_trailing_bad_xml_reports_failure_and_rolls_back_import(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            image = root/'image'; image.write_bytes(b'A'*16)
            xml = root/'bad.xml'; xml.write_text('<dfxml><fileobject><filename>x.mp4</filename>'
                '<filesize>16</filesize><byte_runs><byte_run file_offset="0" img_offset="0" len="16"/>'
                '</byte_runs></fileobject><broken></dfxml>')
            args = parser().parse_args(['scan', '--case', str(root/'case'), '--image', str(image), '--extents', str(xml)])
            events = self.observe(stack)
            with Case(args.case) as case:
                with self.assertRaises(FormatError):
                    intake(case, args)
                self.assertEqual(case.db.execute("SELECT COUNT(*) FROM sources WHERE kind='logical'").fetchone()[0], 0)
            failed = {label for label, state, *_ in events if state == 'failed'}
            self.assertTrue(failed)
            self.assertTrue(all(not (state == 'finished' and label in failed) for label, state, *_ in events))

    def test_progress_does_not_change_report_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'clip.mp4'; source.write_bytes(b'synthetic')
            with Case(root/'case') as case:
                sid = case.register_source(source)
                scan_logical(case, sid)
                case.result('synthetic', 'unresolved', {'reason': 'synthetic', 'outputs': []})
                with patch('videorebuild.reports.time.time', return_value=123):
                    report(case)
                    before = {path.name: path.read_bytes() for path in (case.root/'reports').iterdir()}
                    with reporting(stream=io.StringIO(), interval=0):
                        report(case)
                    after = {path.name: path.read_bytes() for path in (case.root/'reports').iterdir()}
                self.assertEqual(before, after)
