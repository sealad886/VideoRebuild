"""Behavioral contracts for logical mappings and acquisition evidence."""
from dataclasses import replace
import hashlib
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from videorebuild.bmff import FormatError
from videorebuild.ranges import (DFXMLRecord, Extent, RescueMap, RescueSpan,
                                UnavailableRange, iter_dfxml, resolve_extents,
                                validate_extents)


class ExtentTests(unittest.TestCase):
    def test_resolves_cross_source_boundaries_and_clips_both_offsets(self):
        extents = validate_extents(12, [Extent(6, 2, 40, 6), Extent(0, 1, 10, 6)],
                                   {1: 20, 2: 50})
        self.assertEqual(list(resolve_extents(extents, 3, 6)),
                         [Extent(3, 1, 13, 3), Extent(6, 2, 40, 3)])
        self.assertEqual(list(resolve_extents(extents, 12, 0)), [])

    def test_holes_are_preserved_and_reads_cannot_cross_them(self):
        extents = validate_extents(12, [Extent(0, 1, 0, 3), Extent(7, 1, 4, 5)], {1: 9})
        self.assertEqual(list(resolve_extents(extents, 8, 4)), [Extent(8, 1, 5, 4)])
        for offset, size in ((2, 6), (4, 1), (12, 1)):
            with self.subTest(offset=offset, size=size), self.assertRaises(UnavailableRange):
                list(resolve_extents(extents, offset, size))

    def test_invalid_ranges_and_unknown_sources_are_rejected(self):
        valid = Extent(0, 1, 0, 4)
        invalid = [replace(valid, logical_offset=-1), replace(valid, source_id=2),
                   replace(valid, source_offset=8), replace(valid, length=0),
                   replace(valid, length=11), replace(valid, logical_offset=True)]
        for extent in invalid:
            with self.subTest(extent=extent), self.assertRaises(FormatError):
                validate_extents(10, [extent], {1: 10})
        with self.assertRaises(FormatError):
            validate_extents(10, [valid, Extent(3, 1, 4, 4)], {1: 10})
        with self.assertRaises(FormatError):
            validate_extents(8, [valid, Extent(4, 1, 2, 4)], {1: 10})
        for offset, length in ((-1, 1), (1, -1), (2**63-1, 1)):
            with self.subTest(offset=offset), self.assertRaises(FormatError):
                list(resolve_extents((valid,), offset, length))

    def test_indexed_resolution_does_not_walk_all_previous_extents(self):
        class CountedSequence:
            def __init__(self, values):
                self.values, self.reads = values, 0
            def __len__(self):
                return len(self.values)
            def __getitem__(self, index):
                self.reads += 1
                return self.values[index]
        extents = CountedSequence(tuple(Extent(i*2, 1, i*2, 2) for i in range(100_000)))
        self.assertEqual(list(resolve_extents(extents, 199_995, 4)),
                         [Extent(199_995, 1, 199_995, 1), Extent(199_996, 1, 199_996, 2),
                          Extent(199_998, 1, 199_998, 1)])
        self.assertLess(extents.reads, 40)


class TemporaryInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, text, name='input.txt'):
        path = self.root/name
        path.write_bytes(text if isinstance(text, (bytes, bytearray)) else text.encode())
        return path


class RescueMapTests(TemporaryInputTests):
    def test_acquisition_statuses_remain_distinct_and_uncovered_is_unknown(self):
        path = self.write('0 + 1\n' + ''.join(f'{i*4} 4 {state}\n'
                                           for i, state in enumerate('+?*/-')))
        rescue = RescueMap.from_file(path, image_offset=8)
        self.assertEqual(list(rescue.spans(6, 24)),
                         [RescueSpan(6, 2, 'unknown'), RescueSpan(8, 4, '+'),
                          RescueSpan(12, 4, '?'), RescueSpan(16, 4, '*'),
                          RescueSpan(20, 4, '/'), RescueSpan(24, 4, '-'),
                          RescueSpan(28, 2, 'unknown')])
        rescue.require_rescued(9, 3)
        for offset, length in ((7, 2), (11, 2), (12, 1), (16, 1), (20, 1), (24, 1), (28, 1)):
            with self.subTest(offset=offset), self.assertRaises(UnavailableRange):
                rescue.require_rescued(offset, length)
        self.assertEqual(list(rescue.spans(0, 0)), [])

    def test_comments_c_style_numbers_and_negative_image_translation(self):
        path = self.write('# acquisition\n0x400 % 2 # pass\n01000 010 +\n01010 0x8 +\n')
        rescue = RescueMap.from_file(path, image_offset=-512)
        self.assertEqual(list(rescue.spans(4, 12)), [RescueSpan(4, 4, '+'), RescueSpan(8, 8, '+')])
        rescue.require_rescued(0, 16)

    def test_malformed_maps_are_rejected_instead_of_reinterpreted(self):
        cases = ('', '# comments only\n', '0 4 +\n', '0 ?* 1\n0 4 +\n',
                 '0 + 0\n0 4 +\n', '0 + 1\n0 4 F\n', '0 + 1\n0 0 +\n',
                 '0 + 1\n0 4 +\n3 4 +\n', '0 + 1\n0 4 +\n5 4 +\n',
                 '0 + 1\n08 4 +\n', '0 + 1\n0 1_000 +\n',
                 '0 + 1\n9223372036854775807 4 +\n')
        for text in cases:
            with self.subTest(text=text), self.assertRaises(FormatError):
                RescueMap.from_file(self.write(text))

    def test_map_limits_are_enforced(self):
        path = self.write('0 + 1\n0 4 +\n4 4 -\n')
        for options in ({'max_bytes': 8}, {'max_blocks': 1}, {'image_offset': True}):
            with self.subTest(options=options), self.assertRaises(FormatError):
                RescueMap.from_file(path, **options)
        with self.assertRaises(FormatError):
            RescueMap.from_file(self.write('0 + 1\n' + ' '*4096 + '0 4 +\n'))


