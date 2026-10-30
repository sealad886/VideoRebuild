#!/usr/bin/env python3
"""Verify an Intake ZIP and produce a binary-structure report without decoding it.

No binary media are extracted, uploaded, or embedded in the JSON output.
"""
from __future__ import annotations
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from videorebuild.fragmented import parse_initialization, inspect_embedded
from videorebuild.image_recovery import XMLReader, descendants, text
from videorebuild.storage import SafetyError, create_private_json, file_sha256


def inspect(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as z:
        infos=z.infolist()
        if len(infos)>1000 or sum(x.file_size for x in infos)>32*1024*1024:
            raise ValueError('Diagnostic ZIP exceeds 1000 entries / 32 MiB uncompressed')
        names=[x.filename for x in infos]
        if len(names)!=len(set(names)): raise ValueError('Duplicate ZIP entry names')
        m=json.loads(z.read('manifest.json'))
        samples=[]; profiles={}
        for row in m['samples']:
            name='samples/'+row['path']; data=z.read(name)
            h=hashlib.sha256(data).hexdigest()
            if h!=row['sha256'] or len(data)!=row['size_bytes']:
                raise ValueError(f'Hash/size mismatch: {name}')
            samples.append((row['path'],data,h))
            if row['path'].endswith('.mp4'):
                p=parse_initialization(data); profiles[p.sha256]=p
        if not profiles: raise ValueError('No supported initialization in diagnostic')
        profile_counts=Counter(h for n,b,h in samples if n.endswith('.mp4'))
        source=[]; mappings={}
        for n in names:
            if n.startswith('photorec/') and n.endswith('.xml'):
                raw=z.read(n)
                r=ET.parse(XMLReader(io.BytesIO(raw))).getroot()
                for s in descendants(r,'source'):
                    source.append({'image_filename':text(s,'image_filename'),
                                   'image_size':int(text(s,'image_size')),
                                   'sector_size':int(text(s,'sectorsize')),
                                   'block_size':int(text(s,'block_size'))})
                for f in descendants(r,'fileobject'):
                    name='/'.join((text(f,'filename') or '').split('/')[-2:])
                    runs=[{k:int(v) for k,v in b.attrib.items()} for b in descendants(f,'byte_run')]
                    runs.sort(key=lambda b:b['offset'])
                    gaps=[]
                    for a,b in zip(runs,runs[1:]):
                        end=a['img_offset']+a['len']
                        if b['img_offset']!=end:
                            gaps.append({'start':end,'end':b['img_offset'],'length':b['img_offset']-end})
                    mappings[name]={'filesize':int(text(f,'filesize')),'byte_runs':runs,'source_discontinuities':gaps}
        inventory=[]
        for name,data,h in samples:
            rec={'path':name,'size':len(data),'sha256':h,'mapping':mappings.get(name)}
            if name.endswith('.mp4'):
                rec['classification']='fragmented_mp4_initialization_without_media'
            else:
                rec['classification']='mdat_box' if data[4:8]==b'mdat' else 'other'
                rec['embedded_by_initialization']={key:inspect_embedded(data,p) for key,p in profiles.items()}
            inventory.append(rec)
        return {'archive_sha256':file_sha256(archive),
                'verified_samples':len(samples),'verified_sample_bytes':sum(len(b) for _,b,_ in samples),
                'collector_complete':m.get('complete'),'collector_errors':m.get('errors'),
                'source':source,'initialization_profiles':{
                    key:{'sample_count':profile_counts[key],'size':len(p.data),
                         'tracks':{str(tid):asdict(t) for tid,t in p.tracks.items()}}
                    for key,p in profiles.items()},
                'inventory':inventory,
                'scope':'Binary-structure analysis of supplied samples; not source-image recovery or a population-wide content hash.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('archive',type=Path); p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    try:
        result=inspect(args.archive)
        create_private_json(args.output,result)
        print(f'Verified {result["verified_samples"]} samples. Report: {args.output}')
        return 0
    except (OSError,ValueError,KeyError,SafetyError,zipfile.BadZipFile,ET.ParseError) as e:
        print(f'ERROR: {e}',file=sys.stderr); return 2

if __name__=='__main__': raise SystemExit(main())
