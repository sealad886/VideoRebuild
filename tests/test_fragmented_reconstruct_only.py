"""Structural reconstruction uses hand-built media; no external codec process runs."""
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fragmented_helpers import full, single_fragment
from videorebuild.bmff import make_box
from videorebuild.fragmented import parse_initialization
from videorebuild.image_recovery import recover_fragmented
from videorebuild.ranges import AllocationScope
from videorebuild.storage import Case, ReaderPool, SafetyError, scan_logical


def structural_movie(idr=True):
    entry = bytearray(78)
    struct.pack_into('>H', entry, 6, 1)
    struct.pack_into('>HH', entry, 24, 16, 16)
    config = make_box(b'avcC', bytes((1, 66, 0, 30, 255)))
    stsd = full(b'stsd', 0, struct.pack('>I', 1)+make_box(b'avc1', bytes(entry)+config))
    stbl = make_box(b'stbl', stsd+b''.join(full(kind, 0, b'\0'*4) for kind in (b'stts', b'stsc', b'stco'))+
                    full(b'stsz', 0, b'\0'*8))
    dref = full(b'dref', 0, struct.pack('>I', 1)+full(b'url ', 1, b''))
    minf = make_box(b'minf', make_box(b'dinf', dref)+stbl)
    mdia = make_box(b'mdia', full(b'mdhd', 0, b'\0'*8+struct.pack('>I', 1000))+
                    full(b'hdlr', 0, b'\0'*4+b'vide')+minf)
    trak = make_box(b'trak', full(b'tkhd', 0, b'\0'*8+struct.pack('>I', 1))+mdia)
    trex = full(b'trex', 0, struct.pack('>IIIII', 1, 1, 100, 0, 0))
    initialization = make_box(b'ftyp', b'isom\0\0\0\0')+make_box(b'moov', trak+make_box(b'mvex', trex))
    parsed = parse_initialization(initialization)
    payload = b'\0\0\0\2'+(b'\x65' if idr else b'\x41')+b'X'
    sample = SimpleNamespace(duration=100, flags=0, size=len(payload))
    pair = single_fragment(1, parsed.tracks[1], sample, payload, tfdt=0)
    return initialization, initialization+pair


class FragmentedReconstructOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.initialization, self.movie = structural_movie()

    def options(self, mode=True):
        return SimpleNamespace(max_record_bytes=1024*1024, max_segments=None, timeout=30,
                               max_output_bytes=1024*1024, reconstruct_only=mode)

    def index(self, case, data=None):
        source = self.root/'source.img'
        source.write_bytes(self.movie if data is None else data)
        sid = case.register_source(source, 'image')
        scan_logical(case, sid)
        return sid

    def test_prefix_is_exact_and_never_claims_decoder_validation(self):
        with Case(self.root/'case') as case:
            self.index(case)
            with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix') as validate:
                self.assertEqual(recover_fragmented(case, pool, {}, self.options()), 1)
            validate.assert_not_called()
            job = next(case.get_jobs('fragmented'))
            self.assertEqual(job['status'], 'reconstructed_prefix')
            self.assertFalse(job['detail']['decode_checked'])
            segment = next(s for s in job['detail']['segments'] if s.get('output'))
            self.assertEqual(segment['status'], 'reconstructed_prefix')
            self.assertFalse(segment['validation']['decode_checked'])
            self.assertEqual(segment['validation']['scope'], 'structural_reconstruction_only')
            self.assertEqual((case.root/segment['output']).read_bytes(), self.movie)

    def test_later_section_is_separate_and_allocation_rules_still_apply(self):
        payload = self.movie[len(self.initialization):]
        data = self.movie+b'gap'+payload
        with Case(self.root/'case') as case:
            sid = self.index(case, data)
            later = len(self.movie)+3
            scope = AllocationScope({sid: [(0, len(self.movie)), (later, len(payload))]}, 'scope')
            with ReaderPool(case.sources(), allocation_scope=scope) as pool, patch('videorebuild.image_recovery.validate_prefix') as validate:
                recover_fragmented(case, pool, {}, self.options())
            validate.assert_not_called()
            job = next(case.get_jobs('fragmented'))
            self.assertEqual(job['status'], 'reconstructed_segments')
            self.assertEqual(len(job['detail']['outputs']), 2)
            for name in job['detail']['outputs']:
                self.assertEqual((case.root/name).read_bytes(), self.movie)
        with Case(self.root/'excluded') as case:
            sid = self.index(case)
            with ReaderPool(case.sources(), allocation_scope=AllocationScope({sid: []}, 'excluded')) as pool:
                self.assertEqual(recover_fragmented(case, pool, {}, self.options()), 0)
            self.assertEqual(list(case.get_jobs('fragmented')), [])

    def test_non_idr_and_truncated_payload_are_not_exported(self):
        for data in (structural_movie(False)[1], self.movie[:-1]):
            with self.subTest(data=data[-8:]), tempfile.TemporaryDirectory(dir=self.root) as directory:
                with Case(Path(directory)/'case') as case:
                    self.index(case, data)
                    with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix') as validate:
                        recover_fragmented(case, pool, {}, self.options())
                    validate.assert_not_called()
                    self.assertFalse(next(case.get_jobs('fragmented'))['detail']['outputs'])

    def test_default_strict_mode_still_requires_quality_gate(self):
        with Case(self.root/'case') as case:
            self.index(case)
            with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix', return_value={'ok': False, 'reason': 'mocked_quality_failure'}) as validate:
                recover_fragmented(case, pool, {}, self.options(False))
            self.assertEqual(validate.call_count, 1)
            job = next(case.get_jobs('fragmented'))
            self.assertEqual(job['status'], 'decode_failed')
            self.assertFalse(job['detail']['outputs'])
            self.assertFalse(job['detail']['decode_checked'])

    def test_retry_mode_change_backs_up_tools_and_does_not_reuse_decoded_coverage(self):
        tools = {'ffmpeg': {'path': 'not-invoked'}, 'ffprobe': {'path': 'not-invoked'}}
        with Case(self.root/'case') as case:
            self.index(case)
            # Mock the existing strict contract; no decoder is executed here.
            with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix', return_value={'ok': True, 'reason': 'mocked_strict'}) as validate:
                recover_fragmented(case, pool, tools, self.options(False))
            self.assertEqual(validate.call_count, 1)
            old = next(case.get_jobs('fragmented'))
            originals = {name: (case.root/name).read_bytes() for name in old['detail']['outputs']}
            mode_tools = {'mode': 'structural_reconstruction_only'}
            with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix') as validate:
                with self.assertRaises(SafetyError):
                    recover_fragmented(case, pool, mode_tools, self.options())
                self.assertEqual(recover_fragmented(case, pool, mode_tools, self.options(), retry=True), 1)
            validate.assert_not_called()
            current = next(case.get_jobs('fragmented'))
            self.assertEqual(current['status'], 'reconstructed_prefix')
            self.assertTrue(current['detail']['search_policy']['reconstruct_only'])
            self.assertEqual(len(current['detail']['segments']), 1)
            self.assertFalse(current['detail']['segments'][0]['validation']['decode_checked'])
            for name, before in originals.items():
                self.assertEqual((case.root/name).read_bytes(), before)
            event = json.loads(case.db.execute("SELECT detail FROM events WHERE kind='fragmented_search_policy_expanded'").fetchone()[0])
            backup = json.loads((case.root/event['backup']).read_text())
            self.assertEqual(backup['fragmented_settings']['tools'], tools)
            self.assertTrue(event['mode_changed'])
