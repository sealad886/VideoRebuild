from collections import Counter
from dataclasses import replace
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
from videorebuild.image_recovery import XMLReader,validate_prefix
from videorebuild.storage import SafetyError
from videorebuild.validate import doctor
from fragmented_helpers import generate,repacketize,write_case_inputs


class XMLReportTests(unittest.TestCase):
    encodings = (('utf-8', b'', 'utf-8'),
                 ('utf-16-le', b'\xff\xfe', 'utf-16'),
                 ('utf-16-be', b'\xfe\xff', 'utf-16'))
    body = ('<dfxml><source><image_filename>synthetic.img</image_filename>'
            '<image_size>1024</image_size><sectorsize>512</sectorsize></source>'
            '<fileobject><filename>NAME</filename><filesize>16</filesize>'
            '<byte_runs><byte_run offset="0" img_offset="0" len="16"/>'
            '</byte_runs></fileobject></dfxml>')

    def report_bytes(self, encoding, bom, declaration, dtd=''):
        name = '&recovered;' if dtd else 'f00000016.mp4'
        xml = f'<?xml version="1.0" encoding="{declaration}"?>'+dtd+self.body.replace('NAME', name)
        return bom+xml.encode(encoding)


    def test_reader_preserves_bytes_and_rejects_split_declarations(self):
        for encoding, bom, declaration in self.encodings:
            normal = self.report_bytes(encoding, bom, declaration)
            for chunk_size in (1, 3, 64):
                with self.subTest(encoding=encoding, chunk_size=chunk_size):
                    reader = XMLReader(io.BytesIO(normal))
                    self.assertEqual(b''.join(iter(lambda: reader.read(chunk_size), b'')), normal)
                    for token in ('<!DOCTYPE dfxml>', '<!ENTITY recovered "filename">'):
                        # Padding puts each token across reads, even for the largest chunk.
                        padding = (63-len(bom))//len(' '.encode(encoding))
                        raw = bom+(' '*padding+token).encode(encoding)
                        reader = XMLReader(io.BytesIO(raw))
                        with self.assertRaises(SafetyError):
                            list(iter(lambda: reader.read(chunk_size), b''))


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
        args=['run','--image',str(image),'--input',str(recovery),'--report',str(recovery/'recup_dir.1/report.xml'),'--case',str(self.root/'case')]
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
        self.assertEqual(image.read_bytes(),image_before)
        output=next((self.root/'case/reconstructed').glob('*.mp4'))
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
    def test_validator_rejects_incorrect_parsed_timing(self):
        # Bytes still decode and hash identically; timing is an independent gate.
        f=self.root/'clip.mp4'; f.write_bytes(self.data)
        for field in ('duration','composition_offset'):
            with self.subTest(field=field):
                p=walk_prefix(self.data,self.init_size)
                fragment=p.fragments[0]; sample=fragment.samples[0]
                wrong=replace(sample,**{field:getattr(sample,field)+1})
                p.fragments[0]=replace(fragment,samples=(wrong,)+fragment.samples[1:])
                checked=validate_prefix(f,self.data,p,self.tools,30)
                self.assertFalse(checked['ok'],checked)
                self.assertEqual(checked['reason'],'packet_timing_mismatch')
        p=walk_prefix(self.data,self.init_size); p.starts[1]+=1
        self.assertEqual(validate_prefix(f,self.data,p,self.tools,30)['reason'],'packet_timing_mismatch')

    def test_validator_rejects_incorrect_terminal_sample_duration(self):
        # No subsequent DTS can expose the final sample's wrong duration.
        f=self.root/'clip.mp4'; f.write_bytes(self.data)
        for tid in self.init.tracks:
            with self.subTest(track=tid):
                p=walk_prefix(self.data,self.init_size)
                fi,si=max((fi,si) for fi,frag in enumerate(p.fragments)
                          for si,sample in enumerate(frag.samples) if sample.track_id==tid)
                fragment=p.fragments[fi]; samples=list(fragment.samples)
                samples[si]=replace(samples[si],duration=samples[si].duration+1)
                p.fragments[fi]=replace(fragment,samples=tuple(samples))
                checked=validate_prefix(f,self.data,p,self.tools,30)
                self.assertFalse(checked['ok'],checked)
                self.assertEqual(checked['reason'],'packet_timing_mismatch')

    def test_bframe_timing_with_signed_and_unsigned_composition_offsets(self):
        for codec in ('avc','hevc'):
            for negative in (False,True):
                with self.subTest(codec=codec,negative=negative):
                    f=self.root/f'{codec}-{negative}.mp4'
                    data,p=generate(f,codec,bframes=2,negative_cts=negative,seconds=3)
                    self.assertGreaterEqual(len(p.fragments),3)
                    offsets=[s.composition_offset for frag in p.fragments for s in frag.samples if s.track_id==1]
                    self.assertGreater(max(offsets),0)
                    if negative: self.assertLess(min(offsets),0)
                    checked=validate_prefix(f,data,p,self.tools,30)
                    self.assertTrue(checked['ok'],checked)
                    # Reordered presentation must not hide the final decode
                    # sample's duration: no next DTS exists to cross-check it.
                    fi,si=max((fi,si) for fi,frag in enumerate(p.fragments)
                              for si,sample in enumerate(frag.samples) if sample.track_id==1)
                    fragment=p.fragments[fi]; samples=list(fragment.samples)
                    samples[si]=replace(samples[si],duration=samples[si].duration+1)
                    p.fragments[fi]=replace(fragment,samples=tuple(samples))
                    checked=validate_prefix(f,data,p,self.tools,30)
                    self.assertFalse(checked['ok'],checked)
                    self.assertEqual(checked['reason'],'packet_timing_mismatch')

    def test_nonzero_per_track_tfdt_preserves_packet_timing(self):
        data=bytearray(self.timed)
        # Offset each track independently while preserving all durations.
        starts={1:90000,2:16000}
        for fragment in walk_prefix(self.timed,self.init_size).fragments:
            at=data.index(b'tfdt',fragment.start,fragment.end)+8
            tid=fragment.samples[0].track_id
            struct.pack_into('>Q',data,at,fragment.decode_times[tid]+starts[tid])
        p=walk_prefix(data,self.init_size)
        self.assertEqual(p.starts,starts)
        f=self.root/'nonzero.mp4'; f.write_bytes(data)
        checked=validate_prefix(f,data,p,self.tools,30)
        self.assertTrue(checked['ok'],checked)
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
        self.assertEqual(self.cli(args),0)
        outputs=list((self.root/'case/reconstructed').glob('*.mp4'))
        self.assertEqual(len(outputs),2)
        self.assertTrue(all(p.read_bytes()==self.data for p in outputs))
        self.assertEqual(image.read_bytes(),before)
        summary=json.loads((self.root/'case/reports/summary.json').read_text())
        self.assertEqual(summary['status_counts'],{'decoded_prefix':2})
    def test_pilot_and_resume(self):
        args,_,_,_=self.arguments([self.data,self.data])
        self.assertEqual(self.cli(args+['--limit','1']),0)
        self.assertEqual(len(list((self.root/'case/reconstructed').glob('*.mp4'))),1)
        self.assertEqual(self.cli(args),0)
        self.assertEqual(len(list((self.root/'case/reconstructed').glob('*.mp4'))),2)
        self.assertEqual(self.cli(args),0)
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
            self.assertEqual(self.cli(args),130)
        reports=self.root/'case/reports'
        records=json.loads((reports/'results.json').read_text())['jobs']
        self.assertEqual(len(records),1)
        first_output=next((self.root/'case/reconstructed').glob('*.mp4'))
        before=first_output.read_bytes()
        self.assertEqual(self.cli(args),0)
        self.assertEqual(first_output.read_bytes(),before)
        outputs=list((self.root/'case/reconstructed').glob('*.mp4'))
        self.assertEqual(len(outputs),2)
        self.assertTrue(all(output.read_bytes()==self.data for output in outputs))
        records=json.loads((reports/'results.json').read_text())['jobs']
        self.assertEqual(len(records),2)
    def test_changed_source_refuses_resume(self):
        args,_,image,_=self.arguments(); self.assertEqual(self.cli(args),0)
        b=bytearray(image.read_bytes()); b[-1]=42; image.write_bytes(b)
        self.assertEqual(self.cli(args),2)
    def test_missing_published_prefix_refuses_resume(self):
        args,_,_,_=self.arguments([self.data,self.data])
        self.assertEqual(self.cli(args+['--limit','1']),0)
        output=next((self.root/'case/reconstructed').glob('*.mp4'))
        output.unlink()
        self.assertEqual(self.cli(args),2)
        self.assertFalse(list((self.root/'case/reconstructed').glob('*.mp4')))
        with contextlib.closing(sqlite3.connect(self.root/'case/case.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE kind='fragmented'").fetchone()[0],1)
    def test_changed_published_prefix_refuses_resume(self):
        args,_,_,_=self.arguments(); self.assertEqual(self.cli(args),0)
        output=next((self.root/'case/reconstructed').glob('*.mp4'))
        raw=bytearray(output.read_bytes()); raw[-1]^=1; output.write_bytes(raw)
        self.assertEqual(self.cli(args),2)
        self.assertEqual(output.read_bytes(),raw)
    def test_truncated_published_prefix_refuses_resume(self):
        args,_,_,_=self.arguments(); self.assertEqual(self.cli(args),0)
        output=next((self.root/'case/reconstructed').glob('*.mp4'))
        output.write_bytes(b'corrupt')
        self.assertEqual(self.cli(args),2)
        self.assertEqual(output.read_bytes(),b'corrupt')
    def test_missing_published_evidence_refuses_resume(self):
        args,_,_,_=self.arguments(); self.assertEqual(self.cli(args),0)
        next((self.root/'case/work').glob('*/result.json')).unlink()
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
    def test_xml_entities_rejected(self):
        args,rec,_,_=self.arguments(); p=rec/'recup_dir.1/report.xml'
        p.write_text('<!DOCTYPE doc [<!ENTITY e "bad">]><doc>&e;</doc>')
        self.assertEqual(self.cli(args),2)
    def test_lost_commit_existing_output_exact_bytes_resumes(self):
        args,_,_,_=self.arguments(); self.assertEqual(self.cli(args),0)
        with contextlib.closing(sqlite3.connect(self.root/'case/case.sqlite3')) as db:
            db.execute("DELETE FROM jobs WHERE kind='fragmented'"); db.commit()
        self.assertEqual(self.cli(args),0)
    def test_untrusted_existing_case_refused(self):
        args,_,_,_=self.arguments(); case=self.root/'case'; case.mkdir(); (case/'important.txt').write_text('keep')
        self.assertEqual(self.cli(args),2); self.assertEqual((case/'important.txt').read_text(),'keep')

    def test_files_only_uses_same_fragmented_strategy(self):
        source=self.root/'recording.mp4'; source.write_bytes(self.data)
        before=source.read_bytes()
        self.assertEqual(self.cli(['run','--input',str(source),'--case',str(self.root/'case')]),0)
        outputs=list((self.root/'case/reconstructed').glob('*.mp4'))
        self.assertEqual([p.read_bytes() for p in outputs],[self.data])
        self.assertEqual(source.read_bytes(),before)
        jobs=json.loads((self.root/'case/reports/results.json').read_text())['jobs']
        self.assertEqual(jobs[0]['detail']['association'],'recovered_file')
        self.assertTrue(all(r['acquisition']=='unknown' for r in jobs[0]['detail']['segments'][0]['source_ranges']))

    def test_filesystem_extents_reassemble_noncontiguous_fragmented_bytes(self):
        import xml.etree.ElementTree as ET
        image=self.root/'scattered.img'
        chunks=[self.data[i:i+512] for i in range(0,len(self.data),512)]
        physical=bytearray(b'\0'*4096)
        mappings=[]
        for index in reversed(range(len(chunks))):
            offset=len(physical)
            physical.extend(chunks[index]); physical.extend(b'\0'*37)
            mappings.append((index*512,offset,len(chunks[index])))
        image.write_bytes(physical)
        doc=ET.Element('dfxml'); obj=ET.SubElement(doc,'fileobject')
        ET.SubElement(obj,'filename').text='recording.mp4'
        ET.SubElement(obj,'filesize').text=str(len(self.data))
        runs=ET.SubElement(obj,'byte_runs')
        for logical,offset,length in sorted(mappings):
            ET.SubElement(runs,'byte_run',file_offset=str(logical),img_offset=str(offset),len=str(length))
        mapping=self.root/'filesystem.xml'; ET.ElementTree(doc).write(mapping,encoding='utf-8')
        self.assertEqual(self.cli(['run','--image',str(image),'--extents',str(mapping),'--case',str(self.root/'case')]),0)
        outputs=list((self.root/'case/reconstructed').glob('*.mp4'))
        self.assertEqual([p.read_bytes() for p in outputs],[self.data])
        self.assertEqual(image.read_bytes(),physical)
        jobs=json.loads((self.root/'case/reports/results.json').read_text())['jobs']
        detail=jobs[0]['detail']
        self.assertEqual(detail['association'],'filesystem_metadata')
        self.assertGreater(len(detail['segments'][0]['source_ranges']),2)

    def test_rescue_hole_preserves_prefix_and_later_independent_section(self):
        data,prefix=generate(self.root/'generated.mp4',closed_gop=True,seconds=4)
        image=self.root/'source.img'; image.write_bytes(data)
        damaged=prefix.fragments[1]
        mapping=self.root/'source.map'
        mapping.write_text(f'0 + 1\n0 {damaged.start} +\n{damaged.start} {damaged.end-damaged.start} -\n{damaged.end} {len(data)-damaged.end} +\n')
        args=['run','--image',str(image),'--mapfile',str(mapping),'--case',str(self.root/'case')]
        self.assertEqual(self.cli(args),0)
        outputs=[p.read_bytes() for p in (self.root/'case/reconstructed').glob('*.mp4')]
        init=prefix.initialization.data
        expected=[data[:prefix.fragments[0].end],init+data[prefix.fragments[2].start:prefix.fragments[-1].end]]
        self.assertCountEqual(outputs,expected)
        self.assertEqual(image.read_bytes(),data)
        jobs=json.loads((self.root/'case/reports/results.json').read_text())['jobs']
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['status'],'decoded_segments')
        sections=jobs[0]['detail']['segments']
        later=next(s for s in sections if s['status']=='decoded_segment')
        self.assertEqual(later['logical_start'],prefix.fragments[2].start)
        self.assertTrue(all(r['acquisition']=='rescued' for s in sections if s['status'].startswith('decoded') for r in s['source_ranges']))
        self.assertTrue(jobs[0]['detail']['gaps'])
        before={p.name:p.read_bytes() for p in (self.root/'case/reconstructed').glob('*.mp4')}
        self.assertEqual(self.cli(args),0)
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'case/reconstructed').glob('*.mp4')},before)

    def test_first_surviving_fragment_is_labeled_later_section(self):
        data,prefix=generate(self.root/'generated.mp4',closed_gop=True,seconds=3)
        source=self.root/'surviving.mp4'
        surviving=prefix.initialization.data+data[prefix.fragments[1].start:prefix.fragments[-1].end]
        source.write_bytes(surviving)
        self.assertEqual(self.cli(['run','--input',str(source),'--case',str(self.root/'case')]),0)
        jobs=json.loads((self.root/'case/reports/results.json').read_text())['jobs']
        self.assertEqual(jobs[0]['status'],'decoded_segments')
        section=jobs[0]['detail']['segments'][0]
        self.assertEqual(section['status'],'decoded_segment')
        self.assertTrue(section['later_section'])
        self.assertEqual(next((self.root/'case/reconstructed').glob('*.mp4')).read_bytes(),surviving)

    def test_restart_without_track_timing_remains_blocked(self):
        data,prefix=generate(self.root/'generated.mp4',closed_gop=True,seconds=3)
        repeated=repacketize(data,prefix)
        parsed=walk_prefix(repeated,len(prefix.initialization.data))
        # Destroy an early non-IDR video fragment while retaining a later IDR.
        damaged=parsed.fragments[2]
        raw=bytearray(repeated); raw[damaged.start:damaged.end]=b'\0'*(damaged.end-damaged.start)
        image=self.root/'source.img'; image.write_bytes(raw)
        self.assertEqual(self.cli(['run','--image',str(image),'--case',str(self.root/'case')]),0)
        jobs=json.loads((self.root/'case/reports/results.json').read_text())['jobs']
        self.assertTrue(any(s.get('reason')=='missing_restart_track_timing' for s in jobs[0]['detail']['segments']))
        self.assertFalse(any(s['status']=='decoded_segment' for s in jobs[0]['detail']['segments']))

    def test_blocked_result_does_not_export_private_diagnostic_windows(self):
        movie=self.data[:self.init_size]+b'corrupt data'+b'\0'*100
        args,_,image,_=self.arguments([movie])
        self.assertEqual(self.cli(args),0)
        jobs=json.loads((self.root/'case/reports/results.json').read_text())['jobs']
        self.assertEqual(jobs[0]['status'],'blocked')
        self.assertFalse((self.root/'case/blocked-windows').exists())
        self.assertFalse(list((self.root/'case/reconstructed').glob('*.mp4')))

    def test_malformed_restart_candidates_cannot_bypass_work_budgets(self):
        from types import SimpleNamespace
        from fragmented_helpers import full
        from videorebuild.image_recovery import recover_fragmented
        from videorebuild.storage import Case, ReaderPool, scan_logical
        init=self.avc[:self.avc_init]
        bad=make_box(b'moof',full(b'mfhd',0,struct.pack('>I',2))+make_box(b'traf',b''))
        for limit in (1,64):
            with self.subTest(limit=limit):
                count=128; tail=2*1024*1024
                total=len(init)+count*(len(bad)+8)+tail
                pieces=[init]
                for index in range(count):
                    offset=len(init)+index*(len(bad)+8)
                    pieces.extend([bad,struct.pack('>I4s',total-offset-len(bad),b'mdat')])
                pieces.append(bytes(tail))
                image=self.root/f'bad-{limit}.img'; image.write_bytes(b''.join(pieces))
                with Case(self.root/f'budget-case-{limit}') as case:
                    scan_logical(case,case.register_source(image,'image'))
                    options=SimpleNamespace(max_record_bytes=16*1024*1024,max_segments=limit,timeout=1,max_output_bytes=16*1024*1024)
                    with ReaderPool(case.sources(),physical_sources=case.physical_sources()) as pool:
                        original=pool.read; requested=0
                        def observed(sid,offset,size):
                            nonlocal requested
                            requested+=size
                            return original(sid,offset,size)
                        with patch.object(pool,'read',side_effect=observed),patch('videorebuild.image_recovery.validate_prefix') as validator:
                            recover_fragmented(case,pool,{},options)
                        self.assertFalse(validator.called)
                    job=next(case.get_jobs('fragmented'))
                    detail=job['detail']
                    self.assertEqual(job['status'],'search_limited')
                    self.assertLessEqual(detail['candidate_starts_considered'],limit)
                    self.assertLessEqual(detail['media_read_bytes_charged'],detail['media_read_byte_budget'])
                    self.assertLessEqual(requested,2*image.stat().st_size+len(init)+32)
                    self.assertFalse(detail['outputs'])
                    self.assertIn(detail['limit_reason'],('segment_attempt_budget','segment_read_byte_budget'))

    def test_search_limit_retains_already_validated_prefix(self):
        source=self.root/'source.mp4'
        source.write_bytes(self.data+b'corrupt'+self.timed[self.init_size:])
        self.assertEqual(self.cli(['run','--input',str(source),'--case',str(self.root/'case'),'--max-segments','1']),0)
        jobs=json.loads((self.root/'case/reports/results.json').read_text())['jobs']
        self.assertEqual(jobs[0]['status'],'search_limited')
        self.assertEqual(jobs[0]['detail']['limit_reason'],'segment_attempt_budget')
        output=next((self.root/'case/reconstructed').glob('*.mp4'))
        self.assertEqual(output.read_bytes(),self.data)

    def test_failed_probe_evidence_retention_is_bounded(self):
        source=self.root/'source.mp4'; source.write_bytes(self.data)
        result={'ok':False,'reason':'synthetic_probe_failure','probe':{'stdout':'x'*1000000,'stderr':''}}
        with patch('videorebuild.image_recovery.validate_prefix',return_value=result):
            self.assertEqual(self.cli(['run','--input',str(source),'--case',str(self.root/'case')]),0)
        jobs=json.loads((self.root/'case/reports/results.json').read_text())['jobs']
        probe=jobs[0]['detail']['segments'][0]['validation']['probe']
        self.assertLessEqual(len(probe['stdout']),65536)
        self.assertTrue(probe['stdout_retained_truncated'])

    def test_segment_budget_limits_validation_attempts(self):
        data,_=generate(self.root/'generated.mp4',closed_gop=True,seconds=3)
        source=self.root/'source.mp4'; source.write_bytes(data)
        args=['run','--input',str(source),'--case',str(self.root/'case'),'--max-segments','1']
        with patch('videorebuild.image_recovery.validate_prefix',return_value={'ok':False,'reason':'synthetic_decode_failure'}) as validator:
            self.assertEqual(self.cli(args),0)
        self.assertEqual(validator.call_count,1)
        jobs=json.loads((self.root/'case/reports/results.json').read_text())['jobs']
        self.assertEqual(jobs[0]['status'],'search_limited')
        self.assertEqual(jobs[0]['detail']['reason'],'segment_attempt_budget')

    def test_segment_budget_is_explicit(self):
        args,_,_,_=self.arguments()
        self.assertEqual(self.cli(args+['--max-segments','1025']),2)


if __name__=='__main__': unittest.main()
