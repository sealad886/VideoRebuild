"""Fragmented-MP4 recovery through the canonical case and logical byte reader.

Each initialization location is a separate job. Later sections remain separate
clips with explicit timing and provenance; no unavailable media is inferred.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from fractions import Fraction
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import struct
import uuid

from . import __version__
from .bmff import FormatError
from .fragmented import MAX_INIT, MAX_MOOF, parse_fragment, parse_initialization, walk_prefix
from .storage import SafetyError, file_sha256, write_json
from .validate import run_tool

LOG = logging.getLogger(__name__)


def signature(p: Path) -> dict:
    s = p.stat()
    if not stat.S_ISREG(s.st_mode):
        raise SafetyError(f'Not a regular file: {p}. Device nodes are not accepted.')
    return {'path': str(p), 'size': s.st_size, 'mtime_ns': s.st_mtime_ns,
            'ctime_ns': s.st_ctime_ns, 'inode': s.st_ino, 'device': s.st_dev}


class XMLReader:
    """Bounded read chunks, reject declarations that could expand entities."""
    def __init__(self, f): self.f, self.tail = f, b''
    def read(self, n=-1):
        b = self.f.read(65536 if n < 0 else min(n, 65536))
        # Inspect ASCII declaration tokens through UTF-16 NUL padding, while
        # preserving the original bytes for ElementTree's encoding detection.
        check = (self.tail+b).replace(b'\x00', b'').upper()
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


def verify_completed(case) -> None:
    """Verify committed publications and their evidence before doing new work."""
    for job in case.get_jobs('fragmented'):
        detail = job['detail']
        if not detail.get('outputs'):
            continue
        historical = bool(detail.get('historical_validation'))
        evidence_root = case.root / ('attempts' if historical else 'work')
        output_root = case.root / ('recovered' if historical else 'reconstructed')
        evidence = case.root / detail.get('detail_path', '')
        if (evidence.is_symlink() or not evidence.is_file() or
                not evidence.resolve().is_relative_to(evidence_root) or
                file_sha256(evidence) != detail.get('evidence_sha256')):
            raise SafetyError(f'Missing or changed fragmented validation evidence: {evidence}')
        for item in detail.get('output_evidence', []):
            output = case.root / item['path']
            if (output.is_symlink() or not output.is_file() or
                    not output.resolve().is_relative_to(output_root) or
                    output.stat().st_size != item['size'] or file_sha256(output) != item['sha256']):
                raise SafetyError(f'Missing or changed fragmented publication: {output}')
        if len(detail.get('output_evidence', [])) != len(detail['outputs']):
            raise SafetyError('Incomplete fragmented publication evidence')


def _initializations(case, pool):
    """Stream distinct initialization locations from the shared source catalogue."""
    has_anchors = case.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='verified_anchors'").fetchone()
    for sid, source in sorted(pool.sources.items()):
        constrained = bool(has_anchors and case.db.execute(
            'SELECT 1 FROM verified_anchors WHERE source_id=? LIMIT 1', (sid,)).fetchone())
        if constrained:
            rows = case.db.execute('SELECT offset FROM verified_anchors WHERE source_id=? ORDER BY offset', (sid,))
        else:
            rows = case.db.execute("SELECT offset FROM atoms WHERE source_id=? AND kind='ftyp' ORDER BY offset", (sid,))
        previous = next(rows, None)
        while previous is not None:
            current = int(previous[0])
            following = next(rows, None)
            boundary = int(following[0]) if following is not None else source['size']
            previous = following
            try:
                head = pool.read(sid, current, min(16, boundary-current))
                # box_at deliberately checks lengths; use the small header only to
                # bound the subsequent source read, never to accept a whole box.
                ftyp_size = int.from_bytes(head[:4], 'big')
                if ftyp_size == 1:
                    ftyp_size = int.from_bytes(head[8:16], 'big')
                if head[4:8] != b'ftyp' or not 8 <= ftyp_size <= 4096:
                    continue
                moov_head = pool.read(sid, current+ftyp_size, min(16, boundary-current-ftyp_size))
                moov_size = int.from_bytes(moov_head[:4], 'big')
                if moov_size == 1:
                    moov_size = int.from_bytes(moov_head[8:16], 'big')
                size = ftyp_size+moov_size
                if moov_head[4:8] != b'moov' or not ftyp_size+8 <= size <= MAX_INIT or current+size > boundary:
                    continue
                data = pool.read(sid, current, size)
                init = parse_initialization(data)
            except (FormatError, struct.error):
                # Ordinary MP4 metadata remains owned by the sample-table strategy.
                continue
            if current+size == boundary and source['kind'] != 'image':
                continue  # A header is evidence for discovery, not a media job.
            yield sid, source, current, boundary, init


def _restart_is_idr(fragment, init) -> bool:
    first = {}
    for sample in fragment.samples:
        first.setdefault(sample.track_id, sample)
    videos = [track for track in init.tracks.values() if track.handler == 'vide']
    for track in videos:
        sample = first.get(track.track_id)
        idrs = {5} if track.codec in ('avc1', 'avc3') else {19, 20}
        if sample is None or sample.flags & 0x10000 or not idrs.intersection(sample.nal_types):
            return False
    return bool(videos)


def _restart_has_timing(prefix) -> bool:
    first = {}
    for fragment in prefix.fragments:
        for tid, base in fragment.decode_times.items():
            first.setdefault(tid, base)
    return all(first.get(tid) is not None for tid in prefix.initialization.tracks)


def _candidate_starts(case, sid: int, start: int, end: int):
    yield start
    for row in case.db.execute("SELECT offset FROM atoms WHERE source_id=? AND kind='moof' AND offset>? AND offset<? ORDER BY offset", (sid, start, end)):
        yield int(row[0])


def check_options(options) -> None:
    """Validate fragmented recovery policy before any case or output mutation."""
    if not 1024 <= options.max_record_bytes <= 256*1024*1024:
        raise SafetyError('--max-record-bytes must be between 1024 and 268435456')
    if not 1 <= options.max_segments <= 1024:
        raise SafetyError('--max-segments must be between 1 and 1024')


def recover_fragmented(case, pool, tools: dict, options, limit=None, retry: bool = False) -> int:
    """Recover bounded fMP4 sections through the canonical read and job owners.

    An initialization's first section retains prefix semantics. Restart sections
    need independent IDR starts and explicit per-track decode times. They remain
    separate files and never bridge unavailable bytes or unexplained boundaries.
    """
    max_bytes = options.max_record_bytes
    max_segments = options.max_segments
    timeout = options.timeout
    check_options(options)
    case.bind('fragmented_settings', {'version': __version__, 'max_record_bytes': max_bytes,
                                    'max_segments': max_segments, 'timeout': timeout, 'tools': tools})
    verify_completed(case)
    processed = 0
    for sid, source, offset, boundary, init in _initializations(case, pool):
        key = f'{sid}:{offset}'
        old = case.job('fragmented', key)
        if old is not None and (not retry or old['detail'].get('outputs')):
            continue
        if limit is not None and processed >= limit:
            break
        end = min(boundary, offset+max_bytes)
        init_end = offset+len(init.data)
        if end <= init_end:
            case.result(key, 'search_limited', {'source_id': sid, 'logical_offset': offset,
                        'reason': 'record_window_excludes_media', 'outputs': []}, kind='fragmented')
            processed += 1
            continue
        pool.check(sid)
        attempt = case.root / 'work' / ('fragment-'+uuid.uuid4().hex)
        attempt.mkdir()
        association = ('raw_image_candidate' if source['kind'] == 'image' else
                       'filesystem_metadata' if source['kind'] == 'logical' else 'recovered_file')
        detail = {'source_id': sid, 'logical_offset': offset, 'initialization_sha256': init.sha256,
                  'association': association, 'window_end': end, 'next_initialization': boundary,
                  'tracks': {str(k): asdict(v) for k, v in init.tracks.items()},
                  'outputs': [], 'output_evidence': [], 'segments': [], 'gaps': [],
                  'timing_note': 'Decode times are media ticks, not wall-clock dates.'}
        read_budget = 2*(end-offset)
        read_bytes = 0

        class ReadBudgetExceeded(Exception):
            pass

        def read_section(at: int, size: int) -> bytes:
            nonlocal read_bytes
            if size < 0:
                raise FormatError('negative_section_read')
            if read_bytes+size > read_budget:
                raise ReadBudgetExceeded
            read_bytes += size
            return pool.read(sid, at, size)

        # Intervals describe availability, without shifting logical coordinates.
        intervals = iter(pool.available_ranges(sid, init_end, end))
        interval = next(intervals, None)
        consumed = init_end
        considered = 0
        for start in _candidate_starts(case, sid, init_end, end):
            if start < consumed:
                continue
            while interval is not None and interval[0]+interval[1] <= start:
                interval = next(intervals, None)
            if interval is None:
                break
            if start < interval[0]:
                detail['gaps'].append({'start': start, 'end': interval[0], 'reason': 'unavailable_source_bytes'})
                continue
            if considered >= max_segments:
                detail['limit_reason'] = 'segment_attempt_budget'
                break
            # Count malformed and non-IDR starts before any prefilter read.
            considered += 1
            pair_data = b''
            readable_end = min(end, interval[0]+interval[1])
            later = start != init_end
            if later:
                # Reject non-random-access starts using one bounded pair before
                # parsing an entire section or launching external decoders.
                try:
                    header = read_section(start, min(16, readable_end-start))
                    moof_size = int.from_bytes(header[:4], 'big')
                    if moof_size == 1:
                        moof_size = int.from_bytes(header[8:16], 'big')
                    if header[4:8] != b'moof' or not 8 <= moof_size <= MAX_MOOF:
                        continue
                    mdat_head = read_section(start+moof_size, min(16, readable_end-start-moof_size))
                    mdat_size = int.from_bytes(mdat_head[:4], 'big')
                    if mdat_size == 1:
                        mdat_size = int.from_bytes(mdat_head[8:16], 'big')
                    length = moof_size+mdat_size
                    if not moof_size+8 <= length <= readable_end-start:
                        continue
                    pair_data = read_section(start, length)
                    first = parse_fragment(pair_data, 0, init)
                    if not _restart_is_idr(first, init):
                        continue
                except ReadBudgetExceeded:
                    detail['limit_reason'] = 'segment_read_byte_budget'
                    break
                except (FormatError, struct.error):
                    continue
            try:
                payload = pair_data+read_section(start+len(pair_data), readable_end-start-len(pair_data))
            except ReadBudgetExceeded:
                detail['limit_reason'] = 'segment_read_byte_budget'
                break
            data = init.data+payload
            prefix = walk_prefix(data, len(init.data), allow_later_start=later)
            if (not later and not prefix.fragments and
                    prefix.stop_reason.startswith('missing_initial_fragments_sequence_starts_at_')):
                # A logical file may begin at a surviving later fragment. Keep
                # the same restart checks and label; never call it a prefix.
                later = True
                prefix = walk_prefix(data, len(init.data), allow_later_start=True)
            segment = {'logical_start': start, 'logical_end': start+prefix.end-len(init.data),
                       'stop_reason': prefix.stop_reason, 'fragment_count': len(prefix.fragments),
                       'sample_counts': prefix.counts, 'later_section': later,
                       'source_ranges': pool.provenance(sid, offset, len(init.data)),
                       'starts': prefix.starts}
            error = prefix.eligibility_error()
            if not error and later and not _restart_has_timing(prefix):
                error = 'missing_restart_track_timing'
            if error:
                segment.update(status='blocked', reason=error)
                detail['segments'].append(segment)
                continue
            retained = data[:prefix.end]
            source_length = prefix.end-len(init.data)
            segment['source_ranges'] += pool.provenance(sid, start, source_length)
            if len(retained) > options.max_output_bytes:
                segment.update(status='search_limited', reason='output_size_budget')
                detail['segments'].append(segment)
                continue
            if shutil.disk_usage(case.root).free < len(retained)*2+1024*1024:
                raise SafetyError('Insufficient free space for fragmented candidate and publication')
            candidate = attempt / f'section-{start}.mp4'
            with candidate.open('xb') as stream:
                stream.write(retained)
                stream.flush()
                os.fsync(stream.fileno())
            checked = validate_prefix(candidate, data, prefix, tools, timeout)
            # Exact packet comparison needs a bounded large probe response, but
            # failed responses must not accumulate that response per section.
            for command in ('probe', 'decode'):
                evidence = checked.get(command, {})
                for channel in ('stdout', 'stderr'):
                    value = evidence.get(channel, '')
                    if len(value) > 65536:
                        evidence[channel] = value[:65536]
                        evidence[channel+'_retained_truncated'] = True
            segment['validation'] = checked
            if not checked['ok']:
                segment.update(status='decode_failed', reason=checked['reason'])
                detail['segments'].append(segment)
                continue
            output = case.root / 'reconstructed' / f'fragment-{sid}-{offset}-{start}.mp4'
            digest = hashlib.sha256(retained).hexdigest()
            if output.exists() or output.is_symlink():
                if output.is_symlink() or not output.is_file() or file_sha256(output) != digest:
                    raise SafetyError(f'Conflicting existing fragmented output: {output}')
            else:
                with output.open('xb') as stream:
                    stream.write(retained)
                    stream.flush()
                    os.fsync(stream.fileno())
            relative = str(output.relative_to(case.root))
            segment.update(status='decoded_segment' if later else 'decoded_prefix',
                           reason='packet_hashes_timing_and_decode_passed', output=relative,
                           first_sequence=prefix.fragments[0].sequence,
                           last_sequence=prefix.fragments[-1].sequence,
                           durations_seconds={str(tid): (clock-prefix.starts[tid])/init.tracks[tid].timescale
                                              for tid, clock in prefix.clocks.items()})
            if start > consumed:
                detail['gaps'].append({'start': consumed, 'end': start,
                                       'reason': 'not_exported_between_sections'})
            detail['outputs'].append(relative)
            detail['output_evidence'].append({'path': relative, 'size': len(retained), 'sha256': digest})
            detail['segments'].append(segment)
            consumed = max(consumed, segment['logical_end'])
        pool.check(sid)
        detail['candidate_starts_considered'] = considered
        detail['media_read_bytes_charged'] = read_bytes
        detail['media_read_byte_budget'] = read_budget
        if detail.get('limit_reason'):
            status = 'search_limited'
        elif detail['outputs']:
            status = 'decoded_segments' if any(s['later_section'] and s['status'] == 'decoded_segment' for s in detail['segments']) else 'decoded_prefix'
        elif any(s['status'] == 'decode_failed' for s in detail['segments']):
            status = 'decode_failed'
        else:
            status = 'blocked'
        detail['reason'] = detail.get('limit_reason') or (detail['segments'][-1].get('reason', detail['segments'][-1]['stop_reason']) if detail['segments'] else 'no_eligible_rescued_media')
        detail['status'] = status
        evidence = attempt / 'result.json'
        detail['detail_path'] = str(evidence.relative_to(case.root))
        write_json(evidence, detail)
        detail['evidence_sha256'] = file_sha256(evidence)
        case.result(key, status, detail, kind='fragmented')
        processed += 1
        LOG.info('Fragmented source=%s offset=%d status=%s outputs=%d', sid, offset, status, len(detail['outputs']))
    return processed
