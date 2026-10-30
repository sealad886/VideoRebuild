"""End-to-end contracts for canonical mapped-file reconstruction."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from helpers import generate, payload
from videorebuild.cli import main


class WorkflowTests(unittest.TestCase):
    def invoke(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(list(args))

    def test_filesystem_extents_reconstruct_noncontiguous_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root/'original.mp4'
            generate(original, faststart=True)
            raw = original.read_bytes()
            cut = len(raw)//2
            image = root/'disk.img'
            image.write_bytes(raw[cut:] + b'GAP'*1024 + raw[:cut])
            offset = len(raw)-cut + 3072
            report = root/'filesystem.xml'
            report.write_text(f'<dfxml xmlns="http://www.forensicswiki.org/wiki/Category:Digital_Forensics_XML">'
                f'<fileobject><filename>camera.mp4</filename><filesize>{len(raw)}</filesize><byte_runs>'
                f'<byte_run file_offset="0" img_offset="{offset}" len="{cut}"/>'
                f'<byte_run file_offset="{cut}" img_offset="0" len="{len(raw)-cut}"/>'
                '</byte_runs></fileobject></dfxml>')
            case = root/'case'
            before = hashlib.sha256(image.read_bytes()).hexdigest()
            self.assertEqual(self.invoke('run','--case',str(case),'--image',str(image),'--extents',str(report)),0)
            outputs = list((case/'reconstructed').glob('*.mp4'))
            self.assertEqual(len(outputs),1)
            self.assertEqual(payload(outputs[0]),payload(original))
            self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(),before)
            rows = json.loads((case/'reports'/'results.json').read_text())['jobs']
            self.assertTrue(any(row['status']=='matched' for row in rows))
            self.assertEqual(self.invoke('recover','--case',str(case)),0)

    def test_malformed_trailing_xml_rolls_back_all_logical_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            image=root/'disk.img'; image.write_bytes(b'A'*4096)
            report=root/'bad.xml'
            report.write_text('<dfxml><fileobject><filename>x.mp4</filename><filesize>16</filesize>'
                '<byte_runs><byte_run file_offset="0" img_offset="0" len="16"/></byte_runs>'
                '</fileobject><broken></dfxml>')
            case=root/'case'
            self.assertEqual(self.invoke('scan','--case',str(case),'--image',str(image),'--extents',str(report)),2)
            db=sqlite3.connect(case/'case.sqlite3')
            try:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sources WHERE kind='logical'").fetchone()[0],0)
            finally:
                db.close()

    def test_map_requires_explicit_image_and_metadata_stays_outside_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'recovered';source.mkdir()
            (source/'x.mp4').write_bytes(b'irrelevant')
            mapfile=root/'rescue.map';mapfile.write_text('0 + 1\n0 10 +\n')
            case=root/'case'
            self.assertEqual(self.invoke('scan','--case',str(case),'--input',str(source),'--mapfile',str(mapfile)),2)
            self.assertEqual(mapfile.read_text(),'0 + 1\n0 10 +\n')

    def test_invalid_recovery_limits_fail_before_case_or_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            original=root/'original.mp4'
            generate(original,seconds=0.5)
            before=original.read_bytes()
            for flag,value in [('--max-segments','1025'),('--max-record-bytes','1023'),
                               ('--max-record-bytes',str(256*1024*1024+1))]:
                case=root/flag.removeprefix('--')/value
                with self.subTest(flag=flag,value=value):
                    self.assertEqual(self.invoke('run','--case',str(case),'--input',str(original),flag,value),2)
                    self.assertFalse(case.exists())
                    self.assertEqual(original.read_bytes(),before)
