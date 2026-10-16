"""Read-only, report-anchored fMP4 recovery from a regular raw image.

Recovered header hashes are configuration identities, NOT recording identities.
The database therefore keys jobs by source offset, even when every header is
identical. A job follows adjacent, structurally verified fragments only. It stops
at discontinuities and never pairs files by their names or global sequence IDs.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict
from fractions import Fraction
import fcntl
import hashlib
import html
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import struct
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from . import __version__
from .bmff import FormatError
from .fragmented import MAX_INIT, walk_prefix
from .storage import SafetyError
from .validate import doctor, run_tool

LOG = logging.getLogger(__name__)


def positive(value: str) -> int:
    n = int(value)
    if n <= 0: raise argparse.ArgumentTypeError('must be positive')
    return n


def add_parser(subs) -> None:
    q = subs.add_parser('recover-fragmented', help='Recover fMP4 prefixes from original-image bytes using PhotoRec report anchors')
    q.add_argument('--image', type=Path, required=True, help='Regular raw image used for the PhotoRec report; never a device node')
    q.add_argument('--root', type=Path, required=True, help='Parent of recup_dir.* folders with the original recovered files')
    q.add_argument('--report', type=Path, help='PhotoRec XML (default: ROOT/recup_dir.1/report.xml)')
    q.add_argument('--case', type=Path, required=True, help='New case folder outside the recovery root; same case resumes completed jobs')
    group = q.add_mutually_exclusive_group()
    group.add_argument('--limit', type=positive, default=50, help='Maximum new header locations to process (default: 50)')
    group.add_argument('--all', action='store_true', help='Process all remaining header locations')
    q.add_argument('--max-record-bytes', type=positive, default=16*1024*1024, help='Per-header source-window limit, default 16777216')
    q.add_argument('--timeout', type=float, default=120, help='Seconds per FFmpeg/ffprobe process; default 120')
    q.add_argument('--diagnostic-windows', type=int, default=4, help='Retain up to this many blocked source windows, 0 disables (default: 4)')
    q.add_argument('--diagnostic-window-bytes', type=positive, default=1024*1024, help='Maximum bytes per blocked source window (default: 1048576)')


def signature(p: Path) -> dict:
    s = p.stat()
    if not stat.S_ISREG(s.st_mode):
        raise SafetyError(f'Not a regular file: {p}. Device nodes are not accepted.')
    return {'path': str(p), 'size': s.st_size, 'mtime_ns': s.st_mtime_ns,
            'ctime_ns': s.st_ctime_ns, 'inode': s.st_ino, 'device': s.st_dev}


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda: f.read(1024*1024), b''): h.update(b)
    return h.hexdigest()


def write_json(p: Path, obj) -> None:
    if p.is_symlink(): raise SafetyError(f'Refusing symlink: {p}')
    tmp = p.with_name(p.name+'.'+uuid.uuid4().hex+'.tmp')
    with tmp.open('x', encoding='utf-8') as f:
        json.dump(obj, f, indent=2, ensure_ascii=True)
        f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)


class XMLReader:
    """Bounded read chunks, reject declarations that could expand entities."""
    def __init__(self, f): self.f, self.tail = f, b''
    def read(self, n=-1):
        b = self.f.read(65536 if n < 0 else min(n, 65536))
        check = (self.tail+b).upper()
        if b'<!DOCTYPE' in check or b'<!ENTITY' in check:
            raise SafetyError('DTD/entity declarations are not accepted in PhotoRec XML')
        self.tail = check[-32:]
        return b


def local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def descendants(e, name):
    return [x for x in e.iter() if local(x.tag) == name]


def text(e, name):
    found = descendants(e, name)
    return found[0].text if found else None


def import_report(db: sqlite3.Connection, report: Path) -> dict:
    """Stream XML once; retain only small MP4 anchors, not 346k full elements."""
    seen = selected = 0
    info = None
    stack = []
    db.execute('BEGIN')
    try:
        db.execute('DELETE FROM anchors')
        with report.open('rb') as f:
            for event, e in ET.iterparse(XMLReader(f), events=('start', 'end')):
                if event == 'start':
                    stack.append(e)
                    if len(stack) > 64: raise SafetyError('XML nesting limit exceeded')
                    continue
                typ = local(e.tag)
                if typ == 'source':
                    info = {'reported_image_path': text(e, 'image_filename'),
                            'image_size': int(text(e, 'image_size') or -1),
                            'sector_size': int(text(e, 'sectorsize') or -1),
                            'block_size': int(text(e, 'block_size') or -1)}
                    if info['image_size'] <= 0 or info['sector_size'] <= 0:
                        raise SafetyError('Missing source geometry in PhotoRec XML')
                elif typ == 'fileobject':
                    seen += 1
                    name = text(e, 'filename') or ''
                    size = int(text(e, 'filesize') or -1)
                    parts = PurePosixPath(name).parts
                    if name.lower().endswith('.mp4') and 16 <= size <= MAX_INIT:
                        # PhotoRec writes bare names for files beside report.xml,
                        # then qualified paths after moving to later recup dirs.
                        # Derive only the report's explicit directory; never
                        # search other folders or infer a location from a name.
                        if len(parts) == 1:
                            parts = (report.parent.name, parts[0])
                        if len(parts) < 2 or not re.fullmatch(r'recup_dir\.\d+', parts[-2]):
                            raise SafetyError(f'Cannot safely map report filename: {name}')
                        if not re.fullmatch(r'[A-Za-z0-9_.-]+\.mp4', parts[-1], re.I):
                            raise SafetyError(f'Unsupported report basename: {name}')
                        runs = []
                        for r in descendants(e, 'byte_run'):
                            a = r.attrib
                            runs.append({'offset': int(a.get('offset', a.get('file_offset', -1))),
                                         'img_offset': int(a.get('img_offset', -1)), 'len': int(a.get('len', -1))})
                        runs.sort(key=lambda r: r['offset'])
                        covered = 0
                        for r in runs:
                            if r['offset'] != covered or r['img_offset'] < 0 or r['len'] <= 0:
                                raise SafetyError(f'Invalid byte-run mapping for {name}')
                            covered += r['len']
                        if not runs or covered < size or len(runs) > 64:
                            raise SafetyError(f'Incomplete/excessive byte-run mapping for {name}')
                        rel = '/'.join(parts[-2:])
                        db.execute('INSERT INTO anchors(offset,path,size,runs) VALUES(?,?,?,?)',
                                   (runs[0]['img_offset'], rel, size, json.dumps(runs)))
                        selected += 1
                    if seen % 50000 == 0: LOG.info('Imported %d XML records; %d header candidates', seen, selected)
                # Keep each source/fileobject intact until its end, then detach it.
                if typ in ('fileobject', 'source', 'creator', 'metadata'):
                    if len(stack) >= 2: stack[-2].remove(e)
                    e.clear()
                stack.pop()
        if info is None: raise SafetyError('PhotoRec XML has no source description')
        info.update(fileobjects_seen=seen, header_locations=selected)
        if not selected: raise SafetyError('No small MP4 header locations in the report')
        db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)', ('report_info', json.dumps(info)))
        db.commit()
        return info
    except BaseException:
        db.rollback()
        raise


@contextmanager
def locked_case(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lp = root/'.fragmented.lock'
    fd = os.open(lp, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if os.fstat(fd).st_nlink != 1: raise SafetyError('Case lock has multiple hard links')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e: raise SafetyError('Another process is using this fragmented case') from e
        yield
    finally:
        os.close(fd)


def _safe_subdir(root: Path, name: str) -> Path:
    d = root/name
    if d.is_symlink(): raise SafetyError(f'Case directory is a symlink: {d}')
    d.mkdir(exist_ok=True)
    if not d.is_dir(): raise SafetyError(f'Not a directory: {d}')
    return d


def validate_prefix(candidate: Path, data: bytes, prefix, tools: dict, timeout: float) -> dict:
    """Independent packet hashes/timing, packet/frame counts, and strict decoding."""
    base = ['-v', 'error', '-threads', '1', '-f', 'mov', '-protocol_whitelist', 'file', '-enable_drefs', '0']
    expected = defaultdict(list)
    samples = defaultdict(list)
    clocks = dict(prefix.starts)
    shifts = defaultdict(int)
    view = memoryview(data)
    for f in prefix.fragments:
        for s in f.samples:
            expected[s.track_id].append(hashlib.sha256(view[s.offset:s.offset+s.size]).hexdigest())
            samples[s.track_id].append(s)
            # MOV demuxing normalizes negative composition offsets by adding
            # the largest negative offset's magnitude to every PTS in a track.
            shifts[s.track_id] = max(shifts[s.track_id], -s.composition_offset)
    probe = run_tool([tools['ffprobe']['path'], *base, '-count_frames', '-count_packets',
                      '-show_streams', '-show_packets', '-show_data_hash', 'sha256',
                      '-show_entries', 'stream=index,id,codec_name,time_base,nb_read_frames,nb_read_packets:packet=stream_index,data_hash,dts,pts,duration',
                      '-of', 'json', str(candidate)], timeout, output_limit=64*1024*1024)
    detail = {'ok': False, 'probe': probe, 'expected_packets': {str(k):len(v) for k,v in expected.items()}}
    if any((probe['returncode'], probe['timeout'], probe['stderr'].strip(), probe['stdout_truncated'])):
        detail['reason'] = 'probe_or_decode_failed'
        return detail
    try:
        obj = json.loads(probe['stdout'])
        streams = obj['streams']
        if len(streams) != len(prefix.initialization.tracks): raise ValueError('track_count_mismatch')
        indices = {}
        scales = {}
        for s in streams:
            tid = int(str(s['id']), 0)
            if tid not in expected or tid in indices.values(): raise ValueError('track_id_mismatch')
            t = prefix.initialization.tracks[tid]
            codec = 'aac' if t.handler == 'soun' else ('hevc' if t.codec in ('hvc1', 'hev1') else 'h264')
            if s['codec_name'] != codec: raise ValueError('codec_mismatch')
            if int(s['nb_read_packets']) != len(expected[tid]) or int(s['nb_read_frames']) != len(expected[tid]):
                raise ValueError('packet_or_decoded_frame_count_mismatch')
            indices[s['index']] = tid
            scales[tid] = Fraction(s['time_base']) * t.timescale
        actual = defaultdict(list)
        for packet in obj.get('packets', []):
            tid = indices[packet['stream_index']]
            index = len(actual[tid])
            if index >= len(samples[tid]): raise ValueError('packet_or_decoded_frame_count_mismatch')
            sample = samples[tid][index]
            dts = clocks[tid]
            wanted = (dts, dts+sample.composition_offset+shifts[tid], sample.duration)
            if tuple(int(packet[k])*scales[tid] for k in ('dts', 'pts', 'duration')) != wanted:
                raise ValueError('packet_timing_mismatch')
            clocks[tid] += sample.duration
            actual[tid].append(packet['data_hash'].split(':',1)[1].lower())
        if dict(actual) != dict(expected): raise ValueError('encoded_packet_hash_mismatch')
    except (ValueError, KeyError, TypeError, ZeroDivisionError) as e:
        detail['reason'] = str(e)
        return detail
    # The hashes above are now summarized; don't duplicate huge JSON in a report.
    detail['probe']['stdout'] = json.dumps({'streams': streams, 'packet_sha256_comparison': 'exact_match',
                                          'packet_timing_comparison': 'exact_match',
                                          'pts_normalization_ticks': dict(shifts)})
    decode = run_tool([tools['ffmpeg']['path'], '-hide_banner', '-nostdin', '-xerror',
                       *base, '-err_detect', 'explode', '-i', str(candidate),
                       '-map', '0:v', '-map', '0:a?', '-threads', '1', '-filter_threads', '1',
                       '-f', 'null', '-'], timeout)
    detail['decode'] = decode
    detail['ok'] = not any((decode['returncode'], decode['timeout'], decode['stderr'].strip()))
    detail['reason'] = 'packet_hashes_and_full_decode_passed' if detail['ok'] else 'strict_decode_failed'
    return detail


def export_report(db, root: Path, binding: dict, info: dict) -> dict:
    counts = dict(db.execute('SELECT status,COUNT(*) FROM results GROUP BY status').fetchall())
    total = db.execute('SELECT COUNT(*) FROM anchors').fetchone()[0]
    done = sum(counts.values())
    reasons = dict(db.execute('SELECT reason,COUNT(*) FROM results GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 30').fetchall())
    summary = {'tool': 'VideoRebuild', 'version': __version__, 'backend': 'report_anchored_fragmented_mp4',
               'header_locations': total, 'processed': done, 'pending': total-done,
               'status_counts': counts, 'stop_reasons': reasons,
               'source_image_bytes': info['image_size'], 'sector_size': info['sector_size'],
               'block_size': info['block_size'], 'run_tools': binding['tools'],
               'warnings': ['Outputs are decode-checked contiguous prefixes, not assertions of complete historical recordings.',
                            'No arbitrary filesystem-fragment reordering or cross-recording joining is performed.',
                            'Repeated initialization hashes and fragment sequence IDs do not establish recording identity.',
                            'Absent tfdt is supported only by per-track duration accumulation along an uninterrupted sequence.',
                            'Source identity uses stored file attributes, sampled image bytes, and exact recovered-header comparisons; not a full-image forensic hash.',
                            'Blocked source windows may contain private video/audio. Review before sharing.']}
    reports = root/'reports'
    write_json(reports/'summary.json', summary)
    # Stream complete results instead of accumulating hundreds of thousands in RAM.
    tmp = reports/('results-'+uuid.uuid4().hex+'.jsonl.tmp')
    with tmp.open('x', encoding='utf-8') as out:
        for off, status, reason, location in db.execute('SELECT offset,status,reason,detail_path FROM results ORDER BY offset'):
            out.write(json.dumps({'image_offset': off, 'status': status, 'reason': reason, 'detail_path': location})+'\n')
    os.replace(tmp, reports/'results.jsonl')
    rows=[]
    for off, status, reason, detail in db.execute('SELECT offset,status,reason,detail_path FROM results ORDER BY updated DESC LIMIT 100'):
        rows.append('<tr><td>'+str(off)+'</td><td>'+html.escape(status)+'</td><td>'+html.escape(reason)+
                    '</td><td><a href="../'+html.escape(detail,quote=True)+'">details</a></td></tr>')
    page='''<!doctype html><html lang="en"><meta charset="utf-8"><title>VideoRebuild fragmented recovery</title>
<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;line-height:1.5}td,th{padding:10px;border-bottom:1px solid #ccc;text-align:left}pre{white-space:pre-wrap}table{width:100%}</style>
<h1>Fragmented MP4 recovery</h1><p><strong>Decoded prefix does not mean complete original recording.</strong> Output names identify original-image byte offsets, not dates.</p><pre>'''+html.escape(json.dumps(summary,indent=2))+'''</pre><h2>Latest 100 results</h2><table><tr><th>Image byte offset</th><th>Status</th><th>Stop reason</th><th>Evidence</th></tr>'''+''.join(rows)+'''</table><p>Complete index: <a href="results.jsonl">results.jsonl</a>. Playable candidates: ../recovered/.</p></html>'''
    p=reports/'index.html'
    if p.is_symlink() or (p.exists() and p.stat().st_nlink != 1): raise SafetyError('Unsafe HTML report path')
    p.write_text(page,encoding='utf-8')
    return summary


def run(args) -> int:
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise SafetyError('--timeout must be finite and positive')
    if not 0 <= args.diagnostic_windows <= 100 or args.diagnostic_window_bytes > 16*1024*1024:
        raise SafetyError('Diagnostic limits: 0..100 windows; at most 16 MiB each')
    if not 1024 <= args.max_record_bytes <= 256*1024*1024:
        raise SafetyError('--max-record-bytes must be between 1024 and 268435456')
    image, recovered = args.image.expanduser().resolve(), args.root.expanduser().resolve()
    if args.case.expanduser().is_symlink(): raise SafetyError('Case root must not be a symlink')
    root = args.case.expanduser().resolve()
    report = (args.report or recovered/'recup_dir.1/report.xml').expanduser().resolve()
    if not recovered.is_dir(): raise SafetyError(f'Recovery root does not exist: {recovered}')
    if root == recovered or root in recovered.parents or recovered in root.parents:
        raise SafetyError('Case and recovered-source folders must not contain each other')
    if root == image or root in image.parents or root == report or root in report.parents:
        raise SafetyError('Case must not contain an input file')
    img_sig, xml_sig = signature(image), signature(report)
    if img_sig['size'] == 0: raise SafetyError('Source image is empty')
    tools = doctor()
    with image.open('rb') as f:
        h = hashlib.sha256()
        for at in sorted({0, max(0,img_sig['size']//2-524288), max(0,img_sig['size']-1048576)}):
            f.seek(at); h.update(at.to_bytes(8,'big')); h.update(f.read(1048576))
    binding={'version': __version__, 'image':img_sig, 'image_sample_sha256':h.hexdigest(),
             'report':xml_sig, 'report_sha256':sha_file(report), 'root':str(recovered),
             'max_record_bytes':args.max_record_bytes, 'timeout':args.timeout, 'tools':tools}
    if signature(image) != img_sig or signature(report) != xml_sig:
        raise SafetyError('An input changed during preflight')
    with locked_case(root):
        metadata = root/'fragmented-case.json'
        if metadata.is_symlink(): raise SafetyError('Case metadata symlink rejected')
        if metadata.exists():
            old=json.loads(metadata.read_text())
            if old != binding:
                raise SafetyError('Source identity, tool version, or recovery settings changed. Use a new case; existing results are not silently reused.')
        else:
            unknown=[p.name for p in root.iterdir() if p.name!='.fragmented.lock']
            if unknown: raise SafetyError(f'New fragmented case must be empty; found {unknown[:5]}')
            write_json(metadata,binding)
        for name in ('reports','attempts','recovered','blocked-windows'):
            _safe_subdir(root,name)
        logpath=root/'fragmented.log'
        if logpath.is_symlink() or (logpath.exists() and logpath.stat().st_nlink != 1):
            raise SafetyError('Unsafe log path')
        handler=logging.FileHandler(logpath,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        LOG.addHandler(handler)
        dbpath=root/'fragmented.sqlite3'
        if dbpath.is_symlink() or (dbpath.exists() and dbpath.stat().st_nlink != 1):
            raise SafetyError('Unsafe database path')
        db=sqlite3.connect(dbpath)
        info=None
        verified_outputs=False
        exported=False
        try:
            db.executescript('''CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS anchors(offset INTEGER PRIMARY KEY,path TEXT NOT NULL,size INTEGER NOT NULL,runs TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS results(offset INTEGER PRIMARY KEY,status TEXT NOT NULL,reason TEXT NOT NULL,detail_path TEXT NOT NULL,updated REAL NOT NULL);''')
            r=db.execute("SELECT value FROM metadata WHERE key='report_info'").fetchone()
            info=json.loads(r[0]) if r else import_report(db,report)
            if info['image_size'] != img_sig['size']:
                raise SafetyError(f'Image size {img_sig["size"]} does not match report {info["image_size"]}; offset rebasing is not guessed')
            if db.execute('SELECT MAX(offset+size) FROM anchors').fetchone()[0] > img_sig['size']:
                raise SafetyError('Report anchor lies beyond image end')
            if signature(report)!=xml_sig: raise SafetyError('Report changed during import')
            # A committed success is reusable only while its published bytes
            # still match the validation evidence. Check before processing any
            # new locations; preserve conflicting output for investigation.
            for offset, detail_path in db.execute("SELECT offset,detail_path FROM results WHERE status='decoded_prefix'"):
                evidence=root/detail_path
                if (evidence.is_symlink() or not evidence.is_file() or
                        not evidence.resolve().is_relative_to(root/'attempts')):
                    raise SafetyError(f'Missing or unsafe completed-prefix evidence at offset {offset}')
                saved=json.loads(evidence.read_text(encoding='utf-8'))
                if not isinstance(saved,dict):
                    raise SafetyError(f'Invalid completed-prefix evidence at offset {offset}')
                expected_size=saved.get('output_bytes')
                expected_hash=saved.get('candidate_sha256')
                if (not isinstance(expected_size,int) or expected_size<=0 or
                        not isinstance(expected_hash,str) or not re.fullmatch(r'[0-9a-f]{64}',expected_hash)):
                    raise SafetyError(f'Incomplete completed-prefix evidence at offset {offset}')
                output=root/'recovered'/f'offset-{offset:014d}-prefix.mp4'
                if (output.is_symlink() or not output.is_file() or
                        output.stat().st_size!=expected_size or sha_file(output)!=expected_hash):
                    raise SafetyError(f'Missing or changed completed prefix: {output}')
            verified_outputs=True
            remaining = None if args.all else args.limit
            done_now=0
            windows=len(list((root/'blocked-windows').glob('*.bin')))
            sql='''SELECT a.offset,a.path,a.size,a.runs,
 (SELECT MIN(b.offset) FROM anchors b WHERE b.offset>a.offset) AS following
 FROM anchors a LEFT JOIN results r ON a.offset=r.offset WHERE a.offset>? AND r.offset IS NULL ORDER BY a.offset'''
            last_offset = -1
            with image.open('rb') as source:
                while remaining is None or done_now<remaining:
                    row=db.execute(sql+' LIMIT 1',(last_offset,)).fetchone()
                    if row is None: break
                    offset,rel,size,runs_json,following=row
                    last_offset=offset
                    if signature(image)!=img_sig: raise SafetyError('Source image changed during recovery; stopping')
                    recovered_file=recovered/rel
                    if recovered_file.is_symlink() or not recovered_file.is_file() or recovered_file.resolve().parent != (recovered/Path(rel).parent).resolve():
                        raise SafetyError(f'Missing or unsafe recovered header: {recovered_file}')
                    if recovered_file.resolve().is_relative_to(recovered) is False:
                        raise SafetyError('Recovered file escapes source root')
                    hs=signature(recovered_file)
                    if hs['size'] != size: raise SafetyError(f'Recovered header size changed: {rel}')
                    header=recovered_file.read_bytes()
                    if signature(recovered_file)!=hs: raise SafetyError(f'Recovered header changed during read: {rel}')
                    runs=json.loads(runs_json)
                    # Verify XML mapping without replacing any missing image bytes.
                    mapped=bytearray()
                    for r in runs:
                        n=min(r['len'],size-r['offset'])
                        if n<=0: break
                        source.seek(r['img_offset']); part=source.read(n)
                        if len(part)!=n: raise SafetyError('Short image read during anchor verification')
                        mapped.extend(part)
                    if bytes(mapped)!=header:
                        raise SafetyError(f'Image does not reproduce the recovered header at offset {offset}; wrong image or changed data')
                    distance=(following-offset) if following is not None else img_sig['size']-offset
                    window_size=min(distance,args.max_record_bytes,img_sig['size']-offset)
                    source.seek(offset); data=source.read(window_size)
                    if len(data)!=window_size: raise SafetyError('Short image read; not treated as zero data')
                    attempt=root/'attempts'/f'{offset:014d}-{uuid.uuid4().hex[:8]}'
                    attempt.mkdir()
                    detail={'image_offset':offset,'recovered_header':rel,'initialization_sha256':hashlib.sha256(header).hexdigest(),
                            'source_window_bytes':len(data),'source_window_sha256':hashlib.sha256(data).hexdigest(),
                            'next_header_image_offset':following,'report_byte_runs':runs,'outputs':[]}
                    status='blocked'; reason='not_attempted'
                    try:
                        if data[:size]!=header:
                            raise FormatError('initialization_not_physically_contiguous; filesystem_reassembly_required')
                        prefix=walk_prefix(data,size)
                        reason=prefix.stop_reason
                        detail.update(stop_reason=reason,stop_image_offset=offset+prefix.stop_offset,
                                      fragment_count=len(prefix.fragments),sample_counts=prefix.counts,
                                      tracks={str(k):asdict(v) for k,v in prefix.initialization.tracks.items()},
                                      durations_seconds={str(k):(v-prefix.starts[k])/prefix.initialization.tracks[k].timescale for k,v in prefix.clocks.items()},
                                      first_sequence=prefix.fragments[0].sequence if prefix.fragments else None,
                                      last_sequence=prefix.fragments[-1].sequence if prefix.fragments else None,
                                      tfdt_present=sum(v is not None for f in prefix.fragments for v in f.decode_times.values()),
                                      tfdt_absent=sum(v is None for f in prefix.fragments for v in f.decode_times.values()),
                                      timing_note='Per-track cumulative durations where tfdt is absent; not wall-clock dates.')
                        eligibility=prefix.eligibility_error()
                        if eligibility:
                            detail['eligibility_error']=eligibility
                        else:
                            candidate=attempt/'candidate.mp4'
                            with candidate.open('xb') as f:
                                f.write(memoryview(data)[:prefix.end]); f.flush(); os.fsync(f.fileno())
                            detail['candidate_sha256']=sha_file(candidate)
                            checked=validate_prefix(candidate,data,prefix,tools,args.timeout)
                            detail['validation']=checked
                            if checked['ok']:
                                output=root/'recovered'/f'offset-{offset:014d}-prefix.mp4'
                                if output.is_symlink():
                                    raise SafetyError(f'Output is a symlink: {output}')
                                if output.exists():
                                    # Recover the commit-after-publish crash window only when bytes agree.
                                    if not output.is_file() or sha_file(output) != detail['candidate_sha256']:
                                        raise SafetyError(f'Conflicting existing output: {output}')
                                else:
                                    # Output is an independent copy of the exact original prefix.
                                    with output.open('xb') as out:
                                        out.write(memoryview(data)[:prefix.end]); out.flush(); os.fsync(out.fileno())
                                status='decoded_prefix'
                                detail['outputs']=[str(output.relative_to(root))]
                                detail['output_bytes']=prefix.end
                                detail['source_range']={'offset':offset,'length':prefix.end}
                            else:
                                status='decode_failed'; reason=checked['reason']
                    except (FormatError, struct.error) as e:
                        reason=str(e); detail['format_error']=reason
                    if signature(image)!=img_sig:
                        raise SafetyError('Source image changed during candidate validation; result not committed')
                    if status!='decoded_prefix' and windows<args.diagnostic_windows:
                        count=min(args.diagnostic_window_bytes,img_sig['size']-offset)
                        source.seek(offset); raw=source.read(count)
                        if len(raw)!=count: raise SafetyError('Short diagnostic image read')
                        dest=root/'blocked-windows'/f'offset-{offset:014d}-{uuid.uuid4().hex[:8]}.bin'
                        with dest.open('xb') as f: f.write(raw)
                        write_json(dest.with_suffix('.json'),{'image_offset':offset,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
                                                           'reason':reason,'privacy':'Raw source bytes may contain private video/audio.'})
                        detail['diagnostic_window']=str(dest.relative_to(root)); windows+=1
                    detail['status']=status; detail['reason']=reason
                    dp=attempt/'result.json'; write_json(dp,detail)
                    db.execute('INSERT INTO results VALUES(?,?,?,?,?)',(offset,status,reason,str(dp.relative_to(root)),time.time()))
                    db.commit(); done_now+=1
                    LOG.info('offset=%d status=%s fragments=%s reason=%s',offset,status,detail.get('fragment_count',0),reason)
            summary=export_report(db,root,binding,info)
            exported=True
            print(json.dumps(summary,indent=2)); print(f'Report: {root / "reports/index.html"}')
            return 0 if summary['status_counts'].get('decoded_prefix',0) else 3
        finally:
            if info is not None and verified_outputs and not exported:
                export_report(db,root,binding,info)
            db.close(); LOG.removeHandler(handler); handler.close()
