"""Ordinary MP4 salvage through scan, image extents, and the public CLI."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from helpers import generate, movie_from, sha
from videorebuild import ordinary_salvage
from videorebuild import validate as validation
from videorebuild.bmff import iter_sample_nals, valid_sample
from videorebuild.cli import main
from videorebuild.storage import Case


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),
                     'FFmpeg/ffprobe required for integration tests')
class OrdinarySalvageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        cls.original = root / 'original.mp4'
        generate(cls.original, faststart=True, bframes=True, seconds=5)
        cls.pristine = cls.original.read_bytes()
        cls.source_probe = cls.probe(cls.original)
        cls.video = next(stream for stream in cls.source_probe['streams']
                         if stream['codec_type'] == 'video')
        cls.samples = [packet for packet in cls.source_probe['packets']
                       if packet['stream_index'] == cls.video['index']]

    @staticmethod
    def probe(path):
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-ignore_editlist', '1', '-show_packets',
             '-show_streams', '-show_data_hash', 'sha256', '-show_entries',
             'stream=index,id,codec_type,time_base:packet=stream_index,pos,size,dts,pts,duration,flags,data_hash',
             '-of', 'json', str(path)], stdin=subprocess.DEVNULL, capture_output=True,
            timeout=30, check=True)
        if result.stderr:
            raise AssertionError(result.stderr.decode(errors='replace'))
        return json.loads(result.stdout)

    def cli(self, *args, expected_status=0):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            status = main(['--no-progress', *map(str, args)])
        self.assertEqual(status, expected_status, error.getvalue() + output.getvalue())

    def fixture(self, *, damage=None):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.assertEqual(len(self.samples), 60)
        self.assertEqual([i for i, sample in enumerate(self.samples) if 'K' in sample['flags']],
                         [0, 12, 24, 36, 48])
        self.assertTrue(any(sample['pts'] != sample['dts'] for sample in self.samples))
        self.assertTrue(any(s['codec_type'] == 'audio' for s in self.source_probe['streams']))
        stream = bytearray(self.pristine)
        overwritten = int(self.samples[40]['pos'])
        if damage == 'map_and_framing':
            stream[overwritten:overwritten + 4] = b'\xff' * 4
        elif damage == 'decoder':
            overwritten = int(self.samples[4]['pos'])
            stop = overwritten + int(self.samples[4]['size'])
            offset = overwritten
            changed = False
            for nal in iter_sample_nals(self.pristine[overwritten:stop], b'avc1', 4):
                offset += 4
                if nal[0] & 31 == 1:
                    stream[offset] = (nal[0] & 0xe0) | 4
                    changed = True
                offset += len(nal)
            self.assertTrue(changed, 'fixture must contain a non-IDR slice')
            self.assertTrue(valid_sample(bytes(stream[overwritten:stop]), b'avc1', 4))

        # Split inside good packets so accepted witnesses cross mapped extents.
        cuts = [0, *(int(self.samples[i]['pos']) + int(self.samples[i]['size']) // 2
                     for i in (5, 27)), len(stream)]
        disk, mappings = bytearray(4096), []
        for start, end in reversed(list(zip(cuts, cuts[1:]))):
            disk.extend(b'\0' * (512 + (-len(disk) % 512)))
            mappings.append((start, len(disk), end - start))
            disk.extend(stream[start:end])
        image = root / 'disk.img'
        image.write_bytes(disk)
        mapfile = root / 'rescue.map'
        hole = None
        if damage == 'map_and_framing':
            for packet in self.samples[13:24]:
                position, size = int(packet['pos']), int(packet['size'])
                logical, physical, length = next(item for item in mappings
                                                  if item[0] <= position < item[0] + item[2])
                self.assertLessEqual(position + size, logical + length)
                start = physical + position - logical
                candidate = (start + 511) // 512 * 512
                if candidate + 512 <= start + size:
                    hole = candidate, candidate + 512
                    break
            self.assertIsNotNone(hole, 'fixture needs one full sector inside a predicted packet')
            start, end = hole
            mapfile.write_text(f'0 + 1\n0 {start} +\n{start} 512 -\n{end} {len(disk)-end} +\n')
        else:
            mapfile.write_text(f'0 + 1\n0 {len(disk)} +\n')
        document = ET.Element('dfxml')
        source = ET.SubElement(document, 'source')
        ET.SubElement(source, 'image_size').text = str(len(disk))
        file = ET.SubElement(document, 'fileobject')
        ET.SubElement(file, 'filename').text = 'recording.mp4'
        ET.SubElement(file, 'filesize').text = str(len(stream))
        # Model retained filesystem metadata for a deleted file in the damage case.
        ET.SubElement(file, 'alloc').text = '0' if damage else '1'
        runs = ET.SubElement(file, 'byte_runs')
        for logical, physical, length in sorted(mappings):
            ET.SubElement(runs, 'byte_run', file_offset=str(logical), img_offset=str(physical),
                          len=str(length))
        extents = root / 'extents.xml'
        ET.ElementTree(document).write(extents, encoding='utf-8')
        case = root / 'case'
        before = {path: sha(path) for path in (image, extents, mapfile)}
        self.cli('scan', '--case', case, '--image', image, '--extents', extents, '--mapfile', mapfile)
        return case, image, hole, before

    def assert_segment(self, output, evidence, image, disk, hole):
        start, stop = evidence['sample_start'], evidence['sample_stop']
        expected = self.samples[start:stop]
        pictures = self.probe(output)
        self.assertEqual([track['codec_type'] for track in pictures['streams']], ['video'])
        stream = pictures['streams'][0]
        self.assertEqual(stream['time_base'], self.video['time_base'])
        self.assertTrue(evidence['validation']['ok'])
        self.assertEqual(evidence['audio'], 'not_exported')
        self.assertEqual(evidence['association'], 'filesystem_metadata_not_historical_identity')
        self.assertEqual(evidence['source_moov_sha256'], movie_from(self.pristine).digest)
        self.assertEqual(evidence['source_track_id'], int(self.video['id'], 0))
        self.assertEqual(evidence['source_decode_start'], expected[0]['dts'])
        self.assertEqual(evidence['track_timescale'], int(self.video['time_base'].split('/')[1]))
        self.assertEqual(len(pictures['packets']), len(expected))
        self.assertEqual(pictures['packets'][0]['dts'], 0)
        for packet, original in zip(pictures['packets'], expected):
            self.assertEqual(packet['data_hash'], original['data_hash'])
            self.assertEqual(packet['duration'], original['duration'])
            self.assertEqual(packet['dts'], original['dts'] - expected[0]['dts'])
            self.assertEqual(packet['pts'], original['pts'] - expected[0]['dts'])

        data = output.read_bytes()
        cursor = 0
        crossed_extent = False
        for entry in evidence['ordered_fragments']:
            self.assertEqual(entry['output_offset'], cursor)
            copied = data[cursor:cursor + entry['length']]
            self.assertEqual(hashlib.sha256(copied).hexdigest(), entry['sha256'])
            cursor += entry['length']
            physical = entry['physical_extents']
            if entry['generated']:
                self.assertEqual(physical, [])
                continue
            self.assertTrue(physical)
            crossed_extent |= len(physical) > 1
            source_parts = []
            for extent in physical:
                self.assertEqual(Path(extent['source_path']), image.resolve())
                self.assertEqual(extent['acquisition'], 'rescued')
                left = extent['source_offset']
                right = left + extent['length']
                if hole:
                    self.assertTrue(right <= hole[0] or left >= hole[1])
                source_parts.append(disk[left:right])
            self.assertEqual(b''.join(source_parts), copied)
        self.assertEqual(cursor, len(data))
        return crossed_extent

    def test_image_gaps_publish_only_verified_video_intervals_and_resume_without_decoding(self):
        case, image, hole, before = self.fixture(damage='map_and_framing')
        self.cli('salvage', '--case', case, '--timeout', 30, '--max-attempts', 3)
        results = json.loads((case / 'reports' / 'results.json').read_text())
        jobs = [job for job in results['jobs'] if job['kind'] == 'ordinary_salvage']
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['status'], 'salvaged_segments')
        self.assertFalse(list((case / 'reconstructed').glob('*.mp4')))
        outputs = sorted((case / 'salvaged').glob('ordinary-*.mp4'))
        self.assertEqual(len(outputs), 3)
        intervals = []
        crossed_extent = False
        disk = image.read_bytes()
        for output in outputs:
            evidence = json.loads(output.with_suffix('.json').read_text())
            intervals.append((evidence['sample_start'], evidence['sample_stop']))
            crossed_extent |= self.assert_segment(output, evidence, image, disk, hole)
        self.assertCountEqual(intervals, [(0, 12), (24, 36), (48, 60)])
        self.assertTrue(crossed_extent, 'fixture must exercise a split sample witness')
        self.assertEqual({path: sha(path) for path in before}, before)

        saved = {path.name: (sha(path), path.stat().st_mtime_ns)
                 for path in (case / 'salvaged').iterdir()}
        popen = subprocess.Popen

        def no_decode(argv, *args, **kwargs):
            if Path(argv[0]).name == 'ffmpeg' and '-version' not in argv:
                self.fail('committed salvage was decoded again')
            return popen(argv, *args, **kwargs)

        with patch.object(subprocess, 'Popen', side_effect=no_decode):
            self.cli('salvage', '--case', case, '--timeout', 30)
        self.assertEqual({path.name: (sha(path), path.stat().st_mtime_ns)
                          for path in (case / 'salvaged').iterdir()}, saved)
        self.assertEqual({path: sha(path) for path in before}, before)

    def test_interrupted_publication_resumes_validated_segment_without_decoding_again(self):
        install = ordinary_salvage.ensure_generated_file
        for suffix in ('.mp4', '.json'):
            with self.subTest(interrupt_before=suffix):
                case, _, _, before = self.fixture(damage='map_and_framing')

                def interrupt(case, path, *args, **kwargs):
                    if Path(path).suffix == suffix:
                        raise KeyboardInterrupt
                    return install(case, path, *args, **kwargs)

                with patch.object(ordinary_salvage, 'ensure_generated_file', side_effect=interrupt):
                    self.cli('salvage', '--case', case, '--timeout', 30, expected_status=130)

                with Case(case) as reopened:
                    job, = list(reopened.get_jobs(ordinary_salvage.KIND))
                    self.assertEqual(job['status'], 'salvage_in_progress')
                    self.assertEqual(job['detail']['outputs'], [])
                    pending = job['detail']['pending_segment']
                    entry = pending['entry']
                    staging = case / pending['stage']
                    self.assertTrue((staging / 'clip.mp4').is_file())
                    self.assertTrue((staging / 'clip.json').is_file())
                media = case / entry['path']
                self.assertEqual(media.exists(), suffix == '.json')
                published = media.stat() if media.exists() else None
                self.assertFalse((case / entry['manifest']).exists())

                with patch.object(ordinary_salvage, 'validate', wraps=ordinary_salvage.validate) as validate:
                    self.cli('salvage', '--case', case, '--timeout', 30)
                self.assertEqual(validate.call_count, 2)
                self.assertEqual(sha(media), entry['sha256'])
                if published is not None:
                    self.assertEqual((media.stat().st_ino, media.stat().st_mtime_ns),
                                     (published.st_ino, published.st_mtime_ns))
                self.assertEqual(sha(case / entry['manifest']), entry['manifest_sha256'])
                self.assertEqual(len(list((case / 'salvaged').glob('ordinary-*.mp4'))), 3)
                self.assertEqual(len(list((case / 'salvaged').glob('ordinary-*.json'))), 3)
                self.assertFalse(staging.exists())
                self.assertEqual({path: sha(path) for path in before}, before)

    def test_published_segment_is_reported_if_next_validation_interrupts(self):
        real_validate = ordinary_salvage.validate
        for replay in (False, True):
            with self.subTest(replay=replay):
                case, _, _, _ = self.fixture(damage='map_and_framing')
                if replay:
                    with patch.object(ordinary_salvage, 'ensure_generated_file', side_effect=KeyboardInterrupt):
                        self.cli('salvage', '--case', case, '--timeout', 30, expected_status=130)
                validations = 0

                def interrupt_later(*args, **kwargs):
                    nonlocal validations
                    validations += 1
                    if replay or validations == 2:
                        raise KeyboardInterrupt
                    return real_validate(*args, **kwargs)

                with patch.object(ordinary_salvage, 'validate', side_effect=interrupt_later):
                    self.cli('salvage', '--case', case, '--timeout', 30, expected_status=130)
                self.assertEqual(validations, 1 if replay else 2)
                report = json.loads((case / 'reports' / 'results.json').read_text())
                job, = [job for job in report['jobs'] if job['kind'] == ordinary_salvage.KIND]
                self.assertEqual(job['status'], 'salvage_in_progress')
                entry, = job['detail']['segments']
                self.assertEqual(job['detail']['outputs'], [entry['path']])
                self.assertNotIn('pending_segment', job['detail'])
                self.assertEqual(sha(case / entry['path']), entry['sha256'])
                self.assertEqual(sha(case / entry['manifest']), entry['manifest_sha256'])

    def test_pristine_mapped_video_can_retry_interrupted_validation_without_exporting(self):
        case, _, _, before = self.fixture()
        real_tool = validation.run_tool

        def interrupt_decode(argv, *args, **kwargs):
            if Path(argv[0]).name == 'ffmpeg' and '-xerror' in argv:
                return {'returncode': -9, 'timeout': False, 'stderr': ''}
            return real_tool(argv, *args, **kwargs)

        with patch.object(validation, 'run_tool', side_effect=interrupt_decode):
            self.cli('salvage', '--case', case, '--timeout', 30)
        results = json.loads((case / 'reports' / 'results.json').read_text())
        self.assertEqual([job['status'] for job in results['jobs']
                          if job['kind'] == 'ordinary_salvage'], ['salvage_search_limited'])

        self.cli('salvage', '--case', case, '--timeout', 30, '--retry')
        self.assertFalse(list((case / 'salvaged').glob('*.mp4')))
        results = json.loads((case / 'reports' / 'results.json').read_text())
        self.assertEqual([job['status'] for job in results['jobs']
                          if job['kind'] == 'ordinary_salvage'], ['salvage_not_needed'])
        self.assertEqual({path: sha(path) for path in before}, before)

    def test_retry_after_decoder_limit_splits_framed_recording_at_intact_idrs(self):
        case, image, _, before = self.fixture(damage='decoder')
        decode_codes = []
        real_tool = validation.run_tool

        def observe_tool(argv, *args, **kwargs):
            result = real_tool(argv, *args, **kwargs)
            if Path(argv[0]).name == 'ffmpeg' and '-xerror' in argv:
                decode_codes.append(result['returncode'])
            return result

        with patch.object(validation, 'run_tool', side_effect=observe_tool):
            for attempt in range(5):
                retry = ['--retry'] if attempt else []
                self.cli('salvage', '--case', case, '--timeout', 30, '--max-attempts', 1, *retry)
                self.assertEqual(len(decode_codes), attempt + 1)
                self.assertEqual(decode_codes[-1] != 0, attempt < 3)
                results = json.loads((case / 'reports' / 'results.json').read_text())
                expected = 'salvage_search_limited' if attempt < 4 else 'salvaged_segments'
                self.assertEqual([job['status'] for job in results['jobs']
                                  if job['kind'] == 'ordinary_salvage'], [expected])
                if attempt < 3:
                    self.assertFalse(list((case / 'salvaged').glob('*.mp4')))
        disk = image.read_bytes()
        verified = []
        for output in (case / 'salvaged').glob('ordinary-*.mp4'):
            evidence = json.loads(output.with_suffix('.json').read_text())
            self.assert_segment(output, evidence, image, disk, None)
            verified.extend(range(evidence['sample_start'], evidence['sample_stop']))
        self.assertCountEqual(verified, range(12, 60))
        self.assertEqual({path: sha(path) for path in before}, before)
