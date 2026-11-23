"""Clip projection preserves sample bytes and the original media timing tables."""
from itertools import accumulate
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from helpers import generate, movie_from
from videorebuild.bmff import FormatError, boxes, children, make_box, one, parse_movie, path
from videorebuild.ordinary_clip import OrdinaryClip


def video_tables(movie):
    root = next(boxes(movie.data))
    tracks = [child for child in children(movie.data, root) if child.kind == b'trak']
    track = next(track for track in movie.tracks if track.handler == b'vide')
    box = path(movie.data, tracks[track.index], b'mdia', b'minf', b'stbl')
    return {child.kind: movie.data[child.payload:child.end] for child in children(movie.data, box)}


def sample_values(table, value_format):
    return [value for count, value in struct.iter_unpack(value_format, table[8:])
            for _ in range(count)]


def sample_bytes(raw, track):
    return [raw[offset:offset+size] for chunk in track.chunks
            for offset, size in zip(accumulate(chunk.sizes, initial=chunk.offset), chunk.sizes)]


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
class OrdinaryClipTests(unittest.TestCase):
    def test_signed_composition_offsets_and_partial_chunk(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'signed-cts.mp4'
            subprocess.run(
                ['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-f', 'lavfi',
                 '-i', 'testsrc2=size=160x96:rate=12:duration=3', '-c:v', 'libx264',
                 '-preset', 'ultrafast', '-g', '12', '-bf', '2', '-threads', '1',
                 '-movflags', '+negative_cts_offsets', str(source)],
                check=True, timeout=30, stdin=subprocess.DEVNULL, capture_output=True)
            raw = source.read_bytes()
        movie = movie_from(raw)
        clip = OrdinaryClip(movie)
        self.assertEqual(len(clip.track.chunks), 1)
        first, stop = clip.keyframes[1], clip.keyframes[1]+7
        projected = clip.project(first, stop)
        self.assertEqual(sample_bytes(raw, projected.tracks[0]), sample_bytes(raw, clip.track)[first:stop])
        before, after = video_tables(movie), video_tables(projected)
        offsets = sample_values(before[b'ctts'], '>Ii')
        self.assertTrue(any(offset < 0 for offset in offsets[first:stop]))
        self.assertEqual(after[b'ctts'][0], 1)
        self.assertEqual(sample_values(after[b'ctts'], '>Ii'), offsets[first:stop])
        durations = sample_values(before[b'stts'], '>II')
        self.assertEqual(sample_values(after[b'stts'], '>II'), durations[first:stop])
        self.assertEqual(clip.decode_start(first), sum(durations[:first]))
        self.assertEqual(projected.tracks[0].duration, sum(durations[first:stop]))
        self.assertEqual(OrdinaryClip(projected).keyframes, (0,))
        with self.assertRaisesRegex(FormatError, 'nominated sync'):
            clip.project(first+1, stop)

        def without_sync(box):
            if box.kind == b'stss':
                return b''
            if box.kind in {b'moov', b'trak', b'mdia', b'minf', b'stbl'}:
                return make_box(box.kind, b''.join(without_sync(child) for child in children(movie.data, box)))
            return movie.data[box.start:box.end]

        implicit_sync = OrdinaryClip(parse_movie(without_sync(next(boxes(movie.data)))))
        projected = implicit_sync.project(first, stop, sync_samples=[first])
        self.assertEqual(OrdinaryClip(projected).keyframes, (0,))

    def test_projection_omits_audio_and_preserves_video_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'av.mp4'
            generate(source, audio=True, bframes=True)
            raw = source.read_bytes()
        movie = movie_from(raw)
        self.assertEqual(len(movie.tracks), 2)
        clip = OrdinaryClip(movie)
        first, stop = clip.keyframes[1], clip.track.sample_count
        projected = clip.project(first, stop)
        self.assertEqual(tuple(track.handler for track in projected.tracks), (b'vide',))
        self.assertEqual(sample_bytes(raw, projected.tracks[0]), sample_bytes(raw, clip.track)[first:stop])
        self.assertEqual(projected.tracks[0].config_hash, clip.track.config_hash)
        root = next(boxes(projected.data))
        track = one(projected.data, root, b'trak')
        self.assertNotIn(b'edts', [child.kind for child in children(projected.data, track)])
