"""Independent supported-sample coverage and multipart support-pruning oracles."""
import hashlib
from itertools import product
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest

from videorebuild import engine, structural_index
from videorebuild.bmff import valid_sample
from videorebuild.constraints import prune_domains
from videorebuild.storage import ReaderPool


class QuietTask:
    def update(self, *args, **kwargs):
        pass

    def heartbeat(self, **kwargs):
        pass

    def advance(self, *args, **kwargs):
        pass


class MatchingIndependentReviewTests(unittest.TestCase):
    def test_complete_index_contains_all_supported_multinal_sample_starts(self):
        rng = random.Random(9183)
        data, wanted = bytearray(), []
        for _ in range(100):
            sample = b''
            for _ in range(rng.randrange(1, 7)):
                payload = b'\x61'+b'X'*rng.randrange(5)
                sample += len(payload).to_bytes(4, 'big')+payload
            self.assertTrue(valid_sample(sample, b'avc1', 4))
            if len(sample) >= 8:
                wanted.append((len(sample)-4, len(data)))
            data.extend(sample+b'XYZ')
        # Finish exactly on a five-byte AVC NAL at the physical EOF.
        sample = bytes.fromhex('00000001610000000161')
        wanted.append((len(sample)-4, len(data)))
        data.extend(sample)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'samples.bin'
            source.write_bytes(data)
            stat = source.stat()
            row = dict(path=str(source), kind='file', size=len(data), mtime_ns=stat.st_mtime_ns,
                       ino=stat.st_ino, dev=stat.st_dev, sha256=hashlib.sha256(data).hexdigest())
            case = SimpleNamespace(root=root/'case')
            case.root.mkdir()
            with ReaderPool({1: row}) as pool:
                with structural_index.postings(case, pool, {length for length, _ in wanted},
                        engine._anchor_locations, engine.Options(), QuietTask()) as (db, _):
                    locations = set(db.execute('SELECT length,start FROM locations WHERE sid=1'))
                    self.assertTrue(set(wanted) <= locations, set(wanted)-locations)

    def test_multipart_alias_support_pruning_preserves_exhaustive_assignments(self):
        rng = random.Random(7719)
        def feasible(domains):
            results = set()
            for choices in product(*domains):
                parts = [extent for _, extents in choices for extent in extents]
                if all(a[:2] != b[:2] or a[2] >= b[3] or b[2] >= a[3]
                       for i, a in enumerate(parts) for b in parts[i+1:]):
                    results.add(tuple(key for key, _ in choices))
            return results
        for fixture in range(100):
            domains = []
            for group in range(rng.randrange(2, 5)):
                domain = []
                for candidate in range(rng.randrange(1, 4)):
                    start = rng.randrange(12)
                    inode = rng.randrange(1, 3)
                    extents = ((1, inode, start, start+2),
                               (1, inode, start+4, start+6))
                    domain.append(((group, candidate), extents))
                domains.append(domain)
            pruned, _ = prune_domains(domains)
            self.assertEqual(feasible(pruned), feasible(domains), fixture)
