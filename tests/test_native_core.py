"""Differential/property/fuzz checks against frozen 0.5.0 recovery semantics."""
from itertools import product
import random
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from videorebuild import _native, bmff, engine, fragmented
from videorebuild.constraints import prune_domains
from oracles import python_core as oracle


class Task:
    def heartbeat(self, **_):
        pass

    def update(self, *_, **__):
        pass


class BufferPool:
    """Synthetic scope-aware reader, including disjoint availability spans."""
    def __init__(self, data, ranges=None, block=37):
        self.data = data
        self.sources = {1: {'size': len(data)}}
        self.ranges = ranges if ranges is not None else [(0, len(data))]
        self.block = block
        self.checks = 0

    def check(self, sid):
        self.assert_sid(sid)
        self.checks += 1

    def assert_sid(self, sid):
        if sid != 1:
            raise AssertionError('unexpected source')

    def read(self, sid, start, size):
        self.assert_sid(sid)
        if not any(lo <= start and start+size <= lo+count for lo, count in self.ranges):
            raise bmff.FormatError('unavailable synthetic range')
        return self.data[start:start+size]

    def iter_blocks(self, sid, overlap=16):
        for lo, count in self.ranges:
            for at in range(lo, lo+count, self.block):
                size = min(self.block+overlap, lo+count-at)
                yield at, self.read(sid, at, size), min(self.block, size)


def scan(function, pool, maximum, wanted, budget=None):
    state = dict(nal_checks=0, anchor_checks=0, limited=False, stop_reason=None)
    result = list(function(pool, maximum, wanted, engine.Options(max_anchor_checks=budget), state, Task()))
    return result, state


def assignments(domains):
    result = set()
    for selected in product(*domains):
        parts = [extent for _, extents in selected for extent in extents if extent[3] > extent[2]]
        if all(a[:2] != b[:2] or a[3] <= b[2] or b[3] <= a[2]
               for index, a in enumerate(parts) for b in parts[index+1:]):
            result.add(tuple(key for key, _ in selected))
    return result


