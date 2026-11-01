"""Deep-search location multiplicity and ambiguity, using structural fixtures."""
import hashlib
from itertools import product
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from videorebuild import engine
from videorebuild.bmff import make_box, parse_movie
from videorebuild.storage import Case, ReaderPool


def structural_movie(offsets, sizes):
    """Build supported sample tables without invoking codec tools."""
    def integers(*values):
        return struct.pack('>' + 'I' * len(values), *values)

    def full(kind, body):
        return make_box(kind, b'\0' * 4 + body)

    sample_entry = make_box(b'avc1', b'\0' * 6 + b'\0\1' + b'\0' * 70 +
                            make_box(b'avcC', b'\1\x42\0\x1e\xff'))
    table = make_box(b'stbl', full(b'stsd', integers(1) + sample_entry) +
                     full(b'stsz', integers(0, len(offsets), *sizes)) +
                     full(b'stsc', integers(1, 1, 1, 1)) +
                     full(b'stco', integers(len(offsets), *offsets)) +
                     full(b'stts', integers(1, len(offsets), 1)))
    references = full(b'dref', integers(1) + make_box(b'url ', b'\0\0\0\1'))
    media = make_box(b'mdia', full(b'mdhd', integers(0, 0, 1000, len(offsets))) +
                     full(b'hdlr', integers(0) + b'vide') +
                     make_box(b'minf', make_box(b'dinf', references) + table))
    track = make_box(b'trak', full(b'tkhd', integers(0, 0, 1, 0)) + media)
    return parse_movie(make_box(b'moov', full(b'mvhd', b'\0' * 20) + track))


def feasible_payloads(data, choices, sizes):
    """Independent interval oracle: no engine range/assembly helpers."""
    outputs = set()
    for offsets in product(*choices):
        if any(a < b + nb and b < a + na
               for i, (a, na) in enumerate(zip(offsets, sizes))
               for b, nb in list(zip(offsets, sizes))[i + 1:]):
            continue
        outputs.add(hashlib.sha256(b''.join(data[o:o+n] for o, n in zip(offsets, sizes))).hexdigest())
    return outputs


class DeepWitnessTests(unittest.TestCase):
    def recover(self, data, sizes, reverse_sources=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source = root / 'tokens.bin'
        source.write_bytes(data)
        movie = structural_movie([100 + 100*i for i in range(len(sizes))], sizes)
        with Case(root / 'case') as case:
            sid = case.register_source(source, 'file')
            sources = {sid: dict(case.db.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone())}
            if reverse_sources:
                alias = root / 'alias.bin'
                alias.hardlink_to(source)
                alias_id = case.register_source(alias, 'file')
                sources[alias_id] = dict(case.db.execute('SELECT * FROM sources WHERE id=?', (alias_id,)).fetchone())
                sources = dict(reversed(list(sources.items())))
            with ReaderPool(sources) as pool, patch.object(engine, 'doctor', side_effect=AssertionError('no codecs')), \
                    patch.object(engine, 'validate', side_effect=AssertionError('no decode checks')):
                with patch.object(engine, 'attempt', wraps=engine.attempt) as attempted:
                    engine.deep_search(case, [movie], pool, {}, engine.Options(deep=True, reconstruct_only=True), 'fixture')
                self.last_attempt_count = attempted.call_count
            job = case.job('ordinary', movie.digest)
            candidates = {row['media_sha256'] for row in job['detail']['candidates']}
            self.assertEqual(source.read_bytes(), data)
            for candidate in job['detail']['candidates']:
                self.assertFalse(candidate['validation']['decode_checked'])
            return job, candidates

    def test_identical_groups_keep_nonoverlapping_physical_copies(self):
        token = b'\0\0\0\x04\x65ABC'
        data = token + b'Z' * 8 + token
        expected = feasible_payloads(data, [[0, 16], [0, 16]], [8, 8])
        for aliases in (False, True):
            with self.subTest(aliases=aliases):
                job, candidates = self.recover(data, [8, 8], aliases)
                self.assertEqual(candidates, expected)
                self.assertEqual(job['status'], 'reconstructed')
                self.assertEqual(len(job['detail']['outputs']), 1)

    def test_many_identical_copies_need_one_complete_content_attempt(self):
        token = b'\0\0\0\x04\x65ABC'
        data = (token + b'Z' * 8) * 8
        job, candidates = self.recover(data, [8] * 8)
        self.assertEqual(job['status'], 'reconstructed')
        self.assertEqual(candidates, {hashlib.sha256(token * 8).hexdigest()})
        self.assertEqual(self.last_attempt_count, 1)
        self.assertEqual(job['detail']['tested_assemblies'], 1)
        self.assertLess(job['detail']['partial_assignments_checked'], 500)

    def test_equal_content_locations_do_not_hide_byte_distinct_alternative(self):
        nested = b'\0\0\0\x04\x65BBB'
        other = b'\0\0\0\x04\x65CCC'
        large = (12).to_bytes(4, 'big') + b'\x65abc' + nested
        data = large + b'Z' * 16 + large + b'Z' * 16 + other
        expected = feasible_payloads(data, [[0, 32], [8, 40, 64]], [16, 8])
        self.assertEqual(len(expected), 2)
        for aliases in (False, True):
            with self.subTest(aliases=aliases):
                job, candidates = self.recover(data, [16, 8], aliases)
                self.assertEqual(candidates, expected)
                self.assertEqual(job['status'], 'ambiguous')
                self.assertEqual(job['detail']['outputs'], [])


if __name__ == '__main__':
    unittest.main()
