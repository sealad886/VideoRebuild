"""A scoped recovery reuses its index and never enters intake."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from helpers import generate, payload
from videorebuild.cli import main, parser
from videorebuild.storage import Case, scan_logical


class ScopedResumeTests(unittest.TestCase):
    def test_scope_is_only_available_for_existing_index_recovery(self):
        with self.assertRaises(SystemExit):
            parser().parse_args(['run', '--case', '/unused', '--image', '/unused.img',
                                 '--allocation-map', '/unused.json'])

    def test_resume_reuses_index_and_preserves_encoded_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'allocated-control.mp4'
            generate(original, audio=False, seconds=0.25)
            image = root / 'image.bin'
            image.write_bytes(original.read_bytes())
            case_root = root / 'case'
            with Case(case_root) as case:
                sid = case.register_source(image, kind='image')
                scan_logical(case, sid)
                source_rows = [tuple(r) for r in case.db.execute('SELECT * FROM sources ORDER BY id')]
                atom_rows = [tuple(r) for r in case.db.execute('SELECT * FROM atoms ORDER BY id')]
            policy = root / 'allocation.json'
            policy.write_text(json.dumps({'schema': 1, 'kind': 'filesystem-unallocated',
                'images': [{'path': str(image), 'size': image.stat().st_size,
                    'sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                    'unallocated_ranges': [[0, image.stat().st_size]]}]}))
            # Disposable synthetic image marks all bytes eligible. This proves
            # resume behavior; actual allocation truth requires bitmap evidence.
            with patch('videorebuild.workflow.intake', side_effect=AssertionError('unexpected reindex')):
                self.assertEqual(main(['recover', '--case', str(case_root), '--allocation-map',
                                       str(policy), '--no-progress']), 0)
            with Case(case_root) as case:
                self.assertEqual([tuple(r) for r in case.db.execute('SELECT * FROM sources ORDER BY id')], source_rows)
                self.assertEqual([tuple(r) for r in case.db.execute('SELECT * FROM atoms ORDER BY id')], atom_rows)
                outputs = list((case_root / 'reconstructed').glob('*.mp4'))
                self.assertEqual(len(outputs), 1)
                self.assertEqual(payload(outputs[0]), payload(original))