class NativeCoreTests(unittest.TestCase):
    def test_fragmented_mutable_buffers_preserve_sample_bytes_and_bounded_copy(self):
        sample = b'\0\0\0\1\x65\0\0\0\2\x09\xf0'
        original = bytearray(b'P'*65536+sample+b'S'*65536)
        track = SimpleNamespace(handler='vide', codec='avc1', nal_length=4)
        native_parse = _native.fragment_nal_types
        for data in (original, memoryview(original), memoryview(bytes(original))):
            with self.subTest(type=type(data).__name__), patch.object(
                    _native, 'fragment_nal_types', wraps=native_parse) as parse:
                self.assertEqual(fragmented.nal_types(data, 65536, len(sample), track), (5, 9))
                # Immutable handoff is necessary before native code releases the
                # GIL, and copying is bounded by the sample rather than the movie.
                handed_off = parse.call_args.args[0]
                self.assertIsInstance(handed_off, bytes)
                self.assertEqual(handed_off, sample)
                self.assertEqual(bytes(data), bytes(original))

    def test_sample_mutation_fuzz_matches_released_oracle(self):
        """12,000 deterministic mutations cover complete framing and SEI envelopes."""
        rng = random.Random(0x5059434f5245)
        for index in range(12_000):
            width = (1, 2, 4)[index % 3]
            codec = (b'avc1', b'avc3', b'hvc1', b'hev1')[index % 4]
            hevc = codec.startswith((b'hvc', b'hev'))
            nals = []
            for _ in range(rng.randrange(1, 7)):
                nal = bytes([rng.choice([0x02, 0x26, 0x40]), 1]) if hevc else bytes([rng.choice([1, 5, 6, 9, 23])])
                nal += rng.randbytes(rng.randrange(0, 20))
                nals.append(len(nal).to_bytes(width, 'big')+nal)
            raw = bytearray(b''.join(nals))
            operation = index % 5
            if operation == 0:
                raw = raw[:rng.randrange(len(raw)+1)]
            elif operation in (1, 2):
                raw[rng.randrange(len(raw))] ^= rng.randrange(1, 256)
            elif operation == 3:
                raw += rng.randbytes(rng.randrange(1, 5))
            raw = bytes(raw)
            expected = oracle.sample_kind(raw, codec, width)
            self.assertEqual(bmff.sample_kind(raw, codec, width), expected, (index, raw.hex()))
            track = SimpleNamespace(handler='vide', codec=codec.decode(), nal_length=width)
            try:
                expected_types = oracle.fragment_nal_types(raw, 0, len(raw), track)
            except bmff.FormatError as error:
                with self.assertRaisesRegex(bmff.FormatError, str(error)):
                    fragmented.nal_types(raw, 0, len(raw), track)
            else:
                self.assertEqual(fragmented.nal_types(raw, 0, len(raw), track), expected_types)

    def test_resumable_scanning_matches_locations_counters_and_limits(self):
        rng = random.Random(0x4e414c)
        for index in range(600):
            raw = bytearray(rng.randbytes(rng.randrange(5, 350)))
            for _ in range(rng.randrange(1, 14)):
                start = rng.randrange(len(raw)-4)
                nal = rng.choice([b'\x65', b'\x09\xf0', b'\x26\x01', b'\xe5'])
                sample = len(nal).to_bytes(4, 'big')+nal
                raw[start:start+len(sample)] = sample
            maximum = rng.randrange(6, 150)
            budget = None if index % 3 else rng.randrange(1, 50)
            ranges = None if index % 4 else [(0, len(raw)//2), (len(raw)//2+3, len(raw)-len(raw)//2-3)]
            wanted = lambda length: length % 3 != 0
            before, stats = scan(oracle._anchor_locations, BufferPool(bytes(raw), ranges), maximum, wanted, budget)
            after, current = scan(engine._anchor_locations, BufferPool(bytes(raw), ranges), maximum, wanted, budget)
            self.assertEqual(after, before, index)
            for key in ('nal_checks', 'limited', 'stop_reason', 'discovery_header_reads', 'discovery_header_cache_hits'):
                self.assertEqual(current.get(key, 0), stats.get(key, 0), (index, key))

    def test_filtered_native_scanner_preserves_unaligned_multinal_starts(self):
        raw = b'X'+b'\0\0\0\1\x65'*80+b'\0\0\0\2\x26\x01'
        bits = bytes([0b01000010, 0b00001000, 0b00000001])
        def wanted(length):
            return length//8 < len(bits) and bool(bits[length//8] & (1 << (length % 8)))
        expected, _ = scan(oracle._anchor_locations, BufferPool(raw, block=19), 24, wanted)
        wanted.native_bits = bits
        actual, _ = scan(engine._anchor_locations, BufferPool(raw, block=19), 24, wanted)
        self.assertEqual(actual, expected)

    def test_constraint_fuzz_preserves_every_exhaustive_assignment(self):
        rng = random.Random(0x434f4e5354)
        for index in range(1200):
            domains = []
            for group in range(rng.randrange(2, 5)):
                domain = []
                for candidate in range(rng.randrange(1, 5)):
                    extents = []
                    for _ in range(rng.randrange(1, 4)):
                        lo = rng.randrange(10)
                        extents.append((rng.randrange(2), rng.randrange(2), lo, lo+rng.randrange(4)))
                    domain.append(((group, candidate), extents))
                domains.append(domain)
            budgets = dict(max_edges=rng.choice([0, 1, 20, 200_000]), max_checks=rng.choice([0, 1, 30, 800_000]))
            before, expected = oracle.prune_domains(domains, **budgets)
            after, actual = prune_domains(domains, **budgets)
            self.assertEqual(after, before, index)
            self.assertEqual(actual, expected, index)
            self.assertEqual(assignments(domains), assignments(after), index)

    def test_integer_bounds_and_cursor_errors_fail_without_panics(self):
        for arguments in ((2**64-1, 1, 2**64-1, 32), (0, 2, 1, 32), (0, 0, 0, 3)):
            with self.assertRaises(ValueError):
                _native.NalScanner(*arguments)
        for value in (-1, 2**64):
            with self.assertRaises(OverflowError):
                _native.NalScanner(value, 0, 0, 32)
        scanner = _native.NalScanner(2**64-9, 8, 2**64-1, 2**64-1)
        self.assertTrue(scanner.scan(b'\0\0\0\xff\x65\0\0\0')[5])
        with self.assertRaises(ValueError):
            scanner.supply_header(1, True)
        with self.assertRaises(ValueError):
            _native.NalScanner(0, 5, 5, 32).scan(b'1234')
        for width in (1, 2, 4):
            validator = _native.SampleValidator([8], b'avc1', width)
            self.assertEqual(validator.validate_window(b'1234'), (0, 0, True))

    def test_point_chain_and_headers_share_native_scanner_rules(self):
        from videorebuild import nal_search
        data = b'\0\0\0\1\x65\0\0\0\2\x26\x01'
        def read(offset, count):
            if offset < 0 or offset+count > len(data):
                raise bmff.FormatError('outside synthetic source')
            return data[offset:offset+count]
        for start in (*range(-1, len(data)+1), 2**64, 2**128):
            for maximum in range(4, 20):
                get_header = lambda at: oracle.header(read, at)
                self.assertEqual(list(nal_search.cumulative_lengths(get_header, start, len(data), maximum)),
                                 list(oracle.cumulative_lengths(get_header, start, len(data), maximum)))


if __name__ == '__main__':
    unittest.main()
