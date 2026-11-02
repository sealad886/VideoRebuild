"""Incremental walking preserves canonical bytes, clocks, stops and scope gates."""
from dataclasses import replace
import struct
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fragmented_helpers import single_fragment
from test_fragmented_performance import with_audio
from test_fragmented_reconstruct_only import structural_movie
from videorebuild.bmff import FormatError, make_box
from videorebuild.fragmented import (FragmentCache, parse_initialization,
                                    walk_prefix, walk_prefix_reader)


class IncrementalFragmentedTests(unittest.TestCase):
    def setUp(self):
        self.initialization, self.movie = structural_movie()
        self.init = parse_initialization(self.initialization)
        self.pair = self.movie[len(self.initialization):]

    def pair_for(self, initialization, track_id=1, sequence=1, timing=0):
        track = parse_initialization(initialization).tracks[track_id]
        payload = b'\0\0\0\2\x65X' if track.handler == 'vide' else b'\x11\x22'
        sample = SimpleNamespace(duration=100, flags=0, size=len(payload))
        return single_fragment(sequence, track, sample, payload, tfdt=timing)

    def compare(self, payload, *, initialization=None, later=False, cache=None, first_pair=b''):
        initialization = self.initialization if initialization is None else initialization
        init = parse_initialization(initialization)
        requests = []
        def read(offset, size):
            self.assertGreaterEqual(offset, 0)
            self.assertGreaterEqual(size, 0)
            self.assertLessEqual(offset+size, len(payload))
            requests.append((offset, size))
            return payload[offset:offset+size]
        actual, prefix = walk_prefix_reader(init, read, len(payload), allow_later_start=later,
                                          cache=cache, first_pair=first_pair)
        expected = walk_prefix(initialization+payload, len(initialization), allow_later_start=later)
        self.assertEqual(prefix, expected)
        self.assertEqual(actual, (initialization+payload)[:expected.end])
        return prefix, requests

    def test_malformed_boundaries_and_all_truncations_match_canonical_walker(self):
        for end in range(len(self.pair)+1):
            with self.subTest(end=end):
                self.compare(self.pair[:end])
        for tail in (b'x', b'\0'*100, make_box(b'junk', b'bad'),
                     struct.pack('>I4s', 1, b'free'),
                     struct.pack('>I4sQ', 1, b'free', 17),
                     make_box(b'moof', b'bad')+make_box(b'mdat', b'bad')):
            with self.subTest(tail=tail[:16]):
                self.compare(self.pair+tail)
        bad_child = make_box(b'moof', struct.pack('>I4s', 1000, b'mfhd'))+make_box(b'mdat', b'bad')
        self.compare(self.pair+make_box(b'free', b'skipped')+bad_child)

    def test_allowed_free_skip_and_terminal_boxes_preserve_exact_prefix_bytes(self):
        second = self.pair_for(self.initialization, sequence=2, timing=100)
        for tail in (b'', make_box(b'mfra', b''), make_box(b'ftyp', b'isom')):
            with self.subTest(tail=tail):
                self.compare(self.pair+make_box(b'free', b'ABCD')+
                             make_box(b'skip', b'EFGH')+second+tail)
        self.compare(self.pair+make_box(b'free', b'not part of output'))

    def test_early_stop_never_reads_large_window_tail(self):
        payload = self.pair+make_box(b'junk', b'x'*(128*1024))
        prefix, requests = self.compare(payload)
        self.assertEqual(prefix.end, len(self.movie))
        self.assertLess(sum(size for _, size in requests), len(self.pair)+64)
        self.assertFalse(any(offset+size > len(self.pair)+16 for offset, size in requests))

    def test_unpublished_free_skip_payloads_are_never_read(self):
        for kind in (b'free', b'skip'):
            for tail in (b'', make_box(b'mfra', b''), make_box(b'junk', b'bad'),
                         make_box(b'moof', b'bad')+make_box(b'mdat', b'bad')):
                with self.subTest(kind=kind, tail=tail):
                    payload = self.pair+make_box(kind, b'x'*(128*1024))+tail
                    prefix, requests = self.compare(payload)
                    self.assertEqual(prefix.end, len(self.movie))
                    self.assertLess(sum(size for _, size in requests), len(self.pair)+128)

    def test_sparse_skipped_window_before_malformed_pair_keeps_memory_bounded(self):
        gap_size = 128*1024*1024
        malformed = make_box(b'moof', b'bad')+make_box(b'mdat', b'bad')
        free_header = struct.pack('>I4s', gap_size, b'free')
        requests = []
        def read(offset, size):
            requests.append((offset, size))
            self.assertLessEqual(size, len(malformed))
            if offset < gap_size:
                self.assertEqual(offset, 0)
                return free_header+b'\0'*(size-len(free_header))
            return malformed[offset-gap_size:offset-gap_size+size]
        tracemalloc.start()
        try:
            output, prefix = walk_prefix_reader(self.init, read, gap_size+len(malformed))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1024*1024)
        self.assertEqual(output, self.initialization)
        self.assertEqual(prefix.stop_offset, len(self.initialization)+gap_size)
        self.assertEqual(prefix.stop_reason, f'trailing bytes at {len(self.initialization)+gap_size+8}')
        self.assertLess(sum(size for _, size in requests), 128)

    def test_multitrack_clocks_restart_missing_times_and_discontinuities(self):
        initialization = with_audio(self.initialization)
        for video_timing, audio_timing, second_video_timing in (
                (0, 0, 100), (None, None, None), (0, 400, 101), (None, 200, 100)):
            payload = (self.pair_for(initialization, 1, 2, video_timing)+
                       self.pair_for(initialization, 2, 3, audio_timing)+
                       self.pair_for(initialization, 1, 4, second_video_timing))
            with self.subTest(times=(video_timing, audio_timing, second_video_timing)):
                self.compare(payload, initialization=initialization, later=True)
                self.compare(payload, initialization=initialization, later=False)
        self.compare(self.pair+self.pair_for(self.initialization, sequence=3, timing=100))

    def test_cached_facts_recheck_budget_and_read_current_bytes(self):
        cache = FragmentCache()
        second = self.pair_for(self.initialization, sequence=2, timing=100)
        self.compare(self.pair+second, cache=cache)
        initial_calls = cache.stats['fragment_parse_calls']
        _, requests = self.compare(self.pair+second, cache=cache)
        self.assertEqual(cache.stats['fragment_parse_calls'], initial_calls)
        self.assertEqual(cache.stats['fragment_parse_cache_hits'], 2)
        self.assertGreater(sum(size for _, size in requests), 0)
        with patch('videorebuild.fragmented.MAX_PREFIX_SAMPLES', 1):
            prefix, _ = self.compare(self.pair+second, cache=cache)
        self.assertEqual(len(prefix.fragments), 1)
        self.assertEqual(prefix.stop_reason, 'prefix_sample_budget_exceeded')
        changed = bytearray(self.pair)
        changed[-2] = 0x80
        prefix, _ = self.compare(bytes(changed), cache=cache)
        self.assertEqual(prefix.fragments, [])

    def test_cache_context_includes_track_defaults_and_cache_is_bounded(self):
        cache = FragmentCache()
        cache.parse(self.pair, self.init)
        changed = replace(self.init, tracks={1: replace(self.init.tracks[1], default_flags=1)})
        cache.parse(self.pair, changed)
        self.assertEqual(cache.stats['fragment_parse_calls'], 2)
        small = FragmentCache(max_bytes=1)
        self.compare(self.pair, cache=small)
        self.assertEqual(small.bytes, 0)
        self.assertFalse(small.entries)

    def test_cache_cannot_bypass_changed_scope_or_leak_mutable_clock_facts(self):
        cache = FragmentCache()
        fragment = cache.parse(self.pair, self.init)
        fragment.decode_times[1] = 999
        self.assertEqual(cache.parse(self.pair, self.init).decode_times[1], 0)
        requests = []
        def excluded(offset, size):
            requests.append((offset, size))
            raise FormatError('excluded_by_current_allocation_scope')
        output, prefix = walk_prefix_reader(self.init, excluded, len(self.pair), cache=cache)
        self.assertEqual(output, self.initialization)
        self.assertFalse(prefix.fragments)
        self.assertEqual(prefix.stop_reason, 'excluded_by_current_allocation_scope')
        self.assertTrue(requests)

    def test_prefilter_pair_reuses_read_and_successful_parse_facts(self):
        cache = FragmentCache()
        cache.parse(self.pair, self.init)
        _, requests = self.compare(self.pair, cache=cache, first_pair=self.pair)
        self.assertEqual(requests, [])
        self.assertEqual(cache.stats, {'fragment_parse_calls': 1, 'fragment_parse_cache_hits': 1})
