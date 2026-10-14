from collections import Counter
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.bmff import FormatError, make_box
from videorebuild.cli import main
from videorebuild.fragmented import box_at,parse_initialization,parse_fragment,walk_prefix,inspect_embedded
from videorebuild.image_recovery import export_report,import_report,validate_prefix
from videorebuild.validate import doctor
from fragmented_helpers import generate,repacketize,write_case_inputs


class FragmentedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared=tempfile.TemporaryDirectory(); cls.base=Path(cls.shared.name)
        cls.original,cls.original_prefix=generate(cls.base/'hevc.mp4')
        cls.data=repacketize(cls.original,cls.original_prefix)
        cls.timed=repacketize(cls.original,cls.original_prefix,True)
        cls.init_size=len(cls.original_prefix.initialization.data)
        cls.init=parse_initialization(cls.data[:cls.init_size])
        cls.prefix=walk_prefix(cls.data,cls.init_size)
        cls.tools=doctor()
        raw,p=generate(cls.base/'avc.mp4','avc')
        cls.avc=repacketize(raw,p); cls.avc_init=len(p.initialization.data)

    @classmethod
    def tearDownClass(cls): cls.shared.cleanup()
    def setUp(self): self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def cli(self,args):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()): return main(args)
    def arguments(self,movies=None):
        recovery,image,locations=write_case_inputs(self.root,movies or [self.data],self.init_size)
        args=['recover-fragmented','--image',str(image),'--root',str(recovery),'--case',str(self.root/'case'), '--diagnostic-windows','0']
        return args,recovery,image,locations
    def altered(self,at,fmt,value,data=None):
        b=bytearray(self.data if data is None else data); struct.pack_into(fmt,b,at,value); return bytes(b)

    def test_initialization_accepts_empty_tables(self):
        self.assertEqual(set(self.init.tracks),{1,2}); self.assertEqual(self.init.tracks[1].timescale,90000)
    def test_report_relative_basename_uses_report_directory(self):
        args,recovery,image,locations=self.arguments()
        report=recovery/'recup_dir.1/report.xml'
        original=report.read_text()
        name=locations[0][1]
        report.write_text(original.replace('/old/mount/recup_dir.1/'+name,name))
        image_before=image.read_bytes()
        self.assertEqual(self.cli(args),0)
        with contextlib.closing(sqlite3.connect(self.root/'case/fragmented.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT path FROM anchors').fetchone()[0],'recup_dir.1/'+name)
        self.assertEqual(image.read_bytes(),image_before)
        output=next((self.root/'case/recovered').glob('*.mp4'))
        self.assertEqual(output.read_bytes(),self.data)
    def test_no_tfdt_complete_stream(self):
        self.assertIsNone(self.prefix.eligibility_error())
        self.assertEqual(self.prefix.counts,{1:10,2:9})
        self.assertEqual(self.prefix.end,len(self.data))
        self.assertTrue(all(v is None for f in self.prefix.fragments for v in f.decode_times.values()))
    def test_observed_76_and_80_byte_moofs(self):
        lengths=Counter(box_at(self.data,f.start).end-f.start for f in self.prefix.fragments)
        self.assertEqual(lengths,{80:10,76:9})
    def test_explicit_tfdt_complete_stream(self):
        p=walk_prefix(self.timed,self.init_size)
        self.assertEqual(p.counts,self.prefix.counts); self.assertIsNone(p.eligibility_error())
    def test_prefix_sample_budget_preserves_complete_prefix(self):
        original=bytearray(self.timed); before=bytes(original)
        complete=walk_prefix(before,self.init_size)
        with patch('videorebuild.fragmented.MAX_PREFIX_SAMPLES',12,create=True):
            bounded=walk_prefix(original,self.init_size)
        self.assertEqual(sum(bounded.counts.values()),12)
        self.assertEqual(bounded.fragments,complete.fragments[:12])
        self.assertEqual(bounded.end,complete.fragments[11].end)
        self.assertEqual(bounded.stop_offset,complete.fragments[12].start)
        self.assertEqual(bounded.stop_reason,'prefix_sample_budget_exceeded')
        expected_clocks={}
        for fragment in complete.fragments[:12]:
            for sample in fragment.samples:
                expected_clocks[sample.track_id]=expected_clocks.get(sample.track_id,0)+sample.duration
        self.assertEqual(bounded.clocks,expected_clocks)
        self.assertEqual(bounded.starts,{1:0,2:0})
        self.assertIsNone(bounded.eligibility_error())
        self.assertEqual(bytes(original),before)
    def test_prefix_sample_budget_accepts_exact_boundary(self):
        with patch('videorebuild.fragmented.MAX_PREFIX_SAMPLES',19,create=True):
            p=walk_prefix(self.data,self.init_size)
        self.assertEqual(p.counts,self.prefix.counts)
        self.assertEqual(p.end,len(self.data))
        self.assertEqual(p.stop_reason,'end_of_bounded_window')
    def test_prefix_sample_budget_rejects_whole_multitrack_fragment(self):
        with patch('videorebuild.fragmented.MAX_PREFIX_SAMPLES',18,create=True):
            p=walk_prefix(self.original,self.init_size)
        self.assertFalse(p.fragments)
        self.assertEqual(p.end,self.init_size)
        self.assertFalse(p.clocks)
        self.assertEqual(p.stop_reason,'prefix_sample_budget_exceeded')
    def test_no_tfdt_decode_and_encoded_hashes(self):
        f=self.root/'clip.mp4'; f.write_bytes(self.data)
        v=validate_prefix(f,self.data,self.prefix,self.tools,30)
        self.assertTrue(v['ok'],v)
    def test_explicit_tfdt_decode_and_encoded_hashes(self):
        f=self.root/'clip.mp4'; f.write_bytes(self.timed); p=walk_prefix(self.timed,self.init_size)
        self.assertTrue(validate_prefix(f,self.timed,p,self.tools,30)['ok'])
    def test_avc_decode(self):
        f=self.root/'clip.mp4'; f.write_bytes(self.avc); p=walk_prefix(self.avc,self.avc_init)
        self.assertTrue(validate_prefix(f,self.avc,p,self.tools,30)['ok'])
    def test_multitrack_single_fragment(self):
        self.assertEqual(self.original_prefix.counts,{1:10,2:9})
        self.assertEqual(len(self.original_prefix.fragments),1)
    def test_ordinary_sample_tables_not_treated_as_init(self):
        b=self.altered(self.data.index(b'stsz')+12,'>I',1)
        with self.assertRaises(FormatError): parse_initialization(b[:self.init_size])
    def test_external_dref_rejected(self):
        b=self.altered(self.data.index(b'url ')+4,'>I',0)
        with self.assertRaises(FormatError): parse_initialization(b[:self.init_size])
    def test_truncated_initialization(self):
        with self.assertRaises(FormatError): parse_initialization(self.data[:self.init_size-1])
    def test_open_ended_box_rejected(self):
        with self.assertRaises(FormatError): box_at(b'\0\0\0\0mdatdata',0)
    def test_sequence_gap_stops_prefix(self):
        f=self.prefix.fragments[2]; b=self.altered(f.start+20,'>I',99)
        p=walk_prefix(b,self.init_size)
        self.assertEqual(len(p.fragments),2); self.assertIn('sequence_gap',p.stop_reason)
    def test_orphan_sequence_cannot_start_recording(self):
        b=self.data[:self.init_size]+self.data[self.prefix.fragments[1].start:]
        p=walk_prefix(b,self.init_size)
        self.assertEqual(len(p.fragments),0); self.assertIn('missing_initial_fragments',p.stop_reason)
    def test_non_idr_start_rejected(self):
        s=self.prefix.fragments[0].samples[0]
        b=bytearray(self.data); b[s.offset+4]=2 # type 1 HEVC, same framing
        p=walk_prefix(bytes(b),self.init_size)
        self.assertIn('not_an_IDR',p.eligibility_error())
    def test_missing_audio_not_silently_dropped(self):
        end=self.prefix.fragments[9].end
        p=walk_prefix(self.data[:end],self.init_size)
        self.assertIn('track_2',p.eligibility_error())
    def test_tfdt_jump_stops(self):
        p=walk_prefix(self.timed,self.init_size); f=p.fragments[1]
        ix=self.timed.index(b'tfdt',f.start,f.end)
        b=self.altered(ix+8,'>Q',999999,data=self.timed)
        q=walk_prefix(b,self.init_size)
        self.assertEqual(len(q.fragments),1); self.assertIn('decode_time_discontinuity',q.stop_reason)
    def test_bad_NAL_size_rejected(self):
        f=self.prefix.fragments[0]; s=f.samples[0]
        b=self.altered(s.offset,'>I',0x7fffffff)
        with self.assertRaises(FormatError): parse_fragment(b,f.start,self.init)
    def test_unknown_track_rejected(self):
        f=self.prefix.fragments[0]; ix=self.data.index(b'tfhd',f.start,f.end)
        b=self.altered(ix+8,'>I',44)
        with self.assertRaises(FormatError): parse_fragment(b,f.start,self.init)
    def test_unknown_trun_flags_rejected(self):
        f=self.prefix.fragments[0]; ix=self.data.index(b'trun',f.start,f.end)
        b=self.altered(ix+4,'>I',0x800301)
        with self.assertRaises(FormatError): parse_fragment(b,f.start,self.init)
    def test_absolute_base_rejected(self):
        f=self.prefix.fragments[0]; ix=self.data.index(b'tfhd',f.start,f.end)
        b=self.altered(ix+4,'>I',0x21)
        with self.assertRaises(FormatError): parse_fragment(b,f.start,self.init)
    def test_sample_cannot_escape_mdat(self):
        f=self.prefix.fragments[0]; ix=self.data.index(b'trun',f.start,f.end)
        b=self.altered(ix+12,'>i',0)
        with self.assertRaises(FormatError): parse_fragment(b,f.start,self.init)
    def test_truncation_each_box_header_byte(self):
        f=self.prefix.fragments[0]; moof=box_at(self.data,f.start)
        for n in range(f.start,min(moof.end+16,f.end)):
            with self.subTest(length=n):
                p=walk_prefix(self.data[:n],self.init_size)
                self.assertEqual(len(p.fragments),0)
    def test_embedded_pairs_ignore_false_outer_mdat_length(self):
        b=make_box(b'mdat',b'noise'*11+self.data[self.init_size:])
        r=inspect_embedded(b,self.init)
        self.assertEqual(len(r['valid_fragments']),19)
    def test_truncated_embedded_fragment_recorded(self):
        r=inspect_embedded(self.data[self.init_size:-10],self.init)
        self.assertEqual(len(r['valid_fragments']),18); self.assertEqual(len(r['rejected_candidates']),1)
    def test_random_signature_not_accepted(self):
        r=inspect_embedded(b'\0\0\0\x10moof'+b'fake'*100,self.init)
        self.assertFalse(r['valid_fragments']); self.assertTrue(r['rejected_candidates'])
    def test_decode_stderr_rejects_zero_exit_code(self):
        f=self.root/'clip.mp4'; f.write_bytes(self.data)
        response={'returncode':0,'timeout':False,'stderr':'missing reference','stdout':'{}','stdout_truncated':False}
        with patch('videorebuild.image_recovery.run_tool',return_value=response):
            self.assertFalse(validate_prefix(f,self.data,self.prefix,self.tools,30)['ok'])
    def test_timeout_rejected(self):
        f=self.root/'clip.mp4'; f.write_bytes(self.data)
        response={'returncode':-9,'timeout':True,'stderr':'','stdout':'','stdout_truncated':False}
        with patch('videorebuild.image_recovery.run_tool',return_value=response):
            self.assertFalse(validate_prefix(f,self.data,self.prefix,self.tools,30)['ok'])
    def test_image_recovery_identical_headers_stay_separate(self):
        args,rec,image,locs=self.arguments([self.data,self.data])
        before=image.read_bytes()
        self.assertEqual(self.cli(args+['--all']),0)
        outputs=list((self.root/'case/recovered').glob('*.mp4'))
        self.assertEqual(len(outputs),2)
        self.assertTrue(all(p.read_bytes()==self.data for p in outputs))
        self.assertEqual(image.read_bytes(),before)
        summary=json.loads((self.root/'case/reports/summary.json').read_text())
        self.assertEqual(summary['status_counts'],{'decoded_prefix':2})
    def test_pilot_and_resume(self):
        args,_,_,_=self.arguments([self.data,self.data])
        self.assertEqual(self.cli(args+['--limit','1']),0)
        self.assertEqual(len(list((self.root/'case/recovered').glob('*.mp4'))),1)
        self.assertEqual(self.cli(args+['--all']),0)
        self.assertEqual(len(list((self.root/'case/recovered').glob('*.mp4'))),2)
        self.assertEqual(self.cli(args+['--all']),0)
    def test_success_exports_complete_catalogue_once(self):
        args,_,_,_=self.arguments([self.data,self.data])
        with patch('videorebuild.image_recovery.export_report',wraps=export_report) as exporter:
            self.assertEqual(self.cli(args+['--all']),0)
        self.assertEqual(exporter.call_count,1)
        records=[json.loads(line) for line in (self.root/'case/reports/results.jsonl').read_text().splitlines()]
        self.assertEqual(len(records),2)
        self.assertTrue(all(record['status']=='decoded_prefix' for record in records))
    def test_interruption_exports_committed_results_and_resumes_pending(self):
        args,_,_,_=self.arguments([self.data,self.data])
        validations=0
        def interrupt_second(*values,**kwargs):
            nonlocal validations
            validations+=1
            if validations==2:
                raise KeyboardInterrupt()
            return validate_prefix(*values,**kwargs)
        with patch('videorebuild.image_recovery.validate_prefix',side_effect=interrupt_second):
            self.assertEqual(self.cli(args+['--all']),130)
        reports=self.root/'case/reports'
        summary=json.loads((reports/'summary.json').read_text())
        self.assertEqual((summary['processed'],summary['pending']),(1,1))
        records=[json.loads(line) for line in (reports/'results.jsonl').read_text().splitlines()]
        self.assertEqual(len(records),1)
        first_output=next((self.root/'case/recovered').glob('*.mp4'))
        before=first_output.read_bytes()
        self.assertEqual(self.cli(args+['--all']),0)
        self.assertEqual(first_output.read_bytes(),before)
        outputs=list((self.root/'case/recovered').glob('*.mp4'))
        self.assertEqual(len(outputs),2)
        self.assertTrue(all(output.read_bytes()==self.data for output in outputs))
        summary=json.loads((reports/'summary.json').read_text())
        self.assertEqual((summary['processed'],summary['pending']),(2,0))
    def test_export_failure_retries_catalogue_during_cleanup(self):
        args,_,_,_=self.arguments()
        exports=0
        def fail_first(*values,**kwargs):
            nonlocal exports
            exports+=1
            if exports==1:
                raise OSError('synthetic report write interruption')
            return export_report(*values,**kwargs)
        with patch('videorebuild.image_recovery.export_report',side_effect=fail_first):
            self.assertEqual(self.cli(args),2)
        self.assertEqual(exports,2)
        reports=self.root/'case/reports'
        summary=json.loads((reports/'summary.json').read_text())
        self.assertEqual((summary['processed'],summary['pending']),(1,0))
        records=[json.loads(line) for line in (reports/'results.jsonl').read_text().splitlines()]
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['status'],'decoded_prefix')
    def test_changed_source_refuses_resume(self):
        args,_,image,_=self.arguments(); self.assertEqual(self.cli(args),0)
        b=bytearray(image.read_bytes()); b[-1]=42; image.write_bytes(b)
        self.assertEqual(self.cli(args),2)
    def test_missing_published_prefix_refuses_resume(self):
        args,_,_,_=self.arguments([self.data,self.data])
        self.assertEqual(self.cli(args+['--limit','1']),0)
        output=next((self.root/'case/recovered').glob('*.mp4'))
        output.unlink()
        self.assertEqual(self.cli(args+['--all']),2)
        self.assertFalse(list((self.root/'case/recovered').glob('*.mp4')))
        with contextlib.closing(sqlite3.connect(self.root/'case/fragmented.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM results').fetchone()[0],1)
    def test_changed_published_prefix_refuses_resume(self):
        args,_,_,_=self.arguments(); self.assertEqual(self.cli(args),0)
        output=next((self.root/'case/recovered').glob('*.mp4'))
        raw=bytearray(output.read_bytes()); raw[-1]^=1; output.write_bytes(raw)
        self.assertEqual(self.cli(args),2)
        self.assertEqual(output.read_bytes(),raw)
    def test_truncated_published_prefix_refuses_resume(self):
        args,_,_,_=self.arguments(); self.assertEqual(self.cli(args),0)
        output=next((self.root/'case/recovered').glob('*.mp4'))
        output.write_bytes(b'corrupt')
        self.assertEqual(self.cli(args),2)
        self.assertEqual(output.read_bytes(),b'corrupt')
    def test_missing_published_evidence_refuses_resume(self):
        args,_,_,_=self.arguments(); self.assertEqual(self.cli(args),0)
        next((self.root/'case/attempts').glob('*/result.json')).unlink()
        self.assertEqual(self.cli(args),2)
    def test_wrong_image_header_refuses(self):
        args,_,image,locs=self.arguments(); b=bytearray(image.read_bytes()); b[locs[0][0]+16]=42; image.write_bytes(b)
        self.assertEqual(self.cli(args),2)
    def test_missing_header_hard_errors(self):
        args,rec,_,_=self.arguments(); next(rec.glob('recup_dir.*/*.mp4')).unlink()
        self.assertEqual(self.cli(args),2)
    def test_wrong_image_size_hard_errors(self):
        args,_,image,_=self.arguments(); image.write_bytes(image.read_bytes()+b'x')
        self.assertEqual(self.cli(args),2)
    def test_device_node_refused(self):
        args,_,_,_=self.arguments(); args[args.index('--image')+1]='/dev/null'
        self.assertEqual(self.cli(args),2)
    def test_output_inside_source_refused(self):
        args,rec,_,_=self.arguments(); args[args.index('--case')+1]=str(rec/'case')
        self.assertEqual(self.cli(args),2); self.assertFalse((rec/'case').exists())
    def test_nonfinite_timeout_rejected(self):
        args,_,_,_=self.arguments(); self.assertEqual(self.cli(args+['--timeout','nan']),2)
    def test_blocked_run_returns_three_and_bounded_window(self):
        movie=self.data[:self.init_size]+b'corrupt data'+b'\0'*100
        args,_,image,_=self.arguments([movie]); args[args.index('--diagnostic-windows')+1]='1'
        self.assertEqual(self.cli(args+['--diagnostic-window-bytes','256']),3)
        files=list((self.root/'case/blocked-windows').glob('*.bin'))
        self.assertEqual(len(files),1); self.assertEqual(files[0].stat().st_size,256)
    def test_xml_entities_rejected(self):
        args,rec,_,_=self.arguments(); p=rec/'recup_dir.1/report.xml'
        p.write_text('<!DOCTYPE doc [<!ENTITY e "bad">]><doc>&e;</doc>')
        self.assertEqual(self.cli(args),2)
    def test_lost_commit_existing_output_exact_bytes_resumes(self):
        args,_,_,_=self.arguments(); self.assertEqual(self.cli(args),0)
        with contextlib.closing(sqlite3.connect(self.root/'case/fragmented.sqlite3')) as db:
            db.execute('DELETE FROM results'); db.commit()
        self.assertEqual(self.cli(args),0)
    def test_untrusted_existing_case_refused(self):
        args,_,_,_=self.arguments(); case=self.root/'case'; case.mkdir(); (case/'important.txt').write_text('keep')
        self.assertEqual(self.cli(args),2); self.assertEqual((case/'important.txt').read_text(),'keep')


if __name__=='__main__': unittest.main()
