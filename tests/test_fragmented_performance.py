"""Structural recovery regressions for bounded reads and unchanged-job resume."""
from pathlib import Path
import hashlib
import json
import struct
import tempfile
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fragmented_helpers import full, single_fragment
from test_fragmented_reconstruct_only import structural_movie
from videorebuild.bmff import make_box
from videorebuild.fragmented import Fragment, FragmentSample, FragmentTrack, Initialization, Prefix, parse_initialization
from videorebuild import image_recovery
from videorebuild.image_recovery import recover_fragmented
from videorebuild.ranges import AllocationScope
from videorebuild.storage import Case, ReaderPool, SafetyError, scan_logical


def with_audio(initialization):
    """Add structural AAC metadata; no codec generation or acceptance is claimed."""
    entry = bytearray(28)
    struct.pack_into('>H', entry, 6, 1)
    stsd = full(b'stsd', 0, struct.pack('>I', 1)+make_box(b'mp4a', bytes(entry)+full(b'esds', 0, b'')))
    stbl = make_box(b'stbl', stsd+b''.join(full(kind, 0, b'\0'*4) for kind in (b'stts', b'stsc', b'stco'))+
                    full(b'stsz', 0, b'\0'*8))
    dref = full(b'dref', 0, struct.pack('>I', 1)+full(b'url ', 1, b''))
    minf = make_box(b'minf', make_box(b'dinf', dref)+stbl)
    mdia = make_box(b'mdia', full(b'mdhd', 0, b'\0'*8+struct.pack('>I', 1000))+
                    full(b'hdlr', 0, b'\0'*4+b'soun')+minf)
    audio = make_box(b'trak', full(b'tkhd', 0, b'\0'*8+struct.pack('>I', 2))+mdia)
    ftyp_size = int.from_bytes(initialization[:4], 'big')
    # Existing initialization contains video trak + mvex. Rebuild mvex with
    # both defaults rather than append a duplicate initialization-level box.
    moov_payload = initialization[ftyp_size+8:]
    video_size = int.from_bytes(moov_payload[:4], 'big')
    defaults = b''.join(full(b'trex', 0, struct.pack('>IIIII', tid, 1, 100, 0, 0)) for tid in (1, 2))
    return initialization[:ftyp_size]+make_box(b'moov', moov_payload[:video_size]+audio+make_box(b'mvex', defaults))


class PacketTimingTests(unittest.TestCase):
    def validate_packet(self, *, scale='1/1000', start=0, sample_duration=3, composition=-2, **packet_values):
        # These mocked probe responses exercise exact packet comparison without
        # claiming codec acceptance for the synthetic six-byte sample.
        data = b'\0\0\0\2\x65X'
        track = FragmentTrack(1, 'vide', 'avc1', 1000, 4, 0, 0, 0, '')
        init = Initialization(b'', {1: track})
        sample = FragmentSample(1, 0, len(data), sample_duration, 0, composition, (5,))
        prefix = Prefix(init, len(data), [Fragment(0, len(data), 1, (sample,), {1: start})],
                        starts={1: start})
        packet = dict(stream_index=0, dts=start, pts=start, duration=sample_duration,
                      data_hash='SHA256:'+hashlib.sha256(data).hexdigest())
        packet.update(packet_values)
        # A None sentinel removes a field so malformed evidence stays rejected.
        packet = {key: value for key, value in packet.items() if value is not None}
        probe = {'returncode': 0, 'timeout': False, 'stderr': '', 'stdout_truncated': False,
                 'stdout': json.dumps({'streams': [{'id': '0x1', 'index': 0, 'codec_name': 'h264',
                     'time_base': scale, 'nb_read_packets': 1, 'nb_read_frames': 1}], 'packets': [packet]})}
        decode = {'returncode': 0, 'timeout': False, 'stderr': ''}
        with patch('videorebuild.image_recovery.run_tool', side_effect=[probe, decode]) as run:
            result = image_recovery.validate_prefix(Path('synthetic.mp4'), data, prefix,
                        {'ffprobe': {'path': 'ffprobe'}, 'ffmpeg': {'path': 'ffmpeg'}}, 30)
        return result, run.call_count

    def test_unit_scale_preserves_negative_composition_normalization(self):
        result, calls = self.validate_packet(start=-6)
        self.assertTrue(result['ok'], result.get('reason'))
        self.assertEqual(calls, 2)

    def test_nonunit_rational_scale_preserves_exact_negative_ticks(self):
        result, calls = self.validate_packet(scale='3/2000', start=-6, dts=-4, pts=-4,
                                             duration=2, composition=-3)
        # Stream duration=2 at 3/2 source ticks equals parsed duration=3.
        self.assertTrue(result['ok'], result.get('reason'))
        self.assertEqual(calls, 2)

    def test_nonintegral_or_changed_timing_prevents_decode(self):
        for packet in ({'dts': -5}, {'pts': -5}, {'duration': 1}):
            with self.subTest(packet=packet):
                result, calls = self.validate_packet(scale='3/2000', start=-6,
                    **dict({'dts': -4, 'pts': -4, 'duration': 2}, **packet))
                self.assertFalse(result['ok'])
                self.assertEqual(result['reason'], 'packet_timing_mismatch')
                self.assertEqual(calls, 1)

    def test_malformed_packet_fields_prevent_decode(self):
        for packet in ({'duration': None}, {'pts': 'invalid'}, {'stream_index': 9}, {'duration': []}):
            with self.subTest(packet=packet):
                result, calls = self.validate_packet(**packet)
                self.assertFalse(result['ok'])
                self.assertEqual(calls, 1)


