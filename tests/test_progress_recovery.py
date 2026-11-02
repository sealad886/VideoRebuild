import contextlib
import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from helpers import generate, movie_from, payload
from videorebuild import engine
from videorebuild.bmff import Chunk
from videorebuild.progress import reporting
from videorebuild.storage import ReaderPool
from videorebuild.validate import run_tool


class WaitingProcess:
    def __init__(self, clock, duration, *, interrupt=False):
        self.clock = clock
        self.remaining = duration
        self.interrupt = interrupt
        self.returncode = None
        self.killed = False
        self.reaped = False
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.killed:
            self.reaped = True
            return self.returncode
        if self.interrupt:
            raise KeyboardInterrupt
        elapsed = min(timeout, self.remaining)
        self.clock.now += elapsed
        self.remaining -= elapsed
        if self.remaining:
            raise subprocess.TimeoutExpired('synthetic tool', timeout)
        self.returncode = 0
        return 0

    def kill(self):
        self.killed = True
        self.returncode = -9


class RecoveryProgressTests(unittest.TestCase):
    def test_slow_process_heartbeat_keeps_one_timeout_deadline(self):
        for duration, timeout, expected_timeout in ((12, 20, False), (100, 7, True)):
            with self.subTest(duration=duration):
                clock = SimpleNamespace(now=0.0)
                process = WaitingProcess(clock, duration)
                stream = io.StringIO()
                with patch('videorebuild.validate.subprocess.Popen', return_value=process), \
                        patch('videorebuild.validate.time.monotonic', side_effect=lambda: clock.now), \
                        reporting(stream=stream, clock=lambda: clock.now):
                    result = run_tool(['/tools/ffmpeg', 'private-input.mp4'], timeout)
                self.assertEqual(result['timeout'], expected_timeout)
                self.assertEqual(clock.now, min(duration, timeout))
                self.assertTrue(all(wait <= 5 for wait in process.waits if wait is not None))
                self.assertIn('running', stream.getvalue())
                self.assertNotIn('%', stream.getvalue())
                self.assertNotIn('private-input.mp4', stream.getvalue())
                self.assertEqual(process.killed, expected_timeout)
                self.assertEqual(process.reaped, expected_timeout)
                self.assertIn('failed' if expected_timeout else 'finished', stream.getvalue())

    def test_nonzero_tool_exit_is_reported_as_failure(self):
        process = Mock(returncode=7)
        stream = io.StringIO()
        with patch('videorebuild.validate.subprocess.Popen', return_value=process), reporting(stream=stream):
            result = run_tool(['/tools/ffmpeg'], 30)
        self.assertEqual(result['returncode'], 7)
        self.assertFalse(result['timeout'])
        self.assertIn('failed', stream.getvalue())
        process.kill.assert_not_called()

    def test_interrupted_process_is_killed_reaped_and_reported(self):
        clock = SimpleNamespace(now=0.0)
        process = WaitingProcess(clock, 100, interrupt=True)
        stream = io.StringIO()
        with patch('videorebuild.validate.subprocess.Popen', return_value=process), \
                reporting(stream=stream):
            with self.assertRaises(KeyboardInterrupt):
                run_tool(['/tools/ffprobe'], 30)
        self.assertTrue(process.killed)
        self.assertTrue(process.reaped)
        self.assertIn('interrupted', stream.getvalue())

    def test_reconstruction_progress_preserves_encoded_bytes_and_quiet_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original.mp4'
            generate(original, audio=False, seconds=0.25)
            raw = original.read_bytes()
            movie = movie_from(raw)
            expected = payload(original)
            info = original.stat()
            sources = {1: dict(path=str(original), size=info.st_size, mtime_ns=info.st_mtime_ns,
                               dev=info.st_dev, ino=info.st_ino, kind='file',
                               sha256=hashlib.sha256(raw).hexdigest())}
            mapping = {(c.track, c.index): (1, c.offset) for c in movie.chunks}
            quiet = io.StringIO()
            visible = io.StringIO()
            for enabled in (False, True):
                target = root / f'candidate-{enabled}.mp4'
                reporting_context = reporting(stream=visible) if enabled else contextlib.nullcontext()
                with ReaderPool(sources) as pool, contextlib.redirect_stderr(quiet), reporting_context:
                    proof = engine.reconstruct(movie, mapping, pool, target, 1024*1024)
                self.assertEqual(payload(target), expected)
                self.assertEqual(proof['media_sha256'], hashlib.sha256(expected).hexdigest())
            self.assertEqual(quiet.getvalue(), '')
            self.assertIn(f'{len(expected):,}/{len(expected):,} bytes', visible.getvalue())
            self.assertEqual(original.read_bytes(), raw)

    def test_sample_validation_progress_clock_reads_are_batched(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'samples.bin'
            source.write_bytes(bytes.fromhex('0000000165') * 9000)
            info = source.stat()
            sources = {1: dict(path=str(source), size=info.st_size, mtime_ns=info.st_mtime_ns,
                               dev=info.st_dev, ino=info.st_ino)}
            movie = SimpleNamespace(chunks=[Chunk(0, 0, 0, (5,)*9000, 0)],
                                    tracks=[SimpleNamespace(handler=b'vide', codec=b'avc1', nal_length=4)])
            clock = Mock(return_value=0.0)
            stream = io.StringIO()
            with ReaderPool(sources) as pool, reporting(stream=stream, clock=clock):
                self.assertTrue(engine.check_samples(movie, {(0, 0): (1, 0)}, pool))
            self.assertLess(clock.call_count, 10)
            self.assertIn('9,000/9,000 samples', stream.getvalue())

    def test_deep_progress_counts_available_bytes_and_reports_search_limit(self):
        for data, limited in ((bytes(16), False), (bytes.fromhex('0000000209f0')*4, True)):
            with self.subTest(limited=limited), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'physical.bin'
                source.write_bytes(data)
                info = source.stat()
                physical = {1: dict(path=str(source), size=info.st_size, mtime_ns=info.st_mtime_ns,
                                    dev=info.st_dev, ino=info.st_ino)}
                logical = {2: dict(path='logical stream', size=4096, extents=[
                    dict(logical_offset=2048, source_id=1, source_offset=0, length=len(data))])}
                movie = SimpleNamespace(digest='synthetic')
                group = SimpleNamespace(key=('synthetic', 0), anchors=lambda: [(1024, 0)])
                stream = io.StringIO()
                with ReaderPool(logical, physical_sources=physical) as pool, reporting(stream=stream), \
                        patch.object(engine, 'groups_for', return_value=[group]), patch.object(engine, 'finish'):
                    engine.deep_search(SimpleNamespace(root=Path(directory)), [movie], pool, {},
                                       engine.Options(max_anchor_checks=1), 'synthetic')
                self.assertNotIn('/4,096 bytes', stream.getvalue())
                if limited:
                    self.assertIn('limited', stream.getvalue())
                    self.assertNotIn(f'{len(data)}/{len(data)} bytes', stream.getvalue())
                else:
                    self.assertIn(f'{len(data)}/{len(data)} bytes', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
