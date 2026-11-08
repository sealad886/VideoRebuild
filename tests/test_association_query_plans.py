"""Assembly domains stream indexed rows instead of building domain-sized sorters."""
from contextlib import contextmanager
from itertools import product
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from test_association_io_parity import exhaustive_hits
from test_deep_witnesses import structural_movie
from videorebuild import engine
from videorebuild.bmff import Chunk, Movie, Track
from videorebuild.storage import Case, ReaderPool, scan_logical


class AuditedQueries:
    """Check the SQL that assembly actually executes, including resumed views."""
    def __init__(self, database, test):
        self.database, self.test = database, test
        self.covered = set()

    def __getattr__(self, name):
        return getattr(self.database, name)

    def execute(self, query, parameters=()):
        normalized = ' '.join(query.lower().split())
        if normalized.startswith('select') and re.search(r'\b(?:hits|hit_locations)\b', normalized):
            # These opcodes materialize/sort a full domain under temp_store=MEMORY.
            # Their absence is a resource contract, independent of index names
            # and SQLite's human-readable query-plan wording.
            forbidden = {'SorterOpen', 'SorterSort', 'Sort', 'OpenEphemeral', 'OpenAutoindex'}
            instructions = self.database.execute('EXPLAIN '+query, parameters).fetchall()
            bad = sorted({row[1] for row in instructions} & forbidden)
            plan = self.database.execute('EXPLAIN QUERY PLAN '+query, parameters).fetchall()
            self.test.assertFalse(bad, f'assembly materializes its domain: {bad}\n{query}\n{plan}')
            if 'count(' in normalized:
                self.covered.add('count')
            if 'group by' in normalized:
                self.covered.add('fingerprint_groups')
            if 'order by' in normalized and 'fingerprint' in normalized.split('order by', 1)[1]:
                self.covered.add('fingerprint_order')
            if 'order by' in normalized and 'sid' in normalized and 'fingerprint' not in normalized.split('order by', 1)[1]:
                self.covered.add('physical_order')
        return self.database.execute(query, parameters)


def compound_movie(label):
    video = tuple(Chunk(0, index, 1000+index*1000, (8, 8), index*2) for index in range(2))
    audio = tuple(Chunk(1, index, 1016+index*1000, (2,), index) for index in range(2))
    tracks = (Track(0, 1, b'vide', b'avc1', 4, 4, 1000, 4, video, 'video'),
              Track(1, 2, b'soun', b'mp4a', 0, 2, 1000, 2, audio, 'audio'))
    chunks = tuple(chunk for pair in zip(video, audio) for chunk in pair)
    return Movie(b'', tracks, chunks, label)


def physical_assignments(groups, rows):
    """Independent small-domain oracle: ordered choices, exact nonoverlap."""
    choices = [sorted((fingerprint, sid, start) for movie, index, fingerprint, sid, start in rows
                      if (movie, index) == group.key) for group in groups]
    for selected in product(*choices):
        occupied = []
        valid = True
        for group, (_, sid, start) in zip(groups, selected):
            for chunk in group.chunks:
                lo = start+chunk.offset-group.start
                hi = lo+chunk.size
                if any(other_sid == sid and lo < other_hi and other_lo < hi
                       for other_sid, other_lo, other_hi in occupied):
                    valid = False
                    break
                occupied.append((sid, lo, hi))
            if not valid:
                break
        if valid:
            yield tuple((sid, start) for _, sid, start in selected), tuple(fp for fp, _, _ in selected)


class AssociationQueryPlanTests(unittest.TestCase):
    def exercise(self, *, certified=True, raw=False, durable=True, prune=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.bin'
            # Each fingerprint has several physical witnesses. Distinct content
            # groups and physical ordering both need streaming index access.
            data = b''.join((b'\0\0\0\x04\x65'+index.to_bytes(3, 'big'))*2+b'pq'+b'Z'*6
                            for index in range(1, 7) for _ in range(2))
            source.write_bytes(data)
            movies = ([structural_movie([1000, 2000], [8, 8])] if certified else [])
            if raw:
                movies.append(compound_movie('raw-compound'))
            if not certified and not raw:
                raise AssertionError('fixture needs a domain')
            coverage = set()
            original = engine._content_assemblies

            @contextmanager
            def no_checkpoint(*args):
                yield None

            with Case(root/'case') as case:
                sid = case.register_source(source)
                scan_logical(case, sid)
                with ReaderPool(case.sources()) as pool:
                    expected = [row[:5] for row in exhaustive_hits(movies, pool, {sid: data})]

                    def inspect(search, groups, reader, stats, task):
                        keys = {group.key for group in groups}
                        rows = [tuple(row) for row in search.execute(
                            'SELECT movie,g,fingerprint,sid,start FROM hits ORDER BY movie,g,fingerprint,sid,start')
                                if (row[0], row[1]) in keys]
                        self.assertEqual(rows, [row for row in expected if (row[0], row[1]) in keys])
                        if prune:
                            removed = next(row for row in rows if (row[0], row[1]) == groups[0].key)
                            search.execute('DELETE FROM hits WHERE movie=? AND g=? AND sid=? AND start=?',
                                           (removed[0], removed[1], removed[3], removed[4]))
                            rows.remove(removed)
                            # A shared canonical geometry retains this physical
                            # witness for its other alias's independent domain.
                            self.assertTrue(search.execute(
                                'SELECT 1 FROM hits WHERE movie=? AND g=? AND sid=? AND start=?',
                                (*groups[1].key, removed[3], removed[4])).fetchone())
                        expected_assignments = list(physical_assignments(groups, rows))
                        expected_content = {fingerprints for _, fingerprints in expected_assignments}
                        fingerprints_by_location = {(movie, index, source, at): fingerprint
                            for movie, index, fingerprint, source, at in rows}
                        audited = AuditedQueries(search, self)
                        observed_content = set()
                        for alternatives in original(audited, groups, reader, stats, task):
                            for choices in alternatives:
                                observed_content.add(tuple(fingerprints_by_location[(*group.key, *choice)]
                                                           for group, choice in zip(groups, choices)))
                        self.assertEqual(observed_content, expected_content)
                        # Exercise the physical enumerator without a fingerprint
                        # predicate as well as the content-filtered path above.
                        assignments = engine._assemblies(audited, groups, reader)
                        try:
                            self.assertEqual(list(assignments), [choices for choices, _ in expected_assignments])
                        finally:
                            assignments.close()
                        coverage.update(audited.covered)
                        yield from ()

                    with patch.object(engine, '_content_assemblies', inspect), \
                            patch.object(engine, 'association_checkpoint',
                                         engine.association_checkpoint if durable else no_checkpoint):
                        engine.deep_search(case, movies, pool, {},
                                           engine.Options(deep=True, reconstruct_only=True), 'query-plans')
            self.assertEqual(coverage, {'count', 'fingerprint_groups', 'fingerprint_order', 'physical_order'})
            self.assertEqual(source.read_bytes(), data)

    def test_plain_raw_domains_stream_aggregates_and_ordered_choices(self):
        self.exercise(certified=False, raw=True)

    def test_durable_certified_aliases_stream_aggregates_and_ordered_choices(self):
        self.exercise()

    def test_durable_alias_pruning_keeps_independent_indexed_domains(self):
        self.exercise(prune=True)

    def test_noncheckpoint_alias_pruning_keeps_independent_indexed_domains(self):
        self.exercise(durable=False, prune=True)

    def test_mixed_raw_and_durable_domains_stream_same_assembly_queries(self):
        self.exercise(raw=True)


if __name__ == '__main__':
    unittest.main()