class DFXMLTests(TemporaryInputTests):
    def document(self, runs, size=6, *, namespace='', fields='', source=''):
        ns = f' xmlns="{namespace}"' if namespace else ''
        return (f'<dfxml{ns}>{source}<volume><fileobject><filename>clip.mp4</filename>'
                f'<filesize>{size}</filesize>{fields}<byte_runs>{runs}</byte_runs>'
                '</fileobject></volume></dfxml>')

    def parse(self, xml, **kwargs):
        return list(iter_dfxml(self.write(xml, 'report.xml'), 64, **kwargs))

    def test_photorec_and_namespaced_fiwalk_runs_preserve_logical_order(self):
        for attribute, namespace, kind in (
                ('offset', '', 'photorec'),
                ('file_offset', 'http://www.forensicswiki.org/wiki/Category:Digital_Forensics_XML', 'filesystem')):
            runs = (f'<byte_run {attribute}="4" img_offset="8" len="4"/>'
                    f'<byte_run {attribute}="0" img_offset="40" len="4"/>')
            with self.subTest(attribute=attribute):
                records = self.parse(self.document(runs, namespace=namespace), evidence_kind=kind)
                self.assertEqual(records, [DFXMLRecord('clip.mp4', 6, ((0, 40, 4), (4, 8, 2)), None, kind)])

    def test_sparse_or_missing_runs_preserve_later_ranges_as_unavailable(self):
        later = '<byte_run file_offset="4" img_offset="20" len="4"/>'
        for sparse in ('', '<byte_run file_offset="0" fill="0" len="4"/>'):
            with self.subTest(sparse=sparse):
                record = self.parse(self.document(sparse+later, size=8))[0]
                self.assertEqual(record.extents, ((4, 20, 4),))
                self.assertEqual(record.reason, 'unmapped_ranges')

    def test_conflicting_sparse_and_raw_claims_are_blocked(self):
        raw = '<byte_run file_offset="0" img_offset="20" len="6"/>'
        for sparse in ('<byte_run file_offset="0" fill="0" len="4"/>',
                       '<byte_run file_offset="0" fill="0"/>',
                       '<byte_run file_offset="100" fill="0" len="4"/>'):
            record = self.parse(self.document(sparse+raw))[0]
            self.assertNotIn(record.reason, (None, 'unmapped_ranges'))
        reused = ('<byte_run file_offset="0" img_offset="20" len="3"/>'
                  '<byte_run file_offset="3" img_offset="20" len="3"/>')
        self.assertEqual(self.parse(self.document(reused))[0].reason, 'overlapping_source_extents')

    def test_unsupported_mapping_types_are_blocked(self):
        for attributes in ('file_offset="0" img_offset="0" uncompressed_len="6"',
                           'file_offset="0" img_offset="0" len="6" type="resident"',
                           'file_offset="0" len="6" unknown_flags="123"',
                           'file_offset="0" len="6" fill="x"'):
            with self.subTest(attributes=attributes):
                record = self.parse(self.document(f'<byte_run {attributes}/>'))[0]
                self.assertIsNotNone(record.reason)
                self.assertNotEqual(record.reason, 'unmapped_ranges')
                self.assertEqual(record.extents, ())
        record = self.parse(self.document('<byte_run file_offset="0" img_offset="0" len="6"/>',
                                          fields='<compressed>1</compressed>'))[0]
        self.assertEqual(record.reason, 'unsupported_compressed_extent')

    def test_invalid_mapping_and_nonregular_objects_cannot_be_eligible(self):
        runs = ('<byte_run offset="0" img_offset="0" len="4"/>'
                '<byte_run offset="3" img_offset="8" len="3"/>')
        self.assertEqual(self.parse(self.document(runs))[0].reason, 'overlapping_logical_extents')
        for run in ('<byte_run offset="0" img_offset="63" len="6"/>',
                    '<byte_run offset="-1" img_offset="0" len="6"/>',
                    '<byte_run offset="0" file_offset="1" img_offset="0" len="6"/>',
                    '<byte_run offset="0" img_offset="0"/>'):
            with self.subTest(run=run):
                self.assertNotIn(self.parse(self.document(run))[0].reason, (None, 'unmapped_ranges'))
        record = self.parse(self.document('<byte_run offset="0" img_offset="0" len="6"/>',
                                          fields='<name_type>d</name_type>'))[0]
        self.assertEqual(record.reason, 'not_regular_file')

    def test_report_image_geometry_and_trailing_xml_are_verified(self):
        runs = '<byte_run offset="0" img_offset="0" len="6"/>'
        for xml in (self.document(runs, source='<source><image_size>32</image_size></source>'),
                    self.document(runs)[:-2]):
            with self.subTest(xml=xml), self.assertRaises(FormatError):
                self.parse(xml)
        record = self.parse(self.document(runs).replace('clip.mp4', '../../never-open-this.mp4'))[0]
        self.assertEqual(record.name, '../../never-open-this.mp4')
        self.assertIsNone(record.reason)

    def test_declarations_are_rejected_even_when_utf16_encoded(self):
        xml = ('<!DOCTYPE dfxml [<!ENTITY filename "private">]>'
               + self.document('<byte_run offset="0" img_offset="0" len="6"/>'))
        for encoding in ('utf-8', 'utf-16'):
            with self.subTest(encoding=encoding), self.assertRaises(FormatError):
                self.parse(xml.encode(encoding))

    def test_byte_depth_object_and_run_limits(self):
        runs = '<byte_run offset="0" img_offset="0" len="6"/>'
        xml = self.document(runs)
        for options in ({'max_bytes': 16}, {'max_depth': 3}):
            with self.subTest(options=options), self.assertRaises(FormatError):
                self.parse(xml, **options)
        multiple_runs = self.document(runs+'<byte_run offset="6" img_offset="8" len="1"/>', size=7)
        for options in ({'max_runs': 1}, {'max_runs_per_file': 1}):
            with self.subTest(options=options), self.assertRaises(FormatError):
                self.parse(multiple_runs, **options)
        with self.assertRaises(FormatError):
            self.parse(xml.replace('</volume>', '<fileobject><filesize>0</filesize></fileobject></volume>'), max_objects=1)
        with self.assertRaises(FormatError):
            self.parse(xml.replace('clip.mp4', 'x'*16_385))

    @unittest.skipUnless(shutil.which('fiwalk'), 'optional Sleuth Kit fiwalk unavailable')
    def test_real_fiwalk_noncontiguous_fat16_mapping_reconstructs_original_bytes(self):
        # Synthetic FAT16 fixture: one file at clusters 2 -> 5 -> 9, no mount.
        image = bytearray(8192*512)
        image[:11] = b'\xeb\x3c\x90MSDOS5.0'
        struct.pack_into('<HBHBHHBHHHII', image, 11,
                         512, 1, 1, 2, 512, 8192, 0xf8, 32, 32, 64, 0, 0)
        image[36:39] = bytes((0x80, 0, 0x29))
        struct.pack_into('<I', image, 39, 12345)
        image[43:54] = b'VIDEOTEST  '
        image[54:62] = b'FAT16   '
        image[510:512] = b'\x55\xaa'
        for fat_sector in (1, 33):
            for cluster, next_cluster in ((0, 0xfff8), (1, 0xffff), (2, 5), (5, 9), (9, 0xffff)):
                struct.pack_into('<H', image, fat_sector*512+cluster*2, next_cluster)
        root_offset = 65*512
        image[root_offset:root_offset+11] = b'VIDEO   MP4'
        image[root_offset+11] = 0x20
        payload = bytes(i % 251 for i in range(1500))
        struct.pack_into('<H', image, root_offset+26, 2)
        struct.pack_into('<I', image, root_offset+28, len(payload))
        for index, cluster in enumerate((2, 5, 9)):
            chunk = payload[index*512:(index+1)*512]
            offset = (97+cluster-2)*512
            image[offset:offset+len(chunk)] = chunk
        image_path = self.write(image, 'synthetic-fat16.img')
        before = hashlib.sha256(image).hexdigest()
        completed = subprocess.run([shutil.which('fiwalk'), '-x', '-z', '-g', '-G0', str(image_path)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=True)
        report = self.write(completed.stdout, 'fiwalk.xml')
        records = list(iter_dfxml(report, len(image)))
        record = next(r for r in records if r.name.upper() == 'VIDEO.MP4')
        self.assertIsNone(record.reason)
        self.assertEqual(len(record.extents), 3)
        rebuilt = b''.join(image[offset:offset+length] for _, offset, length in record.extents)
        self.assertEqual(rebuilt, payload)
        self.assertEqual(hashlib.sha256(image_path.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
