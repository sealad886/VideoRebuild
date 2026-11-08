"""Positive byte reuse remains bound to current source and eligibility checks."""
from collections import OrderedDict
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_structural_index import source_row
from videorebuild import engine
from videorebuild.bmff import Chunk, FormatError
from videorebuild.candidate_tiers import _TierView
from videorebuild.ranges import AllocationScope
from videorebuild.storage import ReaderPool, SafetyError


class AssociationReadCacheTests(unittest.TestCase):
    def group(self, *sizes):
        offset = 0
        chunks = []
        mapping = {}
        for index, size in enumerate(sizes):
            chunks.append(Chunk(index, 0, offset, (size,), 0))
            mapping[index, 0] = 1, offset
            offset += size
        return SimpleNamespace(chunks=chunks), mapping

    def test_shared_video_read_is_reused_but_each_audio_range_is_exact(self):
        data = b'video123audio456audio789'
        reads, permits = [], []
        pool = SimpleNamespace(read=lambda sid, offset, size: reads.append((sid, offset, size)) or data[offset:offset+size],
                               permits=lambda sid, offset, size: permits.append((sid, offset, size)) or True)
        cache = OrderedDict()
        for audio in (4, 8, 12):
            group, mapping = self.group(8, audio)
            digest = engine._group_hash(group, mapping, pool, read_cache=cache)
            self.assertEqual(digest, hashlib.sha256(data[:8]+data[8:8+audio]).hexdigest())
        self.assertEqual(reads, [(1, 0, 8), (1, 8, 4), (1, 8, 8), (1, 8, 12)])
        self.assertEqual(permits, [(1, 0, 8), (1, 0, 8)])

    def test_source_rewrite_with_same_size_and_mtime_rejects_hit(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'source.bin'
            source.write_bytes(b'original')
            stamp = source.stat()
            group, mapping = self.group(8)
            with ReaderPool({1: source_row(source)}) as pool:
                cache = OrderedDict()
                engine._group_hash(group, mapping, pool, read_cache=cache)
                source.write_bytes(b'changed!')
                os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                with self.assertRaises(SafetyError):
                    engine._group_hash(group, mapping, pool, read_cache=cache)

    def test_current_allocation_and_tier_permissions_gate_cached_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'image.bin'
            source.write_bytes(b'original'*2)
            group, mapping = self.group(8)
            with ReaderPool({1: source_row(source, 'image')},
                            allocation_scope=AllocationScope({1: ((0, 8),)}, 'allowed')) as pool:
                cache = OrderedDict()
                digest = engine._group_hash(group, mapping, pool, read_cache=cache)
                pool.allocation_scope = AllocationScope({1: ((8, 8),)}, 'excluded')
                with self.assertRaises(FormatError):
                    engine._group_hash(group, mapping, pool, read_cache=cache)
                pool.allocation_scope = AllocationScope({1: ((0, 8),)}, 'allowed')
                blocked = []
                intervals = SimpleNamespace(overlaps=lambda *args, **kwargs: blocked)
                tiers = SimpleNamespace(pool=pool, _intervals={pool.physical_key(1): intervals})
                view = _TierView(tiers, None)
                blocked.append((0, 8))
                with self.assertRaises(FormatError):
                    engine._group_hash(group, mapping, view, read_cache=cache)
                blocked.clear()
                self.assertEqual(engine._group_hash(group, mapping, view, read_cache=cache), digest)

    def test_same_mtime_rescue_map_change_rejects_hit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, rescue = root/'image.bin', root/'image.map'
            source.write_bytes(b'original')
            rescue.write_text('0 + 1\n0 8 +\n')
            bound = source_row(rescue)
            row = source_row(source, 'image') | {'binding': {'mapfile': bound}}
            group, mapping = self.group(8)
            with ReaderPool({1: row}) as pool:
                cache = OrderedDict()
                engine._group_hash(group, mapping, pool, read_cache=cache)
                stamp = rescue.stat()
                rescue.write_text('0 + 1\n0 8 ?\n')
                os.utime(rescue, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                with self.assertRaises(SafetyError):
                    engine._group_hash(group, mapping, pool, read_cache=cache)

    def test_logical_aliases_do_not_share_cache_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, alias = root/'source.bin', root/'alias.bin'
            source.write_bytes(b'original')
            alias.hardlink_to(source)
            group, mapping = self.group(8)
            with ReaderPool({1: source_row(source), 2: source_row(alias)}) as pool, \
                 patch.object(pool, 'read', wraps=pool.read) as read:
                cache = OrderedDict()
                first = engine._group_hash(group, mapping, pool, read_cache=cache)
                second = engine._group_hash(group, {(0, 0): (2, 0)}, pool, read_cache=cache)
                self.assertEqual(first, second)
                self.assertEqual(read.call_count, 2)

    def test_failed_or_partial_read_is_not_cached_as_a_complete_request(self):
        group, mapping = self.group(8)
        cache = OrderedDict()

        def failed_read(*args):
            raise OSError('read failed')

        pool = SimpleNamespace(read=failed_read, permits=lambda *args: True)
        with self.assertRaises(OSError):
            engine._group_hash(group, mapping, pool, read_cache=cache)
        self.assertFalse(cache)
        data = b'original'
        pool.read = lambda sid, offset, size: data[offset:offset+min(size, 4)]
        self.assertEqual(engine._group_hash(group, mapping, pool, read_cache=cache), hashlib.sha256(data).hexdigest())
        self.assertNotIn((1, 0, 8), cache)

    def test_four_entry_cache_keeps_at_most_sixteen_mebibytes(self):
        size = 4*1024*1024
        data = b'x'*size
        calls = []
        pool = SimpleNamespace(read=lambda sid, offset, count: calls.append(count) or data,
                               permits=lambda *args: True)
        group, _ = self.group(size)
        cache = OrderedDict()
        for index in range(5):
            engine._group_hash(group, {(0, 0): (1, index*size)}, pool, read_cache=cache)
        self.assertEqual(calls, [size]*5)
        self.assertEqual(len(cache), 4)
        self.assertLessEqual(sum(map(len, cache.values())), 16*1024*1024)
        self.assertNotIn((1, 0, size), cache)
