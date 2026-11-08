"""Strict failures should avoid counting while successful decodes retain every gate."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_deep_witnesses import structural_movie
from videorebuild import engine
import videorebuild.validate as validation
from videorebuild.storage import Case, ReaderPool, scan_logical


class ValidateOrderTests(unittest.TestCase):
    def setUp(self):
        self.tools = {'ffmpeg': {'path': 'ffmpeg', 'version': 'fixture'},
                      'ffprobe': {'path': 'ffprobe', 'version': 'fixture'}}
        self.movie = SimpleNamespace(tracks=(SimpleNamespace(
            handler=b'vide', codec=b'avc1', track_id=1, sample_count=2),))

    def tool_result(self, **changes):
        result = dict(returncode=0, timeout=False, stderr='', stdout='',
                      stdout_truncated=False, stderr_truncated=False)
        result.update(changes)
        return result

    def probe_result(self, **changes):
        stream = dict(codec_type='video', codec_name='h264', nb_read_packets=2, nb_read_frames=2)
        stream.update(changes)
        return self.tool_result(stdout=json.dumps({'streams': [stream]}))

    def test_decoder_failure_skips_count_probe(self):
        for failure in ({'returncode': 1}, {'stderr': 'invalid encoded sample'}, {'timeout': True}):
            with self.subTest(failure=failure):
                decode = self.tool_result(**failure)
                with patch.object(validation, 'run_tool', return_value=decode) as run:
                    result = validation.validate(Path('candidate.mp4'), self.movie, self.tools, 10)
                self.assertFalse(result['ok'])
                self.assertEqual(result['decode'], decode)
                self.assertNotIn('probe', result)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][0], 'ffmpeg')

    def test_success_requires_strict_decode_and_exact_counts(self):
        decode = self.tool_result()
        probe = self.probe_result()
        with patch.object(validation, 'run_tool', side_effect=[decode, probe]) as run:
            result = validation.validate(Path('candidate.mp4'), self.movie, self.tools, 10)
        self.assertTrue(result['ok'])
        self.assertEqual(result['reason'], 'decode_checked')
        self.assertEqual(result['decode'], decode)
        self.assertEqual(result['probe'], probe)
        self.assertEqual(result['counts'], [dict(track=1, expected_packets=2, packets=2, frames=2, codec='h264')])
        decode_args, probe_args = [call.args[0] for call in run.call_args_list]
        self.assertIn('-xerror', decode_args)
        self.assertEqual(decode_args[decode_args.index('-err_detect')+1], 'explode')
        self.assertIn('-count_packets', probe_args)
        self.assertIn('-count_frames', probe_args)

    def test_successful_decode_does_not_bypass_probe_rejections(self):
        for probe in (self.probe_result(nb_read_packets=3), self.probe_result(nb_read_frames=1),
                      self.probe_result(codec_name='hevc'), self.probe_result(nb_read_frames=None),
                      self.tool_result(stdout='invalid json'), self.tool_result(timeout=True),
                      self.tool_result(returncode=1), self.tool_result(stderr='probe warning'),
                      self.tool_result(stdout_truncated=True)):
            with self.subTest(probe=probe):
                with patch.object(validation, 'run_tool', side_effect=[self.tool_result(), probe]) as run:
                    result = validation.validate(Path('candidate.mp4'), self.movie, self.tools, 10)
                self.assertFalse(result['ok'])
                self.assertEqual(result['probe'], probe)
                self.assertEqual(run.call_count, 2)

    def test_early_rejection_receipt_is_reused_and_timeout_stays_retryable(self):
        token = b'\0\0\0\x04\x65ABC'
        movie = structural_movie([100], [len(token)])
        for timed_out in (False, True):
            with self.subTest(timeout=timed_out), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root/'token.bin'
                source.write_bytes(token)
                with Case(root/'case') as case:
                    sid = case.register_source(source)
                    scan_logical(case, sid)
                    mapping = {(0, 0): (sid, 0)}
                    failed_decode = self.tool_result(returncode=1, timeout=timed_out, stderr='decode failed')
                    with ReaderPool(case.sources()) as pool:
                        with patch.object(validation, 'run_tool', return_value=failed_decode) as run:
                            first = engine.attempt(case, movie, mapping, pool, self.tools,
                                                   engine.Options(), 'fixture')
                            second = engine.attempt(case, movie, mapping, pool, self.tools,
                                                    engine.Options(), 'fixture')
                        self.assertEqual(run.call_count, 2 if timed_out else 1)
                        self.assertEqual(first['status'], 'limited' if timed_out else 'rejected')
                        self.assertEqual(second['status'], first['status'])
                        self.assertEqual(second['tools'], self.tools)
                        self.assertEqual(second['validation_revision'], engine.VALIDATION_REVISION)
                        self.assertEqual(second['validation']['decode']['timeout'], timed_out)
