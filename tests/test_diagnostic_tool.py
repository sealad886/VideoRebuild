"""Synthetic diagnostic ZIP contracts: safe XML, private reports, exact hashes."""
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

from fragmented_helpers import generate


class DiagnosticToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        _, prefix = generate(Path(cls.fixture.name)/'generated.mp4', 'avc')
        cls.initialization = prefix.initialization.data
        cls.tool = Path(__file__).resolve().parents[1]/'tools/inspect_diagnostic.py'

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def archive(self, root, encoding, entity=False):
        xml = ('<dfxml><source><image_filename>camera Ω synthetic.img</image_filename>'
               '<image_size>8192</image_size><sectorsize>512</sectorsize>'
               '<block_size>4096</block_size></source></dfxml>')
        if entity:
            xml = '<!DOCTYPE dfxml [<!ENTITY name "EXPANDED">]>'+xml.replace('camera Ω synthetic.img', '&name;')
        raw = xml.encode(encoding)
        if encoding == 'utf-16-le':
            raw = b'\xff\xfe'+raw
        elif encoding == 'utf-16-be':
            raw = b'\xfe\xff'+raw
        path = root/'input.zip'
        manifest = {'samples': [{'path': 'header.mp4', 'size_bytes': len(self.initialization),
                                'sha256': hashlib.sha256(self.initialization).hexdigest()}]}
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('manifest.json', json.dumps(manifest))
            archive.writestr('samples/header.mp4', self.initialization)
            archive.writestr('photorec/report.xml', raw)
        return path

    def run_tool(self, archive, report):
        return subprocess.run([sys.executable, str(self.tool), str(archive), '--output', str(report)],
                              capture_output=True, text=True, timeout=30, umask=0)

    def test_valid_encodings_preserve_text_hash_and_private_exclusive_report(self):
        for encoding in ('utf-8', 'utf-16-le', 'utf-16-be'):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = self.archive(root, encoding)
                report = root/'report.json'
                completed = self.run_tool(archive, report)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(report.read_text())
                self.assertEqual(result['source'][0]['image_filename'], 'camera Ω synthetic.img')
                self.assertEqual(result['archive_sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
                self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
                before = report.read_bytes()
                repeated = self.run_tool(archive, report)
                self.assertEqual(repeated.returncode, 2, repeated.stderr)
                self.assertEqual(report.read_bytes(), before)

    def test_entity_documents_rejected_without_report_for_all_encodings(self):
        for encoding in ('utf-8', 'utf-16-le', 'utf-16-be'):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = self.archive(root, encoding, entity=True)
                report = root/'report.json'
                completed = self.run_tool(archive, report)
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertFalse(report.exists())
