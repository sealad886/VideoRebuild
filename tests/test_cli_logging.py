"""Recovery failures remain in the case log after report refresh."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.cli import main
from videorebuild.storage import SafetyError


class CaseErrorLoggingTests(unittest.TestCase):
    def test_recovery_error_is_saved_before_log_handler_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory) / 'case'
            message = 'original evidence unavailable'
            with patch('videorebuild.workflow.reconstruct', side_effect=SafetyError(message)), \
                    redirect_stdout(io.StringIO()), \
                    self.assertLogs('videorebuild.cli', level='ERROR') as terminal:
                result = main(['recover', '--case', str(case), '--no-progress'])
            self.assertEqual(result, 2)
            self.assertIn(message, '\n'.join(terminal.output))
            self.assertIn(message, (case / 'run.log').read_text())
            self.assertTrue((case / 'reports' / 'summary.json').is_file())
