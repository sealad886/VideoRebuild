"""A bounded footprint cache must retain hot choices without changing witnesses."""
from collections import OrderedDict
from itertools import product
import sqlite3
from types import SimpleNamespace
import unittest

from videorebuild import engine
from videorebuild.bmff import Chunk, FormatError
from videorebuild.progress import phase
from videorebuild.storage import SafetyError


class AssemblyFootprintTests(unittest.TestCase):
    def test_recent_choices_stay_cached_after_more_than_4096_choices(self):
        calls, checks = [], []

        def provenance(sid, start, size):
            calls.append(start)
            return [dict(source_id=sid, source_offset=start, length=size)]

        pool = SimpleNamespace(check=checks.append, physical_key=lambda sid: (1, sid), provenance=provenance)
        group = SimpleNamespace(key=('fixture', 0), chunks=[Chunk(0, 0, 0, (8,), 0)], start=0)
        cache = OrderedDict()
        for choice in range(4096):
            tuple(engine._group_physical_ranges(group, (1, choice*8), pool, cache))
        for choice in (0, 4096, 0, 4096):
            self.assertEqual(tuple(engine._group_physical_ranges(group, (1, choice*8), pool, cache)),
                             ((1, 1, choice*8, choice*8+8),))
        self.assertEqual(len(calls), 4097)
        self.assertEqual(checks, [1, 1, 1])
        tuple(engine._group_physical_ranges(group, (1, 8), pool, cache))
        self.assertEqual(calls[-1], 8)
        self.assertEqual(len(cache), 4096)

    def test_reuse_checks_source_and_unavailable_provenance_is_not_cached(self):
        group = SimpleNamespace(key=('fixture', 0), chunks=[Chunk(0, 0, 0, (8,), 0)], start=0)
        cache = OrderedDict()
        state = dict(available=False, changed=False)

        def provenance(sid, start, size):
            if not state['available']:
                raise FormatError('unavailable range')
            return [dict(source_id=sid, source_offset=start, length=size)]

        def check(sid):
            if state['changed']:
                raise SafetyError('source changed')

        pool = SimpleNamespace(check=check, physical_key=lambda sid: (1, sid), provenance=provenance)
        with self.assertRaises(FormatError):
            tuple(engine._group_physical_ranges(group, (1, 0), pool, cache))
        self.assertFalse(cache)
        state['available'] = True
        tuple(engine._group_physical_ranges(group, (1, 0), pool, cache))
        state['changed'] = True
        with self.assertRaises(SafetyError):
            tuple(engine._group_physical_ranges(group, (1, 0), pool, cache))

    def test_large_domains_preserve_all_physical_alias_witnesses(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE hits(movie,g,fingerprint,sid,start)')
        db.execute('CREATE INDEX hits_order ON hits(movie,g,fingerprint,sid,start)')
        choices = [[(1, 0), (2, 0)], [(1, i*4) for i in range(4100)]]
        groups = [SimpleNamespace(key=('fixture', i), chunks=[Chunk(0, i, 0, (8,), 0)], start=0)
                  for i in range(2)]
        for index, domain in enumerate(choices):
            db.executemany('INSERT INTO hits VALUES(?,?,?,?,?)',
                           (('fixture', index, str(index), sid, start) for sid, start in domain))
        pool = SimpleNamespace(check=lambda sid: None, physical_key=lambda sid: (1, 1),
                               provenance=lambda sid, start, size: [dict(source_id=sid, source_offset=start, length=size)])
        expected = {assignment for assignment in product(*choices)
                    if assignment[0][1]+8 <= assignment[1][1] or assignment[1][1]+8 <= assignment[0][1]}
        stats = dict(partial_assignments_checked=0, partial_assignments_rejected=0,
                     content_prefixes_checked=0, content_prefixes_rejected=0, content_tuples_considered=0)
        with phase('test') as task:
            contents = engine._content_assemblies(db, groups, pool, stats, task)
            self.assertEqual(set(next(contents)), expected)
            self.assertEqual(list(contents), [])
        self.assertTrue(stats['constraint_skipped_large_domains'])
