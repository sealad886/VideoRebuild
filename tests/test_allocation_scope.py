"""Allocation evidence excludes allocated image bytes before canonical reads."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.bmff import FormatError
from videorebuild.ranges import Extent, UnavailableRange, load_allocation_scope
from videorebuild.storage import Case, ReaderPool, scan_logical


class AllocationScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root/'image.bin'
        self.image.write_bytes(bytes(range(32)))
        self.row = self.source(self.image, 'image')
        self.policy = self.root/'allocation.json'

    def source(self, path, kind):
        stat = path.stat()
        return dict(path=str(path.resolve()), kind=kind, size=stat.st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    mtime_ns=stat.st_mtime_ns, dev=stat.st_dev, ino=stat.st_ino)

    def document(self, ranges):
        return dict(schema=1, kind='filesystem-unallocated',
                    images=[dict(path=str(self.image), size=self.row['size'],
                                 sha256=self.row['sha256'], unallocated_ranges=ranges)])

    def load(self, ranges, physical=None):
        self.policy.write_text(json.dumps(self.document(ranges)))
        return load_allocation_scope(self.policy, physical or {1:self.row})

    def test_allocated_bytes_cannot_be_read_or_claimed_as_provenance(self):
        scope = self.load([[4,8], [20,4]])
        with ReaderPool({1:self.row}, allocation_scope=scope) as pool:
            with patch.object(pool, 'handle', wraps=pool.handle) as handle:
                self.assertFalse(pool.permits(1,0,8))
                self.assertFalse(pool.permits(1,8,16))
                self.assertTrue(pool.permits(1,4,8))
                self.assertFalse(pool.permits(1,31,2))
                self.assertFalse(pool.permits(9,0,1))
                for offset,length in ((0,8), (8,16)):
                    with self.assertRaises(UnavailableRange):
                        pool.read(1,offset,length)
                    with self.assertRaises(UnavailableRange):
                        pool.provenance(1,offset,length)
                handle.assert_not_called()
            self.assertEqual(pool.read(1,4,8), bytes(range(4,12)))
            self.assertEqual(list(pool.available_ranges(1)), [(4,8),(20,4)])

    def test_small_allowed_read_does_not_prefetch_allocated_image_bytes(self):
        scope = self.load([[4,8]])
        with ReaderPool({1:self.row}, allocation_scope=scope) as pool:
            self.assertEqual(pool.read(1,4,8), bytes(range(4,12)))
            # The OS descriptor position exposes reads hidden by a Python
            # buffering layer: it must stop at the allowed range's end.
            self.assertEqual(os.lseek(pool.handle(1).fileno(), 0, os.SEEK_CUR), 12)
        self.assertEqual(self.image.read_bytes(), bytes(range(32)))

    def test_entire_logical_request_checked_before_first_extent_is_read(self):
        scope = self.load([[4,4]])
        logical = dict(kind='logical', size=8,
                       extents=[Extent(0,1,4,4),Extent(4,1,20,4)])
        with ReaderPool({2:logical}, physical_sources={1:self.row}, allocation_scope=scope) as pool:
            with patch.object(pool, 'handle', wraps=pool.handle) as handle:
                self.assertFalse(pool.permits(2,0,8))
                with self.assertRaises(UnavailableRange):
                    pool.read(2,0,8)
                with self.assertRaises(UnavailableRange):
                    pool.provenance(2,0,8)
                handle.assert_not_called()
            self.assertEqual(list(pool.available_ranges(2)), [(0,4)])
            self.assertEqual(pool.read(2,0,4), bytes(range(4,8)))

    def test_unmapped_logical_hole_is_not_permitted(self):
        scope = self.load([[0,32]])
        logical = dict(kind='logical', size=12,
                       extents=[Extent(0,1,0,4),Extent(8,1,8,4)])
        with ReaderPool({2:logical}, physical_sources={1:self.row}, allocation_scope=scope) as pool:
            self.assertFalse(pool.permits(2,0,12))
            self.assertEqual(list(pool.available_ranges(2)), [(0,4),(8,4)])

    def test_allocation_and_rescue_ranges_intersect_in_logical_coordinates(self):
        rescue = self.root/'rescue.map'
        rescue.write_text('0 + 1\n0 12 +\n12 8 -\n20 12 +\n')
        row = dict(self.row, binding=dict(mapfile=self.source(rescue,'file'), image_offset=0))
        scope = self.load([[4,24]])
        with ReaderPool({1:row}, allocation_scope=scope) as pool:
            self.assertEqual(list(pool.available_ranges(1)), [(4,8),(20,8)])
            self.assertFalse(pool.permits(1,4,24))
        logical = dict(kind='logical', size=16,
                       extents=[Extent(0,1,4,8),Extent(8,1,20,8)])
        with ReaderPool({2:logical}, physical_sources={1:row}, allocation_scope=scope) as pool:
            self.assertEqual(list(pool.available_ranges(2)), [(0,16)])
            self.assertTrue(pool.permits(2,0,16))
            self.assertEqual(pool.read(2,0,16), bytes(range(4,12))+bytes(range(20,28)))

    def test_recovered_files_remain_available_and_scope_digest_tracks_policy(self):
        recovered = self.root/'recovered.mp4'
        recovered.write_bytes(b'previously carved media')
        file_row = self.source(recovered,'file')
        scope = self.load([])
        with ReaderPool({1:self.row,2:file_row}, allocation_scope=scope) as pool:
            self.assertEqual(list(pool.available_ranges(1)), [])
            self.assertEqual(pool.read(2,0,file_row['size']), recovered.read_bytes())
        other = self.load([[4,8]])
        self.assertNotEqual(scope.digest,other.digest)
        self.policy.write_text(json.dumps(self.document([[4,8]]),indent=4))
        self.assertEqual(other.digest,load_allocation_scope(self.policy,{1:self.row}).digest)

    def test_bad_image_bindings_and_malformed_ranges_are_rejected(self):
        changes = [lambda d:d.update(schema=True),
                   lambda d:d['images'][0].update(size=31),
                   lambda d:d['images'][0].update(sha256='0'*64),
                   lambda d:d['images'][0].update(path=str(self.root/'other-image')),
                   lambda d:d.update(images=[]),
                   lambda d:d['images'].append(dict(d['images'][0]))]
        for change in changes:
            document = self.document([[4,8]])
            change(document)
            self.policy.write_text(json.dumps(document))
            with self.subTest(document=document), self.assertRaises(FormatError):
                load_allocation_scope(self.policy,{1:self.row})
        for ranges in ([[8,8],[4,2]], [[4,8],[10,4]], [[31,2]], [[-1,4]],
                       [[4,0]], [[True,4]], [[4]], 'all'):
            with self.subTest(ranges=ranges), self.assertRaises(FormatError):
                self.load(ranges)
        self.policy.write_text('{bad json')
        with self.assertRaises(FormatError):
            load_allocation_scope(self.policy,{1:self.row})

    def test_allocation_policy_preserves_existing_source_catalogue(self):
        with Case(self.root/'case') as case:
            sid = case.register_source(self.image,'image')
            scan_logical(case,sid)
            before_sources = [tuple(row) for row in case.db.execute('SELECT * FROM sources')]
            before_atoms = [tuple(row) for row in case.db.execute('SELECT * FROM atoms')]
            scope = self.load([[4,8]],case.physical_sources())
            with ReaderPool(case.sources(),physical_sources=case.physical_sources(),allocation_scope=scope) as pool:
                self.assertEqual(pool.read(sid,4,8),bytes(range(4,12)))
                self.assertEqual(list(pool.available_ranges(sid)),[(4,8)])
            self.assertEqual([tuple(row) for row in case.db.execute('SELECT * FROM sources')],before_sources)
            self.assertEqual([tuple(row) for row in case.db.execute('SELECT * FROM atoms')],before_atoms)