class ManifestBackfillTests(unittest.TestCase):
    def test_many_mixed_jobs_backfill_each_legacy_publication_once(self):
        with tempfile.TemporaryDirectory() as directory, Case(Path(directory)/'case') as case:
            keys = ['', '2', '10', 'z', '\u03b1']+[f'fragment-{i}' for i in range(260)]
            for index, key in enumerate(keys):
                output = case.root/'reconstructed'/f'legacy-{index}.mp4'
                output.write_bytes(b'unchanged media')
                case.result(key, 'reconstructed_prefix', {
                    'outputs': [str(output.relative_to(case.root))],
                    'output_evidence': [{'path': str(output.relative_to(case.root)), 'size': output.stat().st_size,
                                         'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}],
                    'padding': 'retained detail'}, kind='fragmented')
            case.result('no-output', 'blocked', {'outputs': [], 'reason': 'unchanged'}, kind='fragmented')
            case.result('ordinary', 'blocked', {'outputs': []}, kind='ordinary')
            pool = SimpleNamespace(sources={})
            image_recovery._backfill_fragment_manifests(case, pool)
            first = list(case.get_jobs('fragmented'))
            for job in first:
                detail = job['detail']
                if not detail.get('outputs'):
                    continue
                self.assertEqual(detail['padding'], 'retained detail')
                self.assertEqual(job['status'], 'reconstructed_prefix')
                entry = detail['output_evidence'][0]
                self.assertEqual((case.root/entry['path']).read_bytes(), b'unchanged media')
                manifest_path = case.root/entry['manifest_path']
                self.assertEqual(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), entry['manifest_sha256'])
                self.assertEqual(json.loads(manifest_path.read_text())['status'], 'historical_manifest_unavailable')
            self.assertEqual(len(list((case.root/'reconstructed').glob('*.json'))), len(keys))
            # Current entries remain stable on resume, including pages changed
            # by the first pass and keys with different lexical/numeric order.
            image_recovery._backfill_fragment_manifests(case, pool)
            self.assertEqual(list(case.get_jobs('fragmented')), first)
            self.assertEqual(case.job('fragmented', 'no-output')['detail'], {'outputs': [], 'reason': 'unchanged'})
            self.assertEqual(case.job('ordinary', 'ordinary')['detail'], {'outputs': []})

    def test_completed_catalogue_startup_does_not_retain_all_job_details(self):
        # Startup must stay bounded as a catalogue grows; this permits ample
        # overhead while rejecting retention of the complete 8 MiB detail set.
        with tempfile.TemporaryDirectory() as directory, Case(Path(directory)/'case') as case:
            raw = json.dumps({'outputs': [], 'padding': 'X'*8192})
            case.db.executemany('INSERT INTO jobs VALUES(?,?,?,?,?)',
                (('fragmented', str(i), 'blocked', raw, 0) for i in range(1024)))
            case.db.commit()
            tracemalloc.start()
            try:
                image_recovery._backfill_fragment_manifests(case, SimpleNamespace())
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 2*1024*1024)


class FragmentedPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.initialization, self.movie = structural_movie()
        self.options = SimpleNamespace(max_record_bytes=1024*1024, max_segments=None,
                                       max_output_bytes=1024*1024, timeout=30, reconstruct_only=True)

    def index(self, case, data=None, name='source.img'):
        source = self.root/name
        source.write_bytes(self.movie if data is None else data)
        sid = case.register_source(source, 'image')
        scan_logical(case, sid)
        return sid

    def pair(self, initialization, track_id, sequence, payload, tfdt):
        track = parse_initialization(initialization).tracks[track_id]
        sample = SimpleNamespace(duration=100, flags=0, size=len(payload))
        return single_fragment(sequence, track, sample, payload, tfdt=tfdt)

    def recover(self, case, **kwargs):
        with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix') as validate:
            result = recover_fragmented(case, pool, {}, self.options, **kwargs)
        validate.assert_not_called()
        return result

    def test_untimed_present_track_rejected_before_reading_remainder(self):
        pair = self.pair(self.initialization, 1, 2, b'\0\0\0\2\x65X', None)
        later = len(self.initialization)+8
        data = self.initialization+b'gapbytes'+pair+b'X'*(128*1024)
        with Case(self.root/'case') as case:
            sid = self.index(case, data)
            with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix') as validate:
                read = pool.read
                requests = []
                def observed(source_id, offset, size):
                    requests.append((offset, size))
                    return read(source_id, offset, size)
                with patch.object(pool, 'read', side_effect=observed):
                    recover_fragmented(case, pool, {}, self.options)
            validate.assert_not_called()
            job = case.job('fragmented', f'{sid}:0')
            self.assertEqual(job['status'], 'blocked')
            self.assertEqual(job['detail']['outputs'], [])
            self.assertTrue(any(s.get('reason') == 'missing_restart_track_timing' for s in job['detail']['segments']))
            # Both initial walking and later prefilters avoid the unused tail.
            # The bounded mdat header peek may include up to 16 bytes; no
            # request may fetch the remaining large window after that pair.
            self.assertFalse(any(offset >= later and offset+size > later+len(pair)+16 for offset, size in requests))

    def test_retained_prefix_excludes_window_tail_and_preserves_source(self):
        data = self.movie+bytes(8192)
        with Case(self.root/'case') as case:
            sid = self.index(case, data)
            self.assertEqual(self.recover(case), 1)
            detail = case.job('fragmented', f'{sid}:0')['detail']
            self.assertEqual(len(detail['outputs']), 1)
            self.assertEqual((case.root/detail['outputs'][0]).read_bytes(), self.movie)
            self.assertEqual((self.root/'source.img').read_bytes(), data)
            entry = detail['output_evidence'][0]
            digest = hashlib.sha256(self.movie).hexdigest()
            self.assertEqual((entry['size'], entry['sha256']), (len(self.movie), digest))
            manifest = json.loads((case.root/entry['manifest_path']).read_text())
            self.assertEqual((manifest['output_size'], manifest['output_sha256']), (len(self.movie), digest))

    def test_track_absent_from_first_fragment_can_start_later_with_timing(self):
        init = with_audio(self.initialization)
        video = self.pair(init, 1, 2, b'\0\0\0\2\x65X', 0)
        audio = self.pair(init, 2, 3, b'AX', 0)
        data = init+b'gapbytes'+video+audio
        with Case(self.root/'case') as case:
            sid = self.index(case, data)
            self.recover(case)
            job = case.job('fragmented', f'{sid}:0')
            self.assertEqual(job['status'], 'reconstructed_segments')
            self.assertFalse(job['detail']['decode_checked'])
            self.assertEqual(len(job['detail']['outputs']), 1)
            self.assertEqual((case.root/job['detail']['outputs'][0]).read_bytes(), init+video+audio)

    def test_later_timing_cannot_repair_first_present_track_missing_timing(self):
        first = self.pair(self.initialization, 1, 2, b'\0\0\0\2\x65X', None)
        following = self.pair(self.initialization, 1, 3, b'\0\0\0\2\x41X', 100)
        with Case(self.root/'case') as case:
            sid = self.index(case, self.initialization+b'gapbytes'+first+following)
            self.recover(case)
            job = case.job('fragmented', f'{sid}:0')
            self.assertEqual(job['detail']['outputs'], [])
            self.assertTrue(any(s.get('reason') == 'missing_restart_track_timing' for s in job['detail']['segments']))

    def test_completed_resume_does_not_read_initialization_media(self):
        with Case(self.root/'case') as case:
            self.index(case)
            self.recover(case)
            with ReaderPool(case.sources()) as pool, patch.object(pool, 'read', side_effect=AssertionError('completed media reread')):
                self.assertEqual(recover_fragmented(case, pool, {}, self.options, retry=True), 0)

    def test_completed_resume_still_rejects_changed_source(self):
        with Case(self.root/'case') as case:
            self.index(case)
            self.recover(case)
            with (self.root/'source.img').open('ab') as stream:
                stream.write(b'changed')
            with ReaderPool(case.sources()) as pool, patch.object(pool, 'read', side_effect=AssertionError('changed media read')):
                with self.assertRaises(SafetyError):
                    recover_fragmented(case, pool, {}, self.options)

    def test_completed_resume_rejects_source_mutation_during_job_lookup(self):
        with Case(self.root/'case') as case:
            self.index(case, self.movie+self.movie)
            self.assertEqual(self.recover(case), 2)
            lookup = case.job
            changed = False
            def mutate(kind, key):
                nonlocal changed
                result = lookup(kind, key)
                if not changed:
                    with (self.root/'source.img').open('ab') as stream:
                        stream.write(b'changed')
                    changed = True
                return result
            with ReaderPool(case.sources()) as pool, patch.object(case, 'job', side_effect=mutate), \
                    patch.object(pool, 'read', side_effect=AssertionError('changed media read')):
                with self.assertRaises(SafetyError):
                    recover_fragmented(case, pool, {}, self.options)

    def test_completed_resume_rechecks_source_after_discovery(self):
        with Case(self.root/'case') as case:
            self.index(case, self.movie+self.movie)
            self.assertEqual(self.recover(case), 2)
            locations = image_recovery._initialization_locations
            def mutate_after_discovery(*args):
                yield from locations(*args)
                with (self.root/'source.img').open('ab') as stream:
                    stream.write(b'changed')
            with ReaderPool(case.sources()) as pool, \
                    patch.object(image_recovery, '_initialization_locations', side_effect=mutate_after_discovery), \
                    patch.object(pool, 'read', side_effect=AssertionError('changed media read')):
                with self.assertRaises(SafetyError):
                    recover_fragmented(case, pool, {}, self.options)

    def test_completed_resume_still_checks_output_and_evidence_hashes(self):
        for artifact in ('output', 'evidence'):
            with self.subTest(artifact=artifact), Case(self.root/f'case-{artifact}') as case:
                sid = self.index(case, name=f'{artifact}.img')
                self.recover(case)
                detail = case.job('fragmented', f'{sid}:0')['detail']
                relative = detail['outputs'][0] if artifact == 'output' else detail['detail_path']
                path = case.root/relative
                data = bytearray(path.read_bytes())
                data[-1] ^= 1
                path.write_bytes(data)
                with ReaderPool(case.sources()) as pool, patch.object(pool, 'read', side_effect=AssertionError('completed media reread')):
                    with self.assertRaises(SafetyError):
                        recover_fragmented(case, pool, {}, self.options)

    def test_retry_reconsiders_blocked_job(self):
        with Case(self.root/'case') as case:
            self.index(case, self.initialization+b'gapbytes')
            self.recover(case)
            with ReaderPool(case.sources()) as pool, patch.object(pool, 'read', wraps=pool.read) as read:
                self.assertEqual(recover_fragmented(case, pool, {}, self.options, retry=True), 1)
                self.assertGreater(read.call_count, 0)

    def test_limit_after_completed_skip_leaves_pending_job_recoverable(self):
        with Case(self.root/'case') as case:
            first = self.index(case, name='first.img')
            second = self.index(case, name='second.img')
            self.assertEqual(self.recover(case, limit=1), 1)
            self.assertIsNone(case.job('fragmented', f'{second}:0'))
            with ReaderPool(case.sources()) as pool, patch.object(pool, 'read', wraps=pool.read) as read:
                self.assertEqual(recover_fragmented(case, pool, {}, self.options, limit=1), 1)
                self.assertTrue(read.call_args_list)
                self.assertEqual({call.args[0] for call in read.call_args_list}, {second})
            self.assertIsNotNone(case.job('fragmented', f'{first}:0'))
            self.assertIsNotNone(case.job('fragmented', f'{second}:0'))

    def test_scope_expansion_revisits_previously_unavailable_initialization(self):
        with Case(self.root/'case') as case:
            sid = self.index(case)
            for allowed, digest, retry in (([], 'excluded', False), ([(0, len(self.movie))], 'included', True)):
                scope = AllocationScope({sid: allowed}, digest)
                with ReaderPool(case.sources(), allocation_scope=scope) as pool:
                    read = pool.read
                    def permitted(source_id, offset, size):
                        self.assertTrue(pool.permits(source_id, offset, size))
                        return read(source_id, offset, size)
                    with patch.object(pool, 'read', side_effect=permitted):
                        self.assertEqual(recover_fragmented(case, pool, {}, self.options, retry=retry), int(retry))
            job = case.job('fragmented', f'{sid}:0')
            self.assertEqual(job['status'], 'reconstructed_prefix')
            self.assertEqual(job['detail']['search_policy']['allocation_scope'], 'included')


if __name__ == '__main__':
    unittest.main()
