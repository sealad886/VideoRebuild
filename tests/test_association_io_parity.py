"""Association I/O changes must retain every unpruned physical witness."""
from contextlib import contextmanager
from collections import defaultdict
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from videorebuild import engine
from videorebuild.bmff import Chunk, FormatError, Movie, Track, valid_sample
from videorebuild.candidate_tiers import CandidateTiers
from videorebuild.ranges import AllocationScope, Extent
from videorebuild.storage import Case, ReaderPool, SafetyError, file_sha256, scan_logical, write_json


TOKEN = b'\0\0\0\x04\x65ABC'
DECOY = b'\0\0\0\x04\x40\x01ZZ'


def movie_with_audio(label, offset, gap=0):
    audio = (Chunk(1, 0, offset, (3,), 0), Chunk(1, 1, offset+11+2*gap, (2,), 1))
    video = Chunk(0, 0, offset+3+gap, (len(TOKEN),), 0)
    chunks = (audio[0], video, audio[1])
    tracks = (Track(0, 1, b'vide', b'avc1', 4, 1, 1000, 1, (video,), 'video'),
              Track(1, 2, b'soun', b'mp4a', 0, 2, 1000, 2, audio, 'audio'))
    return Movie(b'', tracks, chunks, hashlib.sha256(label.encode()).hexdigest())


def ordered_group_hash(group, mapping, pool):
    """Independent ordered-byte hash used by the complete hit-set oracle."""
    digest = hashlib.sha256()
    for chunk in group.chunks:
        sid, offset = mapping[(chunk.track, chunk.index)]
        remaining = chunk.size
        while remaining:
            raw = pool.read(sid, offset, min(remaining, 4*1024*1024))
            digest.update(raw)
            offset += len(raw)
            remaining -= len(raw)
    return digest.hexdigest()


def capture_association(case, movies, pool, options=None):
    """Capture before assembly pruning, including alternatives sharing content."""
    rows = []
    mapping_order = defaultdict(list)
    groups = {group.key: group for movie in movies for group in engine.groups_for(movie)}
    original_store = engine._search_store
    original_mapping = engine.Group.mapping

    def mapping(group, sid, start):
        mapping_order[group.key].append((sid, start))
        return original_mapping(group, sid, start)

    @contextmanager
    def capture(scratch_case):
        with original_store(scratch_case) as store:
            yield store
            for movie, index, fingerprint, sid, start in store.execute(
                    'SELECT movie,g,fingerprint,sid,start FROM hits ORDER BY movie,g,fingerprint,sid,start'):
                group = groups[(movie, index)]
                physical = tuple(
                    (chunk.track, chunk.index, part['source_id'], part['source_offset'], part['length'])
                    for chunk in group.chunks
                    for part in pool.provenance(sid, start+chunk.offset-group.start, chunk.size))
                rows.append((movie, index, fingerprint, sid, start, physical))

    def no_assemblies(*args):
        yield from ()

    with patch.object(engine, '_search_store', capture), \
            patch.object(engine.Group, 'mapping', mapping), \
            patch.object(engine, '_content_assemblies', no_assemblies):
        engine.deep_search(case, movies, pool, {}, options or engine.Options(deep=True, reconstruct_only=True), 'io-parity')
    fields = ('anchor_associations', 'anchor_checks', 'located_extents', 'hit_counts', 'nal_checks',
              'posting_probes', 'posting_rejections', 'duplicate_bases', 'stop_reason', 'search_limited')
    counters = {movie.digest: {name: case.job('ordinary', movie.digest)['detail'][name] for name in fields}
                for movie in movies}
    return dict(hits=rows, mapping_order=dict(mapping_order), counters=counters)


