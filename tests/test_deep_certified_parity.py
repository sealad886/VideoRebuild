"""Framing reuse must preserve deep recovery bytes and physical witnesses."""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_deep_witnesses import structural_movie
from videorebuild import engine, structural_index
from videorebuild.candidate_tiers import CandidateTiers
from videorebuild.progress import phase
from videorebuild.storage import Case, ReaderPool, scan_logical


class DeepCertifiedParityTests(unittest.TestCase):
    def snapshot(self, case, movie):
        job = case.job('ordinary', movie.digest)
        detail = job['detail']
        candidates = {}
        for candidate in detail['candidates']:
            path = case.root / candidate['path']
            manifest = json.loads(path.with_suffix('.json').read_text())
            witnesses = tuple(
                tuple((extent['source_id'], extent['source_offset'], extent['length'])
                      for extent in chunk['physical_extents'])
                for chunk in manifest['provenance']['chunks']
            )
            candidates[candidate['media_sha256']] = (path.read_bytes(), witnesses)
        outputs = tuple((case.root / path).read_bytes() for path in detail['outputs'])
        return job['status'], detail['search_limited'], candidates, outputs

    def test_indexed_framing_reuse_preserves_witnesses_and_ambiguity(self):
        token = b'\0\0\0\x04\x65ABC'
        other = b'\0\0\0\x04\x65DEF'
        # The HEVC-like header nominates this same-size sample. AVC rejects it.
        decoy = b'\0\0\0\x04\x40\x01ZZ'
        gap = b'Z' * len(token)
        movie = structural_movie([100, 200], [len(token), len(token)])
        opt = engine.Options(deep=True, reconstruct_only=True)
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'tokens.bin'
                data = decoy + gap + token + gap + token
                if ambiguous:
                    data += gap + other
                source.write_bytes(data)
                snapshots = []
                for reference in (True, False):
                    path = root / ('reference' if reference else 'certified')
                    with Case(path) as case:
                        sid = case.register_source(source)
                        scan_logical(case, sid)
                        with ReaderPool(case.sources()) as pool:
                            if reference:
                                with phase('Fixture structural index') as task, structural_index.postings(
                                        case, pool, [len(token)-4], engine._anchor_locations, opt, task) as (db, _):
                                    posting = db.execute('SELECT 1 FROM locations WHERE length=? AND sid=? AND start=0',
                                                         (len(token)-4, sid)).fetchone()
                                    self.assertIsNotNone(posting)
                            compare = (patch.object(engine.Group, 'sample_profile', return_value=None)
                                       if reference else nullcontext())
                            with compare:
                                engine.deep_search(case, [movie], pool, {}, opt, 'parity')
                        snapshots.append(self.snapshot(case, movie))

                self.assertEqual(snapshots[0], snapshots[1])
                status, limited, candidates, outputs = snapshots[1]
                self.assertFalse(limited)
                payloads = [token + token]
                if ambiguous:
                    payloads.extend((token + other, other + token))
                self.assertEqual(set(candidates), {hashlib.sha256(payload).hexdigest() for payload in payloads})
                self.assertEqual(status, 'ambiguous' if ambiguous else 'reconstructed')
                self.assertEqual(len(outputs), 0 if ambiguous else 1)
                for _, witnesses in candidates.values():
                    self.assertEqual(len(witnesses), 2)
                    offsets = {offset for group in witnesses for _, offset, _ in group}
                    self.assertEqual(len(offsets), 2)
                    self.assertNotIn(0, offsets)
                self.assertEqual(source.read_bytes(), data)

    def test_pruning_one_movie_preserves_shared_witnesses_for_another(self):
        token = b'\0\0\0\x04\x65ABC'
        large = b'\0\0\0\x0c\x65XYZ' + token
        gap = b'Z' * 8
        first = structural_movie([100, 200], [len(large), len(token)])
        second = structural_movie([100, 200], [len(token), len(token)])
        opt = engine.Options(deep=True, reconstruct_only=True)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'tokens.bin'
            source.write_bytes(large + gap + token)
            snapshots = []
            for reference in (True, False):
                path = root / ('reference' if reference else 'certified')
                with Case(path) as case:
                    sid = case.register_source(source)
                    scan_logical(case, sid)
                    compare = (patch.object(engine.Group, 'sample_profile', return_value=None)
                               if reference else nullcontext())
                    with ReaderPool(case.sources()) as pool, compare:
                        engine.deep_search(case, [first, second], pool, {}, opt, 'shared')
                    first_detail = case.job('ordinary', first.digest)['detail']
                    # The nested token overlaps the required large sample.
                    self.assertGreater(first_detail['constraint_removed'], 0)
                    if not reference:
                        self.assertGreater(first_detail['shared_extent_domains'], 0)
                    snapshots.append((self.snapshot(case, first), self.snapshot(case, second)))

            self.assertEqual(snapshots[0], snapshots[1])
            for (status, limited, candidates, outputs), payload, offsets in zip(
                    snapshots[1], (large + token, token + token), ({0, 24}, {8, 24})):
                self.assertEqual((status, limited, len(outputs)), ('reconstructed', False, 1))
                digest = hashlib.sha256(payload).hexdigest()
                self.assertEqual(set(candidates), {digest})
                _, witnesses = candidates[digest]
                self.assertEqual({offset for group in witnesses for _, offset, _ in group}, offsets)

    def test_framing_cache_does_not_share_owner_eligibility(self):
        token = b'\0\0\0\x04\x65ABC'
        accepted = structural_movie([100], [len(token)])
        other = structural_movie([200], [len(token)])
        opt = engine.Options(deep=True, reconstruct_only=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'token.bin'
            source.write_bytes(token)
            with Case(root / 'case') as case:
                sid = case.register_source(source)
                scan_logical(case, sid)
                with ReaderPool(case.sources()) as pool:
                    with patch.object(engine.Group, 'sample_profile', return_value=None):
                        engine.deep_search(case, [accepted], pool, {}, opt, 'owner')
                    original = self.snapshot(case, accepted)
                    self.assertEqual(original[0], 'reconstructed')

                    tiers = CandidateTiers(case, pool)
                    engine.deep_search(case, [other], tiers.view(), {}, opt, 'owner')
                    self.assertEqual(self.snapshot(case, other), ('unresolved', False, {}, ()))

                    owner = tiers.view(('ordinary', accepted.digest))
                    engine.deep_search(case, [accepted], owner, {}, opt, 'owner')
                    self.assertEqual(self.snapshot(case, accepted), original)
