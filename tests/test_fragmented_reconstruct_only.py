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
from videorebuild.image_recovery import recover_fragmented, verify_completed
from videorebuild.ranges import AllocationScope
from videorebuild.storage import Case, ReaderPool, SafetyError, file_sha256, scan_logical


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

    def test_failed_strict_upgrade_remains_retryable_until_requested_mode_is_accepted(self):
        with Case(self.root/'case') as case:
            sid = self.index(case)
            source = self.root/'source.img'
            identity = source.stat()
            with ReaderPool(case.sources()) as pool:
                recover_fragmented(case, pool, {}, self.options())
            original = case.job('fragmented', f'{sid}:0')
            entry = original['detail']['output_evidence'][0]
            output = case.root/entry['path']
            output_identity = output.stat()
            manifest = case.root/entry['manifest_path']
            original_manifest = manifest.read_bytes()
            with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix',
                    return_value={'ok': False, 'reason': 'temporary_decode_failure'}) as validate:
                self.assertEqual(recover_fragmented(case, pool, {}, self.options(False), retry=True), 1)
            self.assertEqual(validate.call_count, 1)
            failed = case.job('fragmented', f'{sid}:0')
            self.assertEqual(failed['detail']['outputs'], original['detail']['outputs'])
            self.assertFalse(failed['detail']['decode_checked'])
            self.assertFalse(failed['detail']['search_policy'].get('reconstruct_only', False))
            verify_completed(case)
            with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix',
                    return_value={'ok': True, 'reason': 'mocked_strict_gate'}) as validate:
                self.assertEqual(recover_fragmented(case, pool, {}, self.options(False), retry=True), 1)
            self.assertEqual(validate.call_count, 1)
            accepted = case.job('fragmented', f'{sid}:0')
            self.assertEqual(accepted['status'], 'decoded_prefix')
            self.assertTrue(accepted['detail']['decode_checked'])
            self.assertEqual(manifest.read_bytes(), original_manifest)
            self.assertEqual(output.read_bytes(), self.movie)
            self.assertEqual((output.stat().st_ino, output.stat().st_mtime_ns),
                             (output_identity.st_ino, output_identity.st_mtime_ns))
            self.assertEqual(source.read_bytes(), self.movie)
            current_identity = source.stat()
            for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                self.assertEqual(getattr(current_identity, field), getattr(identity, field))
            verify_completed(case)
            with ReaderPool(case.sources()) as pool, \
                    patch.object(pool, 'read', side_effect=AssertionError('completed media reread')), \
                    patch('videorebuild.image_recovery.validate_prefix') as validate:
                self.assertEqual(recover_fragmented(case, pool, {}, self.options(False), retry=True), 0)
            validate.assert_not_called()
            self.assertEqual(case.job('fragmented', f'{sid}:0'), accepted)

    def test_mode_transition_interruption_preserves_committed_manifest_and_resumes(self):
        from videorebuild import image_recovery
        for initial_mode in (True, False):
            for boundary in ('manifest_written', 'before_job_commit'):
                with self.subTest(initial_mode=initial_mode, boundary=boundary):
                    case_root = self.root/f'case-{initial_mode}-{boundary}'
                    with Case(case_root) as case:
                        sid = self.index(case)
                        source = self.root/'source.img'
                        identity = source.stat()
                        with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix',
                                return_value={'ok': True, 'reason': 'mocked_strict_gate'}):
                            recover_fragmented(case, pool, {}, self.options(initial_mode))
                        original = case.job('fragmented', f'{sid}:0')
                        entry = original['detail']['output_evidence'][0]
                        output = case.root/entry['path']
                        output_identity = output.stat()
                        original_manifest = case.root/entry['manifest_path']
                        original_bytes = original_manifest.read_bytes()
                        write_json = image_recovery.write_json
                        result = case.result
                        new_manifests = []

                        def write_and_interrupt(path, value):
                            write_json(path, value)
                            if value.get('kind') == 'ordered-fragment-manifest':
                                new_manifests.append(path)
                                if boundary == 'manifest_written':
                                    raise KeyboardInterrupt('interrupted after manifest write')

                        def commit_and_interrupt(*args, **kwargs):
                            if boundary == 'before_job_commit' and kwargs.get('kind') == 'fragmented':
                                raise KeyboardInterrupt('interrupted before job commit')
                            return result(*args, **kwargs)

                        with ReaderPool(case.sources()) as pool, \
                                patch('videorebuild.image_recovery.validate_prefix',
                                      return_value={'ok': True, 'reason': 'mocked_strict_gate'}), \
                                patch('videorebuild.image_recovery.write_json', side_effect=write_and_interrupt), \
                                patch.object(case, 'result', side_effect=commit_and_interrupt):
                            with self.assertRaises(KeyboardInterrupt):
                                recover_fragmented(case, pool, {}, self.options(not initial_mode), retry=True)
                        self.assertEqual(len(new_manifests), 1)
                    with Case(case_root) as case:
                        self.assertEqual(case.job('fragmented', f'{sid}:0'), original)
                        self.assertEqual(original_manifest.read_bytes(), original_bytes)
                        self.assertEqual(file_sha256(original_manifest), entry['manifest_sha256'])
                        self.assertNotEqual(new_manifests[0], original_manifest)
                        verify_completed(case)
                        with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix',
                                return_value={'ok': True, 'reason': 'mocked_strict_gate'}) as validate:
                            self.assertEqual(recover_fragmented(case, pool, {}, self.options(not initial_mode), retry=True), 1)
                        self.assertEqual(validate.call_count, int(initial_mode))
                        current = case.job('fragmented', f'{sid}:0')
                        current_entry = current['detail']['output_evidence'][0]
                        current_manifest = case.root/current_entry['manifest_path']
                        self.assertNotEqual(current_manifest, original_manifest)
                        self.assertEqual(json.loads(current_manifest.read_text())['decode_checked'], initial_mode)
                        self.assertEqual(current['detail']['decode_checked'], initial_mode)
                        self.assertEqual(original_manifest.read_bytes(), original_bytes)
                        self.assertTrue(new_manifests[0].is_file())
                        self.assertEqual(output.read_bytes(), self.movie)
                        self.assertEqual((output.stat().st_ino, output.stat().st_mtime_ns),
                                         (output_identity.st_ino, output_identity.st_mtime_ns))
                        self.assertEqual(source.read_bytes(), self.movie)
                        current_identity = source.stat()
                        for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
                            self.assertEqual(getattr(current_identity, field), getattr(identity, field))
                        verify_completed(case)

    def test_partial_strict_job_retries_uncovered_failed_or_limited_section(self):
        track = parse_initialization(self.initialization).tracks[1]
        payload = b'\0\0\0\2\x41X'
        sample = SimpleNamespace(duration=100, flags=0, size=len(payload))
        following = single_fragment(2, track, sample, payload, tfdt=100)
        later = len(self.movie)+3
        data = self.movie+b'gap'+self.movie[len(self.initialization):]+following
        for failure in ('decode_failed', 'search_limited'):
            with self.subTest(failure=failure), Case(self.root/f'case-{failure}') as case:
                sid = self.index(case, data)
                options = self.options(False)
                if failure == 'search_limited':
                    options.max_output_bytes = len(self.movie)
                outcomes = [{'ok': True, 'reason': 'mocked_strict_gate'}]
                if failure == 'decode_failed':
                    outcomes.append({'ok': False, 'reason': 'temporary_decode_failure'})
                with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix',
                        side_effect=outcomes) as validate:
                    recover_fragmented(case, pool, {}, options)
                self.assertEqual(validate.call_count, len(outcomes))
                original = case.job('fragmented', f'{sid}:0')
                self.assertEqual(len(original['detail']['outputs']), 1)
                self.assertTrue(any(s['logical_start'] == later and s['status'] == failure
                                    for s in original['detail']['segments']))
                options.max_output_bytes = 1024*1024
                with ReaderPool(case.sources()) as pool, patch('videorebuild.image_recovery.validate_prefix',
                        return_value={'ok': True, 'reason': 'mocked_strict_gate'}) as validate:
                    self.assertEqual(recover_fragmented(case, pool, {}, options, retry=True), 1)
                self.assertEqual(validate.call_count, 1)
                accepted = case.job('fragmented', f'{sid}:0')
                self.assertEqual(accepted['status'], 'decoded_segments')
                self.assertEqual(len(accepted['detail']['outputs']), 2)
                self.assertEqual((case.root/original['detail']['outputs'][0]).read_bytes(), self.movie)
                verify_completed(case)
                with ReaderPool(case.sources()) as pool, \
                        patch.object(pool, 'read', side_effect=AssertionError('completed media reread')), \
                        patch('videorebuild.image_recovery.validate_prefix') as validate:
                    self.assertEqual(recover_fragmented(case, pool, {}, options, retry=True), 0)
                validate.assert_not_called()
                self.assertEqual(case.job('fragmented', f'{sid}:0'), accepted)
