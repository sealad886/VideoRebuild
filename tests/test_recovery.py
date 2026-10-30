from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.bmff import boxes, parse_movie, rewrite_offsets, FormatError
from videorebuild.cli import main, report
from videorebuild.engine import Options, recover
from videorebuild.storage import Case, ReaderPool, SafetyError, scan, scan_source
from videorebuild.validate import doctor, run_tool
from helpers import generate, movie_from, payload, scatter, sha, split


class DoctorTests(unittest.TestCase):
    def tool_versions(self, ffmpeg_version, ffprobe_version):
        versions = {'ffmpeg': ffmpeg_version, 'ffprobe': ffprobe_version}
        def version_result(args, timeout):
            name = Path(args[0]).name
            return {'returncode': 0, 'timeout': False, 'stderr': '',
                    'stdout': f'{name} version {versions[name]} Copyright FFmpeg developers\n'}
        with patch('videorebuild.validate.shutil.which', side_effect=lambda name: '/tools/'+name), \
                patch('videorebuild.validate.run_tool', side_effect=version_result):
            return doctor()

    def test_supported_release_versions(self):
        for version in ('8.0', '8.0.1-3ubuntu1', 'n8.0.1', '9.0.2', '9.0.2+local'):
            with self.subTest(version=version):
                tools = self.tool_versions(version, version)
                self.assertEqual(set(tools), {'ffmpeg', 'ffprobe'})

    def test_each_tool_requires_version_eight_or_newer(self):
        for versions in (('7.1.5', '8.0.1'), ('8.0.1', '7.1.5')):
            with self.subTest(versions=versions):
                with self.assertRaises(SafetyError):
                    self.tool_versions(*versions)

    def test_unidentifiable_versions_fail_explicitly(self):
        for version in ('N-123456-gabcdef', 'unknown', '', '8.invalid'):
            with self.subTest(version=version):
                with self.assertRaises(SafetyError):
                    self.tool_versions(version, version)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/ffprobe required for integration tests')
