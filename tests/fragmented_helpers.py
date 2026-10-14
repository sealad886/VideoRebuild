"""Generate nonprivate, decoder-tested fixtures resembling the observed layout."""
from pathlib import Path
import struct
import subprocess
import xml.etree.ElementTree as ET

from videorebuild.bmff import make_box
from videorebuild.fragmented import box_at, walk_prefix


def full(typ, flags, payload, version=0):
    return make_box(typ, struct.pack('>I', (version << 24)|flags)+payload)


def single_fragment(seq, track, sample, payload, tfdt=None):
    tfhd = full(b'tfhd', 0x020020 if track.handler == 'vide' else 0x020008,
                struct.pack('>II', track.track_id,
                            sample.flags if track.handler == 'vide' else sample.duration))
    extra = b'' if tfdt is None else full(b'tfdt',0,struct.pack('>Q',tfdt),1)
    flags = 0x301 if track.handler == 'vide' else 0x201
    def moof(data_offset):
        fields=struct.pack('>Ii',1,data_offset)
        if track.handler == 'vide': fields+=struct.pack('>I',sample.duration)
        fields+=struct.pack('>I',sample.size)
        trun=full(b'trun',flags,fields)
        return make_box(b'moof',full(b'mfhd',0,struct.pack('>I',seq))+
                        make_box(b'traf',tfhd+extra+trun))
    m=moof(0); m=moof(len(m)+8)
    return m+make_box(b'mdat',payload)


def generate(path: Path, codec='hevc'):
    args=['ffmpeg','-hide_banner','-v','error','-nostdin','-y',
          '-f','lavfi','-i','testsrc2=size=128x96:rate=10:duration=1',
          '-f','lavfi','-i','sine=frequency=440:sample_rate=8000:duration=1',
          '-c:v','libx265' if codec=='hevc' else 'libx264','-threads','1','-bf','0','-g','10']
    if codec=='hevc': args+=['-x265-params','pools=none:frame-threads=1:bframes=0:keyint=10:log-level=error','-tag:v','hvc1']
    args+=['-c:a','aac','-b:a','24k','-video_track_timescale','90000',
           '-movflags','empty_moov+default_base_moof+frag_keyframe',str(path)]
    subprocess.run(args,check=True,timeout=45,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    data=path.read_bytes(); init_size=box_at(data,box_at(data,0).end).end
    p=walk_prefix(data,init_size)
    assert p.eligibility_error() is None and p.stop_reason=='terminal_index'
    return data,p


def repacketize(data,p,with_tfdt=False):
    result=[p.initialization.data]; clocks={}; seq=1
    samples=sorted((s for f in p.fragments for s in f.samples),key=lambda s:s.offset)
    for s in samples:
        t=p.initialization.tracks[s.track_id]
        result.append(single_fragment(seq,t,s,data[s.offset:s.offset+s.size],clocks.get(s.track_id,0) if with_tfdt else None))
        clocks[s.track_id]=clocks.get(s.track_id,0)+s.duration; seq+=1
    return b''.join(result)


def write_case_inputs(root: Path, movies, init_size):
    recovered=root/'recovery'; d=recovered/'recup_dir.1'; d.mkdir(parents=True)
    image=root/'source.img'; pieces=[b'\0'*8192]; locations=[]
    for index,movie in enumerate(movies):
        off=sum(map(len,pieces)); name=f'f{off//512:08d}.mp4'
        (d/name).write_bytes(movie[:init_size]); locations.append((off,name))
        pieces += [movie,b'\0'*((-len(movie))%4096)]
    raw=b''.join(pieces); image.write_bytes(raw)
    doc=ET.Element('dfxml'); source=ET.SubElement(doc,'source')
    ET.SubElement(source,'image_filename').text=str(image)
    ET.SubElement(source,'image_size').text=str(len(raw))
    ET.SubElement(source,'sectorsize').text='512'
    volume=ET.SubElement(source,'volume'); ET.SubElement(volume,'block_size').text='16384'
    for off,name in locations:
        f=ET.SubElement(doc,'fileobject'); ET.SubElement(f,'filename').text='/old/mount/recup_dir.1/'+name
        ET.SubElement(f,'filesize').text=str(init_size)
        runs=ET.SubElement(f,'byte_runs')
        ET.SubElement(runs,'byte_run',offset='0',img_offset=str(off),len=str(((init_size+511)//512)*512))
    ET.ElementTree(doc).write(d/'report.xml',encoding='utf-8',xml_declaration=True)
    return recovered,image,locations
