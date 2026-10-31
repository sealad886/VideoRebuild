"""Finder metadata is never an indexing, recovery or file-selection source."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.cli import parser
from videorebuild.paths import is_ignored_path, is_ignored_source
from videorebuild.storage import Case, ReaderPool, SafetyError, scan_logical
from videorebuild.tui_browser import Browser
from videorebuild.workflow import intake


class IgnoredMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.metadata = self.inputs / '.DS_Store'
        self.metadata.write_bytes(b'Finder metadata')

    def arguments(self, *extra):
        return parser().parse_args(['scan', '--case', str(self.root / 'case'), *extra])

    def test_only_metadata_basename_is_excluded_across_path_styles(self):
        for name in ('.DS_Store', 'folder/.ds_store', r'C:\folder\.DS_STORE'):
            with self.subTest(name=name):
                self.assertTrue(is_ignored_path(name))
        for name in ('camera.mp4', '.DS_Store.backup', 'camera.DS_Store', 'folder/.DS_Store/clip.mp4'):
            with self.subTest(name=name):
                self.assertFalse(is_ignored_path(name))

    def test_physical_and_logical_source_names_use_same_exclusion(self):
        self.assertTrue(is_ignored_source({'path': str(self.metadata), 'kind': 'file'}))
        for binding in ({'name': r'folder\.DS_Store'}, json.dumps({'name': '.DS_Store'})):
            self.assertTrue(is_ignored_source({'path': 'logical:example', 'kind': 'logical', 'binding': binding}))
        self.assertFalse(is_ignored_source({'path': 'logical:example', 'kind': 'logical',
                                            'binding': {'name': '.DS_Store.backup'}}))

    def test_unnamed_logical_extent_source_remains_eligible(self):
        self.assertFalse(is_ignored_source({'size': 16, 'extents': []}))
        self.assertTrue(is_ignored_source({'kind': 'logical', 'binding': {'name': '.DS_Store'}}))

    def test_all_files_scan_does_not_register_or_read_finder_metadata(self):
        nested = self.inputs / 'nested'
        nested.mkdir()
        (nested / '.ds_store').write_bytes(b'Other Finder metadata')
        (self.inputs / '.DS_Store.backup').write_bytes(b'Ordinary source')
        args = self.arguments('--input', str(self.inputs), '--all-files')
        open_path = Path.open
        def guarded(path, *args, **kwargs):
            if is_ignored_path(path):
                raise AssertionError('Finder metadata was opened')
            return open_path(path, *args, **kwargs)
        with Case(args.case) as case, patch.object(Path, 'open', guarded):
            intake(case, args)
            paths = [row['path'] for row in case.db.execute('SELECT path FROM sources')]
        self.assertEqual(paths, [str(self.inputs / '.DS_Store.backup')])
        self.assertEqual(self.metadata.read_bytes(), b'Finder metadata')

    def test_explicit_metadata_input_does_not_fall_back_to_an_image(self):
        args = self.arguments('--input', str(self.metadata), '--all-files')
        with Case(args.case) as case, patch.object(case, 'register_source') as register, \
                patch('videorebuild.workflow.scan_logical') as scan:
            with self.assertRaises(SafetyError):
                intake(case, args)
        register.assert_not_called()
        scan.assert_not_called()

    def test_metadata_cannot_be_used_as_image_or_intake_report(self):
        args = self.arguments('--image', str(self.metadata))
        with Case(args.case) as case, patch.object(case, 'register_source') as register:
            with self.assertRaises(SafetyError):
                intake(case, args)
        register.assert_not_called()
        image = self.root / 'disk.img'
        image.write_bytes(b'Image fixture')
        for option in ('--report', '--extents', '--mapfile'):
            with self.subTest(option=option):
                args = self.arguments('--image', str(image), option, str(self.metadata))
                with Case(args.case) as case, patch('videorebuild.workflow.file_sha256') as digest, \
                        patch('videorebuild.workflow.regular') as regular:
                    with self.assertRaises(SafetyError):
                        intake(case, args)
                digest.assert_not_called()
                regular.assert_not_called()

    def test_filesystem_mapping_does_not_import_metadata_stream(self):
        image = self.root / 'disk.img'
        image.write_bytes(b'A' * 64)
        xml = self.root / 'filesystem.xml'
        xml.write_text('<dfxml>' + ''.join(
            '<fileobject><filename>' + name + '</filename><filesize>16</filesize>'
            '<byte_runs><byte_run file_offset="0" img_offset="' + str(offset) + '" len="16"/>'
            '</byte_runs></fileobject>'
            for name, offset in ((r'folder\.DS_Store', 0), ('camera.mp4', 32))) + '</dfxml>')
        args = self.arguments('--image', str(image), '--extents', str(xml))
        with Case(args.case) as case:
            intake(case, args)
            names = [json.loads(row['binding'])['name'] for row in
                     case.db.execute("SELECT binding FROM sources WHERE kind='logical'")]
        self.assertEqual(names, ['camera.mp4'])
        self.assertEqual(image.read_bytes(), b'A' * 64)

    def test_changed_historical_metadata_never_reaches_recovery_reader(self):
        with Case(self.root / 'case') as case:
            state = self.metadata.stat()
            case.db.execute('''INSERT INTO sources(path,kind,size,mtime_ns,dev,ino,complete,sha256,hash_scope)
                               VALUES(?,?,?,?,?,?,1,?,'full')''',
                            (str(self.metadata), 'file', state.st_size, state.st_mtime_ns,
                             state.st_dev, state.st_ino, hashlib.sha256(b'Finder metadata').hexdigest()))
            case.db.commit()
            sid = case.db.execute('SELECT id FROM sources').fetchone()[0]
            self.metadata.write_bytes(b'Finder changed this metadata after reboot')
            with patch.object(Path, 'open', side_effect=AssertionError('Metadata opened')):
                self.assertEqual(case.sources(), {})
                self.assertEqual(case.physical_sources(), {})
                scan_logical(case, sid)
                with ReaderPool(case.sources(), physical_sources=case.physical_sources()) as pool:
                    with self.assertRaises((SafetyError, ValueError)):
                        pool.handle(sid)
            self.assertEqual(case.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 1)

    def test_browser_hides_metadata_and_rejects_direct_jump_without_changes(self):
        keep = self.inputs / '.DS_Store.backup'
        keep.write_bytes(b'Ordinary file')
        browser = Browser(str(self.inputs))
        names = [entry.name for entry in browser.entries]
        self.assertNotIn('.DS_Store', names)
        self.assertIn('.DS_Store.backup', names)
        before = (browser.current, browser.selected)
        browser.jump(str(self.metadata))
        self.assertEqual((browser.current, browser.selected), before)
        self.assertTrue(browser.error)
        self.assertEqual(self.metadata.read_bytes(), b'Finder metadata')


if __name__ == '__main__':
    unittest.main()
