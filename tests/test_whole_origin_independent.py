"""Independent all-pairs oracle for cold whole-payload origin nominations."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from test_deep_witnesses import structural_movie
from videorebuild import engine
from videorebuild.bmff import valid_sample
from videorebuild.storage import Case, ReaderPool, SafetyError


FIVE = bytes.fromhex('0000000161')
EIGHT = bytes.fromhex('0000000465414243')
MULTI = bytes.fromhex('00000001610000000161')


class WholeOriginIndependentTests(unittest.TestCase):
    def profile(self, movie):
        for chunk in movie.chunks:
            track = movie.tracks[chunk.track]
            relative = chunk.offset-movie.chunks[0].offset
            for size in chunk.sizes:
                if track.handler == b'vide' and track.nal_length == 4 and 8 <= size < 16*1024*1024:
                    return relative, size-4
                relative += size
        return None

    def movies(self):
        return [structural_movie(offsets, sizes) for offsets, sizes in (
            ([100, 105], [5, 10]), ([200, 205], [5, 8]),
            ([100, 108], [8, 10]), ([100, 112], [5, 10]), ([100], [5]))]

    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Case(root/'case') as case:
                origins = []
                payloads = [FIVE+MULTI, FIVE+EIGHT, EIGHT+MULTI,
                            FIVE+b'gapbyte'+MULTI, b'BAD!!'+MULTI,
                            EIGHT+bytes(10), FIVE]
                for number, bodies in enumerate((payloads, list(reversed(payloads[:4])))):
                    data = bytearray(b'xyz')
                    spans = []
                    for index, payload in enumerate(bodies):
                        extended = index % 2
                        header = 16 if extended else 8
                        offset = len(data)
                        prefix = (struct.pack('>I4sQ', 1, b'mdat', header+len(payload)) if extended else
                                  struct.pack('>I4s', header+len(payload), b'mdat'))
                        data.extend(prefix+payload+b'Q')
                        spans.append((offset, header, len(payload)))
                    path = root/f'source-{number}.bin'
                    path.write_bytes(data)
                    sid = case.register_source(path, 'image')
                    case.db.execute('UPDATE sources SET complete=1 WHERE id=?', (sid,))
                    for offset, header, size in spans:
                        case.db.execute('''INSERT INTO atoms(source_id,kind,offset,size,header,available,payload_size)
                                           VALUES(?,'mdat',?,?,?,?,?)''',
                                        (sid, offset, header+size, header, header+size, size))
                        origins.append((sid, offset, header, size))
                case.db.commit()
                yield case, origins

    def oracle(self, movie, pool, origins, options):
        """Try every origin without structural-index or engine matching helpers."""
        successful, considered = set(), []
        for sid, offset, header, size in sorted(origins, key=lambda item: (item[3], item[0], item[1])):
            if size < movie.span or options.max_padding is not None and size > movie.span+options.max_padding:
                continue
            if not pool.permits(sid, offset, header):
                continue
            translation = offset+header-movie.chunks[0].offset
            lower = min(chunk.offset for chunk in movie.chunks)+translation
            upper = max(chunk.offset+chunk.size for chunk in movie.chunks)+translation
            if getattr(pool, 'tiers', None) and not pool.candidate_permits(sid, lower, upper-lower):
                continue
            if options.max_candidates is not None and len(considered) >= options.max_candidates:
                break
            considered.append((sid, translation))
            encoded = bytearray()
            supported = True
            for chunk in movie.chunks:
                at = translation+chunk.offset
                if not pool.permits(sid, at, chunk.size):
                    supported = False
                    break
                raw = pool.read(sid, at, chunk.size)
                track = movie.tracks[chunk.track]
                cursor = 0
                for length in chunk.sizes:
                    sample = raw[cursor:cursor+length]
                    if track.handler == b'vide' and not valid_sample(sample, track.codec, track.nal_length):
                        supported = False
                        break
                    cursor += length
                if not supported:
                    break
                encoded.extend(raw)
            if supported:
                successful.add(hashlib.sha256(encoded).hexdigest())
        return successful, considered

    def whole_results(self, case, movie, pool, options):
        observed = []
        original = engine.attempt
        def attempt(case, movie, mapping, *args, **kwargs):
            sid, first = mapping[(movie.chunks[0].track, movie.chunks[0].index)]
            observed.append((sid, first-movie.chunks[0].offset))
            return original(case, movie, mapping, *args, **kwargs)
        with patch.object(engine, 'attempt', side_effect=attempt):
            _, candidates, limited = engine.whole_payload(case, movie, pool, {}, options, 'independent-origin')
        return {candidate['media_sha256'] for candidate in candidates}, observed, limited

    def test_first_anchor_origins_preserve_all_pairwise_valid_encoded_outputs(self):
        from videorebuild.origin_index import OriginIndex
        movies = self.movies()
        profiles = [self.profile(movie) for movie in movies if self.profile(movie) is not None]
        self.assertEqual({relative for relative, _ in profiles}, {0, 5, 12})
        with self.fixture() as (case, origins), ReaderPool(case.sources()) as pool:
            with OriginIndex(case, pool) as index:
                index.prepare(profiles)
                pool.origin_index = index
                for movie in movies:
                    with self.subTest(offsets=[chunk.offset for chunk in movie.chunks]):
                        expected, all_pairs = self.oracle(movie, pool, origins, engine.Options(reconstruct_only=True))
                        actual, tested, limited = self.whole_results(case, movie, pool, engine.Options(reconstruct_only=True))
                        self.assertEqual(actual, expected)
                        self.assertFalse(limited)
                        ranks = {candidate: rank for rank, candidate in enumerate(all_pairs)}
                        self.assertTrue(set(tested) <= set(all_pairs))
                        self.assertEqual(tested, sorted(tested, key=ranks.__getitem__))
                        profile = self.profile(movie)
                        if profile is None:
                            self.assertEqual(tested, all_pairs)
                            continue
                        nominated = list(index.candidates(*profile))
                        ordered = sorted(nominated, key=lambda item: (item[3], item[0], item[1]))
                        self.assertEqual(nominated, ordered)
                        self.assertEqual(len(nominated), len(set(nominated)))
                        self.assertTrue(set(nominated) <= set(origins))
                        nominated_outputs, _ = self.oracle(movie, pool, nominated, engine.Options(reconstruct_only=True))
                        self.assertEqual(nominated_outputs, expected)
                        if profile == (0, 4):
                            self.assertIn(origins[5], nominated)  # First anchor fits; second sample is invalid.
                            rejected, _ = self.oracle(movie, pool, [origins[5]], engine.Options(reconstruct_only=True))
                            self.assertFalse(rejected)

    def test_scope_holes_cannot_become_valid_through_origin_nomination(self):
        from videorebuild.origin_index import OriginIndex
        from videorebuild.ranges import AllocationScope
        movies = self.movies()
        profiles = [self.profile(movie) for movie in movies if self.profile(movie) is not None]
        with self.fixture() as (case, origins):
            rows = case.sources()
            denied = origins[0]
            sid, offset, header, size = denied
            # Keep a later anchor readable, but exclude the first sample. The
            # origin nomination remains only a necessary structural predicate.
            scope = AllocationScope({source: ([(0, offset+header), (offset+header+5, row['size']-(offset+header+5))]
                                              if source == sid else [(0, row['size'])])
                                     for source, row in rows.items()}, 'independent-first-sample-hole')
            with ReaderPool(rows, allocation_scope=scope) as pool, OriginIndex(case, pool) as index:
                index.prepare(profiles)
                pool.origin_index = index
                for movie in movies:
                    profile = self.profile(movie)
                    if profile is None:
                        continue
                    expected, _ = self.oracle(movie, pool, origins, engine.Options(reconstruct_only=True))
                    nominated = list(index.candidates(*profile))
                    actual, _ = self.oracle(movie, pool, nominated, engine.Options(reconstruct_only=True))
                    self.assertEqual(actual, expected)
                    recovered, _, _ = self.whole_results(case, movie, pool, engine.Options(reconstruct_only=True))
                    self.assertEqual(recovered, expected)

    def test_one_byte_nals_at_permitted_body_boundaries_keep_required_postings(self):
        from videorebuild.origin_index import OriginIndex
        from videorebuild.ranges import AllocationScope
        with self.fixture() as (case, origins):
            rows = case.sources()
            ranges = {}
            for sid, row in rows.items():
                cursor, allowed = 0, []
                for _, offset, header, size in sorted(item for item in origins if item[0] == sid):
                    end = offset+header+size
                    allowed.append((cursor, end-cursor))
                    cursor = end+1  # The separator after each body is unavailable.
                if cursor < row['size']:
                    allowed.append((cursor, row['size']-cursor))
                ranges[sid] = allowed
            scope = AllocationScope(ranges, 'independent-body-boundaries')
            movie = self.movies()[0]
            profile = self.profile(movie)
            with ReaderPool(rows, allocation_scope=scope) as pool, OriginIndex(case, pool) as index:
                index.prepare([profile])
                pool.origin_index = index
                expected, _ = self.oracle(movie, pool, origins, engine.Options(reconstruct_only=True))
                nominated = list(index.candidates(*profile))
                actual, _ = self.oracle(movie, pool, nominated, engine.Options(reconstruct_only=True))
                self.assertTrue(expected)
                self.assertEqual(actual, expected)
                recovered, _, _ = self.whole_results(case, movie, pool, engine.Options(reconstruct_only=True))
                self.assertEqual(recovered, expected)

    def test_base_origin_postings_preserve_owner_tier_exemption(self):
        from videorebuild.origin_index import OriginIndex
        from videorebuild.candidate_tiers import CandidateTiers
        from test_candidate_tiers import CandidateTierTests
        with self.fixture() as (case, origins), ReaderPool(case.sources()) as base:
            sid, offset, header, size = origins[0]
            CandidateTierTests.publish(self, case, base, sid, offset+header, size, key='accepted-owner')
            tiers = CandidateTiers(case, base)
            movie = self.movies()[0]
            profile = self.profile(movie)
            with OriginIndex(case, base) as index:
                index.prepare([profile])
                base.origin_index = index
                nominated = list(index.candidates(*profile))
                counts = []
                for owner in (None, ('ordinary', 'accepted-owner')):
                    view = tiers.view(owner)
                    expected, considered = self.oracle(movie, view, origins, engine.Options(reconstruct_only=True))
                    actual, _ = self.oracle(movie, view, nominated, engine.Options(reconstruct_only=True))
                    self.assertEqual(actual, expected)
                    recovered, _, _ = self.whole_results(case, movie, view, engine.Options(reconstruct_only=True))
                    self.assertEqual(recovered, expected)
                    counts.append(len(considered))
                self.assertGreater(counts[1], counts[0])

    def test_explicit_candidate_cap_and_unsupported_template_keep_all_pairs_order(self):
        from videorebuild.origin_index import OriginIndex
        movies = self.movies()
        with self.fixture() as (case, origins), ReaderPool(case.sources()) as pool, OriginIndex(case, pool) as index:
            index.prepare([self.profile(movie) for movie in movies if self.profile(movie) is not None])
            pool.origin_index = index
            for movie, options in ((movies[0], engine.Options(reconstruct_only=True, max_candidates=1)),
                                   (movies[-1], engine.Options(reconstruct_only=True))):
                with self.subTest(capped=options.max_candidates):
                    expected, considered = self.oracle(movie, pool, origins, options)
                    with patch.object(index, 'candidates', side_effect=AssertionError('fallback must not use origin postings')):
                        actual, tested, limited = self.whole_results(case, movie, pool, options)
                    self.assertEqual(actual, expected)
                    self.assertEqual(tested, considered)
                    self.assertEqual(limited, options.max_candidates is not None)

    def test_padding_limit_keeps_all_pairwise_results_and_incompleteness(self):
        from videorebuild.origin_index import OriginIndex
        movies = self.movies()
        with self.fixture() as (case, origins), ReaderPool(case.sources()) as pool, OriginIndex(case, pool) as index:
            index.prepare([self.profile(movie) for movie in movies if self.profile(movie) is not None])
            pool.origin_index = index
            options = engine.Options(reconstruct_only=True, max_padding=0)
            for movie in movies:
                with self.subTest(span=movie.span):
                    expected, considered = self.oracle(movie, pool, origins, options)
                    actual, tested, limited = self.whole_results(case, movie, pool, options)
                    self.assertEqual(actual, expected)
                    self.assertTrue(set(tested) <= set(considered))
                    self.assertEqual(limited, any(size > movie.span for _, _, _, size in origins))

    def test_warm_negative_origin_coverage_still_checks_source_before_completion(self):
        from videorebuild.origin_index import OriginIndex
        with self.fixture() as (case, origins), ReaderPool(case.sources()) as pool:
            with OriginIndex(case, pool) as index:
                index.prepare([(0, 9999)])
                self.assertFalse(list(index.candidates(0, 9999)))
            with self.assertRaises(SafetyError):
                with OriginIndex(case, pool) as index:
                    index.prepare([(0, 9999)])
                    self.assertFalse(list(index.candidates(0, 9999)))
                    path = Path(pool.sources[origins[0][0]]['path'])
                    changed = bytearray(path.read_bytes())
                    changed[-1] ^= 1
                    path.write_bytes(changed)