class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures=tempfile.TemporaryDirectory()
        cls.base=Path(cls.fixtures.name)
        cls.h264=cls.base/'h264.mp4'; generate(cls.h264)
        cls.hevc=cls.base/'hevc.mp4'; generate(cls.hevc,codec='hevc')
        cls.fast=cls.base/'fast.mp4'; generate(cls.fast,faststart=True,bframes=True)
        cls.silent=cls.base/'silent.mp4'; generate(cls.silent,audio=False,bframes=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_case(self, inputs=(), images=(), opt=None):
        with Case(self.root/'case') as c:
            scan(c,list(inputs),list(images))
            recover(c,opt or Options())
            report(c)
            rows=[dict(r) for r in c.db.execute('SELECT * FROM results')]
        return rows

    def assert_recovered(self, rows, original):
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'matched',rows)
        d=json.loads(rows[0]['detail'])
        self.assertEqual(len(d['outputs']),1)
        out=self.root/'case'/d['outputs'][0]
        self.assertEqual(payload(out),payload(original))
        self.assertTrue(d['candidates'][0]['validation']['ok'])
        return out

    def test_split_avc_aac_with_wrong_decoy(self):
        split(self.h264,self.root/'input',decoy=True)
        before={p.name:sha(p) for p in (self.root/'input').iterdir()}
        rows=self.run_case([self.root/'input'])
        self.assert_recovered(rows,self.h264)
        self.assertEqual(before,{p.name:sha(p) for p in (self.root/'input').iterdir()})

    def test_split_hevc_aac(self):
        split(self.hevc,self.root/'input')
        self.assert_recovered(self.run_case([self.root/'input']),self.hevc)

    def test_faststart_bframes_intact(self):
        self.assert_recovered(self.run_case([self.fast]),self.fast)

    def test_duplicate_payload_is_not_ambiguity(self):
        split(self.h264,self.root/'input',duplicate=True)
        self.assert_recovered(self.run_case([self.root/'input']),self.h264)

    def test_different_valid_payloads_are_ambiguous(self):
        split(self.h264,self.root/'input')
        p=self.root/'input'/'f000040_mdat.mov'
        data=p.read_bytes()
        self.assertIn(b'x264',data)
        other=data.replace(b'x264',b'y264',1)  # Same-length ignored SEI text, still valid media.
        (self.root/'input'/'f888_mdat.mov').write_bytes(other)
        rows=self.run_case([self.root/'input'])
        self.assertEqual(rows[0]['status'],'ambiguous')
        self.assertFalse(list((self.root/'case'/'reconstructed').glob('*.mp4')))
        self.assertEqual(len(json.loads(rows[0]['detail'])['candidates']),2)

    def test_candidate_budget_does_not_assert_uniqueness(self):
        split(self.h264,self.root/'input',duplicate=True)
        rows=self.run_case([self.root/'input'],opt=Options(max_candidates=1))
        self.assertEqual(rows[0]['status'],'search_limited')
        self.assertFalse(list((self.root/'case'/'reconstructed').glob('*.mp4')))

    def test_truncated_payload_not_recovered(self):
        split(self.h264,self.root/'input')
        p=self.root/'input'/'f000040_mdat.mov'; p.write_bytes(p.read_bytes()[:-1500])
        rows=self.run_case([self.root/'input'])
        self.assertEqual(rows[0]['status'],'unresolved')

    def test_scattered_image_avc_aac(self):
        image=self.root/'card.img'; scatter(self.h264,image)
        before=sha(image)
        rows=self.run_case(images=[image],opt=Options(deep=True))
        self.assert_recovered(rows,self.h264)
        self.assertEqual(json.loads(rows[0]['detail'])['stage'],'scattered_extents')
        self.assertEqual(sha(image),before)

    def test_scattered_image_hevc_aac(self):
        image=self.root/'card.img'; scatter(self.hevc,image)
        self.assert_recovered(self.run_case(images=[image],opt=Options(deep=True)),self.hevc)

    def test_scattered_separate_files(self):
        scatter(self.h264,self.root/'input',separate_files=True)
        self.assert_recovered(self.run_case([self.root/'input'],opt=Options(deep=True)),self.h264)

    def test_scattered_video_only(self):
        image=self.root/'card.img'; scatter(self.silent,image)
        self.assert_recovered(self.run_case(images=[image],opt=Options(deep=True)),self.silent)

    def test_missing_extent_is_not_invented(self):
        image=self.root/'card.img'; scatter(self.h264,image,drop=True)
        rows=self.run_case(images=[image],opt=Options(deep=True))
        self.assertEqual(rows[0]['status'],'unresolved')
        self.assertFalse(list((self.root/'case'/'reconstructed').glob('*.mp4')))

    def test_anchor_budget_reported(self):
        image=self.root/'card.img'; scatter(self.h264,image)
        rows=self.run_case(images=[image],opt=Options(deep=True,max_anchor_checks=1))
        self.assertEqual(rows[0]['status'],'search_limited')

    def test_group_budget_reported(self):
        image=self.root/'card.img'; scatter(self.h264,image)
        rows=self.run_case(images=[image],opt=Options(deep=True,max_groups=1))
        self.assertEqual(rows[0]['status'],'search_limited')

    def test_source_change_aborts_resume(self):
        split(self.h264,self.root/'input')
        self.run_case([self.root/'input'])
        p=self.root/'input'/'f000040_mdat.mov'; p.write_bytes(p.read_bytes()+b'X')
        with Case(self.root/'case') as c:
            with self.assertRaises(SafetyError):
                scan(c,[self.root/'input'],[])

    def test_candidate_change_aborts_resume(self):
        split(self.h264,self.root/'input')
        out=self.assert_recovered(self.run_case([self.root/'input']),self.h264)
        out.write_bytes(out.read_bytes()+b'X')
        with Case(self.root/'case') as c:
            with self.assertRaises(SafetyError):
                recover(c,Options())

    def test_resume_adds_no_attempts(self):
        split(self.h264,self.root/'input')
        out=self.assert_recovered(self.run_case([self.root/'input']),self.h264)
        before=sha(out)
        with Case(self.root/'case') as c:
            n=c.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]
            with patch('videorebuild.engine.validate',side_effect=AssertionError('cached recovery must not decode again')):
                recover(c,Options())
            self.assertEqual(c.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],n)
        self.assertEqual(sha(out),before)

    def test_missing_publication_aborts_resume(self):
        split(self.h264,self.root/'input')
        out=self.assert_recovered(self.run_case([self.root/'input']),self.h264)
        out.unlink()
        with Case(self.root/'case') as c:
            with self.assertRaisesRegex(SafetyError,'Published output changed or disappeared'):
                recover(c,Options())
        self.assertFalse(out.exists())

    def test_changed_independent_publication_aborts_resume(self):
        import errno
        split(self.h264,self.root/'input')
        with patch('videorebuild.engine.os.link',side_effect=OSError(errno.ENOTSUP,'no hardlinks')):
            rows=self.run_case([self.root/'input'])
        out=self.assert_recovered(rows,self.h264)
        detail=json.loads(rows[0]['detail'])
        candidate=self.root/'case'/detail['candidates'][0]['path']
        candidate_hash=sha(candidate)
        out.write_bytes(b'corrupted publication')
        with Case(self.root/'case') as c:
            with self.assertRaisesRegex(SafetyError,'Published output changed or disappeared'):
                recover(c,Options())
        self.assertEqual(sha(candidate),candidate_hash)
        self.assertEqual(out.read_bytes(),b'corrupted publication')

    def test_new_evidence_invalidates_previous_unique_match(self):
        split(self.h264,self.root/'input')
        self.run_case([self.root/'input'])
        data=(self.root/'input'/'f000040_mdat.mov').read_bytes().replace(b'x264',b'y264',1)
        (self.root/'input'/'f888_mdat.mov').write_bytes(data)
        with Case(self.root/'case') as c:
            scan(c,[self.root/'input'],[])
            recover(c,Options())
            self.assertEqual(c.db.execute('SELECT status FROM results').fetchone()[0],'ambiguous')
        self.assertFalse(list((self.root/'case'/'reconstructed').glob('*.mp4')))

    def test_co64_promotion_above_four_gib(self):
        movie=movie_from(self.h264.read_bytes())
        off={(c.track,c.index): c.offset+(1<<33) for c in movie.chunks}
        rewritten=rewrite_offsets(movie,off)
        parsed=parse_movie(rewritten)
        self.assertEqual([c.offset for c in parsed.chunks],[c.offset+(1<<33) for c in movie.chunks])
        self.assertNotIn(b'stco',rewritten)

    def test_scanner_finds_headers_across_every_block_boundary(self):
        raw=self.h264.read_bytes()
        for shift in range(12):
            with self.subTest(shift=shift):
                p=self.root/f'b{shift}.bin'; p.write_bytes(b'X'*shift+raw)
                with Case(self.root/f'case{shift}') as c:
                    with patch('videorebuild.storage.BLOCK',127):
                        scan_source(c,p,'image')
                    kinds=[r[0] for r in c.db.execute('SELECT kind FROM atoms')]
                    self.assertEqual(kinds.count('moov'),1)
                    self.assertEqual(kinds.count('mdat'),1)
                    self.assertEqual(kinds.count('ftyp'),1)
                    stored=c.db.execute('SELECT sha256 FROM sources').fetchone()[0]
                    self.assertEqual(stored,sha(p))

    def test_external_data_reference_rejected(self):
        m=movie_from(self.h264.read_bytes())
        data=bytearray(m.data)
        at=data.index(b'url ')
        data[at+7]=0  # Self-contained flag removed.
        with self.assertRaisesRegex(FormatError,'external data reference'):
            parse_movie(bytes(data))

    def test_lock_prevents_second_writer(self):
        with Case(self.root/'case'):
            with self.assertRaises(SafetyError):
                with Case(self.root/'case'):
                    pass

    def test_case_inside_input_rejected(self):
        (self.root/'input').mkdir()
        self.assertEqual(main(['scan','--input',str(self.root/'input'),'--case',str(self.root/'input'/'output')]),2)
        self.assertFalse((self.root/'input'/'output').exists())

    def test_image_only_without_metadata_reports_no_movies(self):
        p=self.root/'unknown.img'; p.write_bytes(b'X'*10000)
        rows=self.run_case(images=[p])
        self.assertEqual(rows,[])
        summary=json.loads((self.root/'case'/'reports'/'summary.json').read_text())
        self.assertEqual(summary['movie_metadata_records'],0)

    def test_subprocess_timeout_is_explicit(self):
        import sys
        r=run_tool([sys.executable,'-c','import time; time.sleep(2)'],.02)
        self.assertTrue(r['timeout'])

    def test_negative_padding_rejected(self):
        with self.assertRaises(SafetyError):
            Options(max_padding=-1).check()

    def test_nonfinite_timeout_rejected(self):
        with self.assertRaises(SafetyError):
            Options(timeout=float('inf')).check()

    def test_interrupted_deep_pass_resumes_without_retry_flag(self):
        image=self.root/'card.img'; scatter(self.h264,image)
        with Case(self.root/'case') as c:
            scan(c,[],[image])
            with patch('videorebuild.engine.deep_search',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    recover(c,Options(deep=True))
            self.assertEqual(c.db.execute('SELECT status FROM results').fetchone()[0],'awaiting_deep')
        with Case(self.root/'case') as c:
            recover(c,Options(deep=True))
            rows=[dict(r) for r in c.db.execute('SELECT * FROM results')]
        self.assert_recovered(rows,self.h264)

    def test_scan_only_invalidates_previous_publication(self):
        split(self.h264,self.root/'input')
        self.run_case([self.root/'input'])
        (self.root/'input'/'another.bin').write_bytes(b'no additional metadata')
        with Case(self.root/'case') as c:
            scan(c,[self.root/'input'],[])
            self.assertEqual(c.db.execute('SELECT status FROM results').fetchone()[0],'stale')
        self.assertFalse(list((self.root/'case'/'reconstructed').glob('*.mp4')))

    def test_identical_candidates_share_only_case_internal_storage(self):
        split(self.h264,self.root/'input',duplicate=True)
        self.run_case([self.root/'input'])
        files=list((self.root/'case'/'candidates').glob('*.mp4'))
        self.assertEqual(len(files),2)
        self.assertEqual(files[0].stat().st_ino,files[1].stat().st_ino)
        self.assertNotEqual(files[0].stat().st_ino,(self.root/'input'/'f000040_mdat.mov').stat().st_ino)

    def test_two_recordings_interleaved_in_one_image(self):
        import random
        from videorebuild.engine import groups_for
        pieces=[]; metadata=[]
        for original in (self.h264,self.hevc):
            raw=original.read_bytes(); m=movie_from(raw); metadata.append(m.data)
            pieces.extend(raw[g.start:g.start+g.span] for g in groups_for(m))
        random.Random(901).shuffle(pieces)
        image=self.root/'mixed.img'
        image.write_bytes(b'Q'*111+b''.join(metadata)+b'Z'*123+b''.join(p+b'Z'*997 for p in pieces))
        rows=self.run_case(images=[image],opt=Options(deep=True))
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r['status']=='matched' for r in rows),rows)
        outputs=[self.root/'case'/json.loads(r['detail'])['outputs'][0] for r in rows]
        self.assertEqual({hashlib.sha256(payload(o)).hexdigest() for o in outputs},
                         {hashlib.sha256(payload(o)).hexdigest() for o in (self.h264,self.hevc)})

    def test_mutated_metadata_fails_with_controlled_errors(self):
        import random
        raw=movie_from(self.h264.read_bytes()).data
        rng=random.Random(8723)
        for _ in range(1000):
            changed=bytearray(raw)
            for __ in range(rng.randint(1,3)):
                changed[rng.randrange(len(changed))] ^= rng.randint(1,255)
            try:
                parse_movie(bytes(changed))
            except FormatError:
                pass

    def test_filesystem_without_hardlinks_uses_logged_copy(self):
        import errno
        split(self.h264,self.root/'input',duplicate=True)
        with patch('videorebuild.engine.os.link',side_effect=OSError(errno.ENOTSUP,'no hardlinks')):
            with self.assertLogs('videorebuild.engine',level='WARNING') as logs:
                rows=self.run_case([self.root/'input'])
        out=self.assert_recovered(rows,self.h264)
        self.assertTrue(any('Hard links unavailable' in x for x in logs.output))
        candidate=self.root/'case'/json.loads(rows[0]['detail'])['candidates'][0]['path']
        self.assertNotEqual(out.stat().st_ino,candidate.stat().st_ino)
