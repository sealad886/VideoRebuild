"""Regression checks for bounded deep-search candidate generation."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import re
from unittest.mock import patch

from videorebuild import engine
from videorebuild.storage import ReaderPool


class DeepSearchPerformanceTests(unittest.TestCase):
    def test_zero_filled_gap_does_not_generate_nal_candidates(self):
        movie = SimpleNamespace(digest='synthetic')
        group = SimpleNamespace(key=('synthetic', 0), anchors=lambda: [(1024, 0)])
        candidates = []
        compile_pattern = re.compile

        def instrument(pattern):
            compiled = compile_pattern(pattern)
            def finditer(data):
                for match in compiled.finditer(data):
                    candidates.append(match.start())
                    yield match
            return SimpleNamespace(finditer=finditer)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'image.bin'
            source.write_bytes(bytes(65536))
            stat = source.stat()
            sources = {1: dict(path=str(source), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                               dev=stat.st_dev, ino=stat.st_ino)}
            with ReaderPool(sources) as pool, \
                    patch.object(engine, 'groups_for', return_value=[group]), \
                    patch.object(engine.re, 'compile', side_effect=instrument), \
                    patch.object(engine, 'finish'):
                engine.deep_search(SimpleNamespace(), [movie], pool, {}, engine.Options(), 'synthetic')
        self.assertEqual(candidates, [])

    def test_overlapping_nonzero_length_anchors_remain_visible(self):
        # At offsets 0 and 1: valid first NAL lengths 1 and 257 respectively.
        # A consuming regex would skip the second possible sample start.
        data = bytes.fromhex('000000010101') + bytes(257)
        movie = SimpleNamespace(digest='synthetic')
        group = SimpleNamespace(key=('synthetic', 0), span=262, chunks=[], movie=movie,
                                anchors=lambda: [(1, 0), (257, 0)],
                                mapping=lambda sid, start: {(1, 0): (sid, start)})
        starts = []
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'image.bin'
            source.write_bytes(data)
            stat = source.stat()
            sources = {1: dict(path=str(source), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                               dev=stat.st_dev, ino=stat.st_ino)}
            def fingerprint(_group, mapping, _pool):
                start = mapping[(1, 0)][1]
                starts.append(start)
                return str(start)
            with ReaderPool(sources) as pool, \
                    patch.object(engine, 'groups_for', return_value=[group]), \
                    patch.object(engine, 'check_samples', return_value=True), \
                    patch.object(engine, '_group_hash', side_effect=fingerprint), \
                    patch.object(engine, 'attempt', return_value={'status': 'valid'}), \
                    patch.object(engine, 'finish'):
                engine.deep_search(SimpleNamespace(), [movie], pool, {}, engine.Options(), 'synthetic')
        self.assertEqual(starts, [0, 1])


class SampleValidationPerformanceTests(unittest.TestCase):
    def fixture(self, directory, sizes):
        import struct
        from videorebuild.bmff import Chunk
        source = Path(directory) / 'samples.bin'
        source.write_bytes(b''.join(struct.pack('>I', n-4) + b'\x65' + b'X'*(n-5)
                                   for n in sizes))
        stat = source.stat()
        sources = {1: dict(path=str(source), size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                           dev=stat.st_dev, ino=stat.st_ino)}
        movie = SimpleNamespace(chunks=[Chunk(0, 0, 0, tuple(sizes), 0)],
                                tracks=[SimpleNamespace(handler=b'vide', codec=b'avc1', nal_length=4)])
        return source, movie, ReaderPool(sources)

    def test_full_validation_reads_bounded_windows(self):
        # Accuracy still covers all samples; I/O grows with bytes, not sample count.
        with tempfile.TemporaryDirectory() as directory:
            source, movie, pool = self.fixture(directory, [1024]*8193)
            with pool, patch.object(pool, 'read', wraps=pool.read) as read:
                self.assertTrue(engine.check_samples(movie, {(0, 0): (1, 0)}, pool))
                self.assertLessEqual(read.call_count, 3)
                self.assertTrue(all(call.args[2] <= 4*1024*1024 for call in read.call_args_list))
                self.assertEqual(sum(call.args[2] for call in read.call_args_list), source.stat().st_size)

    def test_invalid_unscreened_middle_sample_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source, movie, pool = self.fixture(directory, [1024]*8193)
            # Index 5000 is neither quick-screen midpoint nor endpoint.
            raw = bytearray(source.read_bytes())
            raw[5000*1024+4] = 0xe5
            source.write_bytes(raw)
            stat = source.stat()
            pool.sources[1]['mtime_ns'] = stat.st_mtime_ns
            with pool:
                self.assertTrue(engine.check_samples(movie, {(0, 0): (1, 0)}, pool, quick=True))
                self.assertFalse(engine.check_samples(movie, {(0, 0): (1, 0)}, pool))

    def test_sample_crossing_window_boundary_validates_completely(self):
        with tempfile.TemporaryDirectory() as directory:
            _, movie, pool = self.fixture(directory, [1023]*4102)
            with pool, patch.object(pool, 'read', wraps=pool.read) as read:
                self.assertTrue(engine.check_samples(movie, {(0, 0): (1, 0)}, pool))
                self.assertGreater(read.call_count, 1)
                self.assertLessEqual(read.call_count, 2)

    def test_source_mutation_during_cached_validation_detected(self):
        from videorebuild.storage import SafetyError
        with tempfile.TemporaryDirectory() as directory:
            source, movie, pool = self.fixture(directory, [1024]*20)
            validate_sample = engine.valid_sample
            changed = False
            def mutate_after_read(raw, codec, length):
                nonlocal changed
                result = validate_sample(raw, codec, length)
                if not changed:
                    changed = True
                    # Deterministic identity change, independent of timestamp resolution.
                    with source.open('ab') as handle:
                        handle.write(b'X')
                return result
            with pool, patch.object(engine, 'valid_sample', side_effect=mutate_after_read):
                with self.assertRaises(SafetyError):
                    engine.check_samples(movie, {(0, 0): (1, 0)}, pool)

    def test_quick_validation_retains_sparse_sample_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            _, movie, pool = self.fixture(directory, [1024]*8193)
            with pool, patch.object(pool, 'read', wraps=pool.read) as read:
                self.assertTrue(engine.check_samples(movie, {(0, 0): (1, 0)}, pool, quick=True))
                self.assertEqual(read.call_count, 6)
                self.assertTrue(all(call.args[2] == 1024 for call in read.call_args_list))

    def test_oversized_sample_and_final_window_clipped_to_chunk_end(self):
        # One legal sample exceeds the normal window; following tail remains exact.
        sizes = [4*1024*1024+37, 1024]
        with tempfile.TemporaryDirectory() as directory:
            source, movie, pool = self.fixture(directory, sizes)
            with pool, patch.object(pool, 'read', wraps=pool.read) as read:
                self.assertTrue(engine.check_samples(movie, {(0, 0): (1, 0)}, pool))
                self.assertEqual([call.args[2] for call in read.call_args_list], sizes)
                self.assertTrue(all(call.args[1]+call.args[2] <= source.stat().st_size
                                    for call in read.call_args_list))
