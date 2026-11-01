"""Generated AVC ancillary samples reconstruct byte-for-byte without codec tools."""
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from helpers import generate, movie_from
from videorebuild.bmff import boxes, make_box, parse_movie, rewrite_offsets, sample_kind, valid_sample
from videorebuild import engine
from videorebuild.cli import main
from videorebuild.storage import Case, scan_logical
from helpers import payload
import contextlib
import io

SEI = b'\0\0\0\5\6\6\1\xc0\x80'


def ancillary_movie(raw, *, sei=SEI, corrupt_video=False, corrupt_audio=False):
    original = movie_from(raw)
    samples = {t.index: [raw[chunk.offset+sum(chunk.sizes[:i]):chunk.offset+sum(chunk.sizes[:i+1])]
                         for chunk in t.chunks for i in range(len(chunk.sizes))] for t in original.tracks}
    video = next(t for t in original.tracks if t.handler == b'vide')
    insert = 3
    samples[video.index].insert(insert, sei)
    if corrupt_video:
        damaged = bytearray(samples[video.index][1])
        damaged[-min(40, len(damaged)-5):] = b'\xff'*min(40, len(damaged)-5)
        samples[video.index][1] = bytes(damaged)
    if corrupt_audio:
        audio = next(t for t in original.tracks if t.handler == b'soun')
        samples[audio.index][2] = b'\xff'*len(samples[audio.index][2])
    def full(kind, payload):
        return make_box(kind, b'\0'*4+payload)
    offsets = {}
    cursor = 1024*1024
    for track in original.tracks:
        offsets[track.index] = cursor
        cursor += sum(map(len, samples[track.index]))
    containers = {b'moov', b'trak', b'mdia', b'minf', b'stbl'}
    track_index = -1
    def rebuild(box, current=-1):
        nonlocal track_index
        if box.kind == b'trak':
            track_index += 1
            current = track_index
        if box.kind == b'stbl':
            out = []
            values = samples[current]
            for child in boxes(original.data, box.payload, box.end):
                if child.kind == b'stsz':
                    out.append(full(b'stsz', struct.pack('>II', 0, len(values))+b''.join(struct.pack('>I', len(x)) for x in values)))
                elif child.kind == b'stsc':
                    out.append(full(b'stsc', struct.pack('>IIII', 1, 1, len(values), 1)))
                elif child.kind in {b'stco', b'co64'}:
                    out.append(full(b'co64', struct.pack('>IQ', 1, offsets[current])))
                elif child.kind == b'stts' and current == video.index:
                    durations = [duration for count, duration in struct.iter_unpack('>II', original.data[child.payload+8:child.end]) for _ in range(count)]
                    durations.insert(insert, 1)
                    out.append(full(b'stts', struct.pack('>I', len(durations))+b''.join(struct.pack('>II', 1, x) for x in durations)))
                elif child.kind == b'stss' and current == video.index:
                    entries = [int.from_bytes(original.data[p:p+4], 'big') for p in range(child.payload+8, child.end, 4)]
                    out.append(full(b'stss', struct.pack('>I', len(entries))+b''.join(struct.pack('>I', n+(n > insert)) for n in entries)))
                else:
                    out.append(original.data[child.start:child.end])
            return make_box(box.kind, b''.join(out))
        if box.kind in containers:
            return make_box(box.kind, b''.join(rebuild(child, current) for child in boxes(original.data, box.payload, box.end)))
        return original.data[box.start:box.end]
    metadata = rebuild(next(boxes(original.data)))
    movie = parse_movie(metadata)
    ftyp = make_box(b'ftyp', b'isom\0\0\2\0isomiso6')
    cursor = len(ftyp)+len(metadata)+16
    final = {}
    for track in movie.tracks:
        final[(track.index, 0)] = cursor
        cursor += sum(map(len, samples[track.index]))
    metadata = rewrite_offsets(movie, final)
    media = b''.join(b''.join(samples[t.index]) for t in movie.tracks)
    result = ftyp+metadata+struct.pack('>I4sQ', 1, b'mdat', len(media)+16)+media
    return result, samples


class ReconstructionOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        source = cls.root/'generated.mp4'
        generate(source)
        cls.raw = source.read_bytes()

    def test_classifier_accepts_only_complete_sei_and_rejects_malformed_attached_sei(self):
        self.assertEqual(sample_kind(SEI, b'avc1', 4), 'avc_sei')
        self.assertTrue(valid_sample(SEI, b'avc3', 4))
        vcl = b'\0\0\0\2\x65X'
        self.assertEqual(sample_kind(SEI+vcl, b'avc1', 4), 'vcl')
        for bad in (SEI[:-1], SEI[:-1]+b'\0', b'\0\0\0\5\6\6\2\xc0\x80',
                    b'\0\0\0\5\x26\6\1\xc0\x80', b'\0\0\0\2\x09\x80'):
            with self.subTest(bad=bad):
                self.assertIsNone(sample_kind(bad, b'avc1', 4))
                self.assertIsNone(sample_kind(bad+vcl, b'avc1', 4)) if bad != b'\0\0\0\2\x09\x80' else None
        self.assertFalse(valid_sample(b'\0\0\0\3\x4e\1\x80', b'hvc1', 4))

    def test_reconstruct_only_exports_all_packets_without_decoder_tools(self):
        for damage in (False, True):
            with self.subTest(codec_damage=damage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                data, _ = ancillary_movie(self.raw, corrupt_video=damage, corrupt_audio=damage)
                source = root/'source.mp4'
                source.write_bytes(data)
                case_root = root/'case'
                with Case(case_root) as case:
                    scan_logical(case, case.register_source(source))
                    indexed = [tuple(r) for r in case.db.execute('SELECT * FROM sources ORDER BY id')]
                    atoms = [tuple(r) for r in case.db.execute('SELECT * FROM atoms ORDER BY id')]
                with patch('videorebuild.engine.doctor', side_effect=AssertionError('quality tools must not run')), \
                        patch('videorebuild.workflow.doctor', side_effect=AssertionError('quality tools must not run')), \
                        patch('videorebuild.engine.validate', side_effect=AssertionError('decoder must not run')), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['recover','--case',str(case_root),'--reconstruct-only','--retry','--no-progress']), 0)
                with Case(case_root) as case:
                    job = next(case.get_jobs('ordinary'))
                    self.assertEqual(job['status'], 'reconstructed', job)
                    self.assertFalse(job['detail']['decode_checked'])
                    outputs = job['detail']['outputs']
                    self.assertEqual(len(outputs), 1)
                    self.assertEqual(payload(case.root/outputs[0]), payload(source))
                    self.assertEqual(source.read_bytes(), data)
                    candidate = job['detail']['candidates'][0]
                    self.assertEqual(candidate['validation']['scope'], 'structural_reconstruction_only')
                    self.assertFalse(candidate['validation']['decode_checked'])
                    self.assertEqual([tuple(r) for r in case.db.execute('SELECT * FROM sources ORDER BY id')], indexed)
                    self.assertEqual([tuple(r) for r in case.db.execute('SELECT * FROM atoms ORDER BY id')], atoms)

    def test_reconstruction_still_rejects_malformed_sample_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, _ = ancillary_movie(self.raw, sei=SEI[:-1]+b'\0')
            source = root/'bad.mp4'
            source.write_bytes(data)
            with Case(root/'case') as case:
                scan_logical(case, case.register_source(source))
                with patch.object(engine, 'doctor', side_effect=AssertionError('quality tools must not run')), \
                        patch.object(engine, 'validate', side_effect=AssertionError('decoder must not run')):
                    engine.recover(case, engine.Options(reconstruct_only=True))
                self.assertFalse(list((case.root/'reconstructed').glob('*.mp4')))
                self.assertEqual(source.read_bytes(), data)