def exhaustive_hits(movies, pool, data_by_source):
    """Enumerate source byte offsets without structural postings or certificates."""
    rows = []
    for movie in movies:
        for group in engine.groups_for(movie):
            for sid, data in data_by_source.items():
                for start in range(len(data)-group.span+1):
                    if any(not pool.permits(sid, start+chunk.offset-group.start, chunk.size) for chunk in group.chunks):
                        continue
                    valid = True
                    for chunk in group.chunks:
                        track = movie.tracks[chunk.track]
                        if track.handler != b'vide':
                            continue
                        offset = start+chunk.offset-group.start
                        for size in chunk.sizes:
                            if not valid_sample(data[offset:offset+size], track.codec, track.nal_length):
                                valid = False
                                break
                            offset += size
                        if not valid:
                            break
                    if valid:
                        mapping = {(chunk.track, chunk.index): (sid, start+chunk.offset-group.start)
                                   for chunk in group.chunks}
                        fingerprint = ordered_group_hash(group, mapping, pool)
                        physical = tuple((chunk.track, chunk.index, part['source_id'], part['source_offset'], part['length'])
                                         for chunk in group.chunks
                                         for part in pool.provenance(sid, start+chunk.offset-group.start, chunk.size))
                        rows.append((*group.key, fingerprint, sid, start, physical))
    return sorted(rows)


