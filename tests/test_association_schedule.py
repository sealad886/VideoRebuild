"""Certified geometries share one source-ordered posting pass per profile."""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_deep_witnesses import structural_movie
from videorebuild import engine
from videorebuild.bmff import Chunk


class AssociationScheduleTests(unittest.TestCase):
    def test_canonical_geometries_share_cursor_and_preserve_each_group_location_order(self):
        prototype = structural_movie([1000, 2000, 3000], [8, 8, 8])
        audio = replace(prototype.tracks[0], index=1, track_id=2, handler=b'soun', codec=b'mp4a')
        # Groups zero and two share a complete geometry. Group one shares
        # their framing profile but has a different adjacent audio length.
        chunks = tuple(chunk for i, size in enumerate((4, 8, 4)) for chunk in (
            Chunk(0, i, (i+1)*1000, (8,), i), Chunk(1, i, (i+1)*1000+8, (size,), i)))
        movie = replace(prototype, tracks=(prototype.tracks[0], audio), chunks=chunks)
        locations = [(1, 0), (1, 32)]
        certificates = SimpleNamespace(locations=Mock(side_effect=lambda profile: iter(locations)))
        checks, visited = [], []
        pool = SimpleNamespace(sources={1: dict(size=64)}, check=checks.append,
                               permits=lambda sid, offset, size: 0 <= offset <= offset+size <= 64)
        search, discovery = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
        self.addCleanup(search.close)
        self.addCleanup(discovery.close)

        @contextmanager
        def store(case):
            yield search

        def fingerprint(group, mapping, reader, *, read_cache=None):
            visited.append((group.index, mapping[(0, group.index)]))
            self.assertEqual(len(checks), len(visited))
            return str(group.index)

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(engine, '_search_store', store), \
             patch.object(engine, 'require_search_space'), \
             patch.object(engine, '_group_hash', side_effect=fingerprint), \
             patch.object(engine, '_content_assemblies', return_value=(choice for choice in ())), \
             patch.object(engine, 'finish'):
            engine._deep_batch(SimpleNamespace(root=Path(directory)), [movie], pool, {},
                               engine.Options(), 'schedule', set(), discovery=discovery,
                               certificates=certificates)
        certificates.locations.assert_called_once_with((4, b'avc1', 4))
        self.assertEqual(visited, [(0, (1, 0)), (1, (1, 0)), (0, (1, 32)), (1, (1, 32))])
        for index in (0, 1):
            self.assertEqual([location for group, location in visited if group == index], locations)
