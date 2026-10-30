"""Physical-interval pruning must preserve the exhaustive feasible choice set."""
from itertools import product
import random
import sqlite3
from types import SimpleNamespace
import unittest

from videorebuild import engine
from videorebuild.bmff import Chunk
from videorebuild.progress import phase


class AssemblyPruningTests(unittest.TestCase):
    def assemblies(self, choices, identities):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE hits(movie,g,fingerprint,sid,start)')
        groups = []
        for index, locations in enumerate(choices):
            groups.append(SimpleNamespace(key=('fixture', index), chunks=[Chunk(0, index, 0, (8,), 0)], start=0))
            for rank, (sid, start) in enumerate(locations):
                db.execute('INSERT INTO hits VALUES(?,?,?,?,?)', ('fixture', index, str(rank), sid, start))
        pool = SimpleNamespace(physical={sid: dict(dev=1, ino=ino) for sid, ino in identities.items()},
                               provenance=lambda sid, start, size: [dict(source_id=sid, source_offset=start, length=size)])
        stats = dict(partial_assignments_checked=0, partial_assignments_rejected=0)
        actual = set(engine._assemblies(db, groups, pool, counts=list(map(len, choices)), stats=stats))
        return actual, stats

    def test_differential_feasibility_including_physical_aliases(self):
        rng = random.Random(773)
        for fixture in range(50):
            identities = {sid: (1 if fixture % 2 == 0 and sid == 2 else sid) for sid in range(1, 8)}
            choices = [[(sid, rng.choice((0, 4, 8))) for sid in rng.sample(range(1, 8), rng.randint(1, 3))]
                       for _ in range(rng.randint(2, 5))]
            expected = set()
            for assignment in product(*choices):
                intervals = [(identities[sid], start, start+8) for sid, start in assignment]
                if all(a[0] != b[0] or a[1] >= b[2] or b[1] >= a[2]
                       for i, a in enumerate(intervals) for b in intervals[i+1:]):
                    expected.add(assignment)
            actual, _ = self.assemblies(choices, identities)
            self.assertEqual(actual, expected, fixture)

    def test_singleton_content_prefixes_use_linear_provenance_work(self):
        count = 512
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE hits(movie,g,fingerprint,sid,start)')
        db.execute('CREATE INDEX hits_order ON hits(movie,g,fingerprint,sid,start)')
        groups = [SimpleNamespace(key=('fixture', i), chunks=[Chunk(0, i, 0, (8,), 0)], start=0) for i in range(count)]
        db.executemany('INSERT INTO hits VALUES(?,?,?,?,?)', (('fixture', i, 'one', i+1, 0) for i in range(count)))
        calls = []
        def provenance(sid, start, size):
            calls.append(sid)
            return [dict(source_id=sid, source_offset=start, length=size)]
        pool = SimpleNamespace(physical={i+1: dict(dev=1, ino=i+1) for i in range(count)}, provenance=provenance)
        stats = dict(partial_assignments_checked=0, partial_assignments_rejected=0,
                     content_prefixes_checked=0, content_prefixes_rejected=0, content_tuples_considered=0)
        with phase('test') as task:
            contents = engine._content_assemblies(db, groups, pool, stats, task)
            witnesses = next(contents)
            self.assertEqual(len(next(witnesses)), count)
            contents.close()
        self.assertLessEqual(len(calls), 3 * count)
        self.assertEqual(stats['content_tuples_considered'], 1)

    def test_greedy_extension_failure_reassigns_earlier_location(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE hits(movie,g,fingerprint,sid,start)')
        # Group zero has two witnesses; group one must occupy bytes 8..32.
        db.executemany('INSERT INTO hits VALUES(?,?,?,?,?)', [('fixture', 0, 'A', 1, 0),
                       ('fixture', 0, 'A', 1, 32), ('fixture', 1, 'B', 1, 8)])
        groups = [SimpleNamespace(key=('fixture', i), chunks=[Chunk(0, i, 0, (size,), 0)], start=0)
                  for i, size in enumerate((16, 24))]
        pool = SimpleNamespace(physical={1: dict(dev=1, ino=1)},
                               provenance=lambda sid, start, size: [dict(source_id=sid, source_offset=start, length=size)])
        stats = dict(partial_assignments_checked=0, partial_assignments_rejected=0,
                     content_prefixes_checked=0, content_prefixes_rejected=0, content_tuples_considered=0)
        with phase('test') as task:
            contents = engine._content_assemblies(db, groups, pool, stats, task)
            self.assertEqual(next(next(contents)), ((1, 32), (1, 8)))
            contents.close()
        self.assertEqual(stats['content_prefixes_rejected'], 0)

    def test_conflicting_prefix_does_not_expand_binary_suffix(self):
        choices = [[(1, 0)], [(1, 0)]] + [[(2*i, 0), (2*i+1, 0)] for i in range(2, 18)]
        actual, stats = self.assemblies(choices, {sid: sid for sid in range(1, 37)})
        self.assertEqual(actual, set())
        self.assertEqual(stats['partial_assignments_rejected'], 1)
        self.assertLess(stats['partial_assignments_checked'], 10)


if __name__ == '__main__':
    unittest.main()