class AssociationIOParityTests(unittest.TestCase):
    def test_hash_respects_logical_mapping_rescue_and_allocation_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root/'first.bin'
            second = root/'second.bin'
            first.write_bytes(b'A'*32)
            second.write_bytes(b'B'*32)
            first_map = root/'first.map'
            second_map = root/'second.map'
            first_map.write_text('0 + 1\n0 8 +\n8 24 -\n')
            second_map.write_text('0 + 1\n0 16 -\n16 8 +\n24 8 -\n')
            with Case(root/'case') as case:
                a = case.register_source(first, 'image', mapfile=first_map)
                b = case.register_source(second, 'image', mapfile=second_map)
                joined = case.add_stream('joined', [Extent(0, a, 0, 8), Extent(8, b, 16, 8)], size=16)
                gapped = case.add_stream('gapped', [Extent(0, a, 0, 8), Extent(16, b, 16, 8)], size=24)
                rows = {row['id']: dict(row) for row in case.db.execute(
                    'SELECT * FROM sources WHERE id IN (?,?)', (joined, gapped))}
                scope = AllocationScope({a: ((0, 8),), b: ((16, 8),)}, 'hash-read-boundary')
                with ReaderPool(rows, physical_sources=case.physical_sources(), allocation_scope=scope) as pool:
                    expected = hashlib.sha256(b'A'*8+b'B'*8).hexdigest()
                    for sid, second_offset in ((joined, 8), (gapped, 16)):
                        group = SimpleNamespace(chunks=(Chunk(0, 0, 0, (8,), 0),
                                                        Chunk(0, 1, second_offset, (8,), 1)))
                        mapping = {(0, 0): (sid, 0), (0, 1): (sid, second_offset)}
                        self.assertEqual(ordered_group_hash(group, mapping, pool), expected)
                        self.assertEqual(engine._group_hash(group, mapping, pool), expected)
                    with self.assertRaises(FormatError):
                        pool.read(gapped, 0, 24)
                    first.write_bytes(b'C'*32)
                    with self.assertRaises(SafetyError):
                        engine._group_hash(group, mapping, pool)

    def test_all_hits_keep_audio_geometry_aliases_and_owner_permissions(self):
        adjacent = b'abc'+TOKEN+b'pq'
        gapped = b'abcZZ'+TOKEN+b'YYpq'
        raw = adjacent+b'X'*7+adjacent+b'X'*7+gapped+b'X'*7+b'abc'+DECOY+b'pq'
        movies = [movie_with_audio('adjacent', 100), movie_with_audio('shared', 200),
                  movie_with_audio('gapped', 300, gap=2)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.bin'
            alias = root/'alias.bin'
            source.write_bytes(raw)
            alias.hardlink_to(source)
            with Case(root/'case') as case:
                for path in (source, alias):
                    scan_logical(case, case.register_source(path))
                with ReaderPool(case.sources()) as pool:
                    sid = min(pool.sources)
                    candidate = case.root/'candidates'/'claimed.bin'
                    output = case.root/'reconstructed'/'claimed.bin'
                    candidate.write_bytes(TOKEN)
                    output.write_bytes(TOKEN)
                    write_json(candidate.with_suffix('.json'), {'provenance': {'chunks': [
                        {'physical_extents': pool.provenance(sid, 3, len(TOKEN))}]}})
                    case.result('claimed', 'reconstructed', {'outputs': [str(output.relative_to(case.root))],
                        'candidates': [{'path': str(candidate.relative_to(case.root)), 'sha256': file_sha256(candidate)}]})
                    tiers = CandidateTiers(case, pool)
                    snapshots = {}
                    for label, view in (('base', pool), ('unclaimed', tiers.view()),
                                        ('owner', tiers.view(('ordinary', 'claimed')))):
                        expected = exhaustive_hits(movies, view, {sid: raw for sid in pool.sources})
                        observed = capture_association(case, movies, view)
                        self.assertEqual(observed['hits'], expected, label)
                        self.assertTrue(observed['hits'])
                        self.assertEqual({row[3] for row in observed['hits']}, set(pool.sources))
                        for order in observed['mapping_order'].values():
                            self.assertEqual(order, sorted(order))
                        for movie in movies:
                            counts = observed['counters'][movie.digest]
                            self.assertEqual(counts['anchor_associations'], 12)
                            self.assertEqual(counts['anchor_checks'], 8 if label == 'unclaimed' else 10)
                            self.assertEqual(counts['located_extents'], 1)
                            self.assertEqual(counts['hit_counts'], [sum(row[0] == movie.digest for row in expected)])
                            self.assertFalse(counts['search_limited'])
                        snapshots[label] = observed['hits']
                    self.assertLess(len(snapshots['unclaimed']), len(snapshots['owner']))
                    self.assertEqual(snapshots['base'], snapshots['owner'])
                    for movie in movies[:2]:
                        alternatives = [row for row in snapshots['base'] if row[0] == movie.digest]
                        self.assertGreater(len(alternatives), len({row[2] for row in alternatives}))
            self.assertEqual(source.read_bytes(), raw)

    def test_mixed_raw_certified_and_capped_paths_keep_complete_hits(self):
        simple = movie_with_audio('single', 100)
        video = Chunk(0, 0, 200, (len(TOKEN), len(TOKEN)), 0)
        audio = Chunk(1, 0, 216, (2,), 0)
        tracks = (Track(0, 1, b'vide', b'avc1', 4, 2, 1000, 2, (video,), 'video'),
                  Track(1, 2, b'soun', b'mp4a', 0, 1, 1000, 1, (audio,), 'audio'))
        compound = Movie(b'', tracks, (video, audio), 'compound')
        movies = [simple, compound]
        raw = TOKEN+TOKEN+b'pq'+b'Z'*8+DECOY+TOKEN+b'pq'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.bin'
            source.write_bytes(raw)
            with Case(root/'case') as case:
                sid = case.register_source(source)
                scan_logical(case, sid)
                with ReaderPool(case.sources()) as pool:
                    expected = exhaustive_hits(movies, pool, {sid: raw})
                    observed = capture_association(case, movies, pool)
                    self.assertEqual(observed['hits'], expected)
                    self.assertTrue(any(row[0] == compound.digest for row in expected))
                    options = engine.Options(deep=True, reconstruct_only=True, max_anchor_checks=1)
                    with patch.object(engine, 'sample_certificates', side_effect=AssertionError('capped path certified')), \
                            patch.object(engine.Group, 'sample_profile', side_effect=AssertionError('capped profile lookup')):
                        limited = capture_association(case, movies, pool, options)
                    for counts in limited['counters'].values():
                        self.assertTrue(counts['search_limited'])
                        self.assertIn(counts['stop_reason'], ('nal_check_budget', 'anchor_check_budget'))

    def test_association_io_failure_never_publishes_a_negative_completion(self):
        raw = b'abc'+TOKEN+b'pq'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source.bin'
            source.write_bytes(raw)
            with Case(root/'case') as case:
                sid = case.register_source(source)
                scan_logical(case, sid)
                with ReaderPool(case.sources()) as pool:
                    capture_association(case, [movie_with_audio('warm', 100)], pool)
                    for failure in (OSError('synthetic source read failure'), KeyboardInterrupt()):
                        movie = movie_with_audio(type(failure).__name__, 100)
                        with patch.object(pool, 'read', side_effect=failure):
                            with self.assertRaises(type(failure)):
                                capture_association(case, [movie], pool)
                        self.assertIsNone(case.job('ordinary', movie.digest))
