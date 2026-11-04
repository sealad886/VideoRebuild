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
from .bmff import FormatError, boxes
from .fragmented import (MAX_INIT, MAX_MOOF, FragmentCache,
                         parse_initialization, walk_prefix, walk_prefix_reader)
from .storage import SafetyError, file_sha256, write_json
from .validate import run_tool
from .progress import phase
from .candidate_tiers import CandidateTiers
from .artifacts import ensure_generated_file, safe_case_path

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
    hashed = 0
    with phase('Hash expected encoded packets', total=sum(prefix.counts.values()), unit='samples') as hash_task:
        for f in prefix.fragments:
            for s in f.samples:
                hashed += 1
                if hashed % 4096 == 0:
                    hash_task.update(hashed)
                expected[s.track_id].append(hashlib.sha256(view[s.offset:s.offset+s.size]).hexdigest())
                samples[s.track_id].append(s)
                # MOV demuxing normalizes negative composition offsets by adding
                # the largest negative offset's magnitude to every PTS in a track.
                shifts[s.track_id] = max(shifts[s.track_id], -s.composition_offset)
        hash_task.update(hashed)
    with phase('Probe fragmented packet evidence', total=1, unit='jobs') as validation_task:
        probe = run_tool([tools['ffprobe']['path'], *base, '-count_frames', '-count_packets',
                          '-show_streams', '-show_packets', '-show_data_hash', 'sha256',
                          '-show_entries', 'stream=index,id,codec_name,time_base,nb_read_frames,nb_read_packets:packet=stream_index,data_hash,dts,pts,duration',
                          '-of', 'json', str(candidate)], timeout, output_limit=64*1024*1024)
        validation_task.advance()
        if any((probe['returncode'], probe['timeout'], probe['stderr'].strip(), probe['stdout_truncated'])):
            validation_task.outcome('failed', detail='probe failed or incomplete')
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
            scale = Fraction(s['time_base']) * t.timescale
            scales[tid] = (scale.numerator, scale.denominator)
        actual = defaultdict(list)
        compared = 0
        with phase('Compare packet hashes and timing', total=len(obj.get('packets', [])), unit='samples') as compare_task:
            for compared, packet in enumerate(obj.get('packets', []), 1):
                if compared % 4096 == 0:
                    compare_task.update(compared)
                tid = indices[packet['stream_index']]
                index = len(actual[tid])
                if index >= len(samples[tid]): raise ValueError('packet_or_decoded_frame_count_mismatch')
                sample = samples[tid][index]
                dts = clocks[tid]
                wanted = (dts, dts+sample.composition_offset+shifts[tid], sample.duration)
                numerator, denominator = scales[tid]
                # Compare exact rational ticks without allocating three Fraction
                # products for every packet. Nonintegral values still fail.
                actual_ticks = tuple(int(packet[k])*numerator for k in ('dts', 'pts', 'duration'))
                if actual_ticks != tuple(ticks*denominator for ticks in wanted):
                    raise ValueError('packet_timing_mismatch')
                clocks[tid] += sample.duration
                actual[tid].append(packet['data_hash'].split(':',1)[1].lower())
            compare_task.update(compared)
            if dict(actual) != dict(expected): raise ValueError('encoded_packet_hash_mismatch')
    except (ValueError, KeyError, TypeError, ZeroDivisionError) as e:
        detail['reason'] = str(e)
        return detail
    # The hashes above are now summarized; don't duplicate huge JSON in a report.
    detail['probe']['stdout'] = json.dumps({'streams': streams, 'packet_sha256_comparison': 'exact_match',
                                          'packet_timing_comparison': 'exact_match',
                                          'pts_normalization_ticks': dict(shifts)})
    with phase('Strict decode fragmented candidate', total=1, unit='jobs') as validation_task:
        decode = run_tool([tools['ffmpeg']['path'], '-hide_banner', '-nostdin', '-xerror',
                           *base, '-err_detect', 'explode', '-i', str(candidate),
                           '-map', '0:v', '-map', '0:a?', '-threads', '1', '-filter_threads', '1',
                           '-f', 'null', '-'], timeout)
        validation_task.advance()
        if any((decode['returncode'], decode['timeout'], decode['stderr'].strip())):
            validation_task.outcome('failed', detail='strict decode failed')
    detail['decode'] = decode
    detail['ok'] = not any((decode['returncode'], decode['timeout'], decode['stderr'].strip()))
    detail['reason'] = 'packet_hashes_and_full_decode_passed' if detail['ok'] else 'strict_decode_failed'
    return detail


def verify_completed(case) -> None:
    """Verify committed publications and their evidence before doing new work."""
    with phase('Verify fragmented committed jobs', unit='jobs') as verification_task:
        for job in case.get_jobs('fragmented'):
            verification_task.advance()
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
                if item.get('manifest_path'):
                    manifest = case.root / item['manifest_path']
                    if (manifest.is_symlink() or not manifest.is_file() or
                            not manifest.resolve().is_relative_to(output_root) or
                            file_sha256(manifest) != item.get('manifest_sha256')):
                        raise SafetyError(f'Missing or changed fragmented manifest: {manifest}')
            if len(detail.get('output_evidence', [])) != len(detail['outputs']):
                raise SafetyError('Incomplete fragmented publication evidence')


def _fragment_jobs(case):
    """Visit bounded pages without keeping a database cursor over repairs."""
    previous = None
    while True:
        if previous is None:
            keys = case.db.execute("SELECT key FROM jobs WHERE kind='fragmented' ORDER BY key LIMIT 256").fetchall()
        else:
            keys = case.db.execute("SELECT key FROM jobs WHERE kind='fragmented' AND key>? ORDER BY key LIMIT 256",
                                   (previous,)).fetchall()
        if not keys:
            return
        for row in keys:
            yield case.job('fragmented', row[0])
        previous = keys[-1][0]


def _fragment_replay_ranges(pool, detail, segment, entry):
    sid, init_at = detail.get('source_id'), detail.get('logical_offset')
    start, end, size = segment.get('logical_start'), segment.get('logical_end'), entry.get('size')
    if (sid not in pool.sources or any(type(v) is not int for v in (init_at, start, end, size)) or
            min(init_at, start) < 0 or end <= start or not 0 < size-(end-start) <= MAX_INIT or
            not segment.get('source_ranges')):
        raise SafetyError('Insufficient committed source proof to recreate fragmented output')
    ranges = [(init_at, size-(end-start)), (start, end-start)]
    expected = [part for offset, length in ranges for part in pool.provenance(sid, offset, length)]
    signature = lambda parts: [(v['source_id'], v['source_offset'], v['length']) for v in parts]
    if signature(expected) != signature(segment['source_ranges']):
        raise SafetyError('Committed fragmented source ranges do not match indexed evidence')
    return sid, ranges


def restore_fragmented_job(case, pool, job):
    """Recreate absent generated artifacts from committed, exact-byte proof."""
    detail = job['detail']
    if not detail.get('outputs'):
        return
    entries = detail.get('output_evidence', [])
    names = ([detail['detail_path']] if detail.get('detail_path') else []) + list(detail['outputs'])
    names.extend(entry['manifest_path'] for entry in entries if entry.get('manifest_path'))
    # Normal resumes keep their existing stat/hash work in the owning verifiers.
    if all(safe_case_path(case, name).is_file() for name in names):
        return
    if (not detail.get('detail_path') or len(entries) != len(detail['outputs']) or
            {entry['path'] for entry in entries} != set(detail['outputs'])):
        raise SafetyError('Insufficient committed fragmented publication evidence')
    evidence_root = case.root/('attempts' if detail.get('historical_validation') else 'work')
    output_root = case.root/('recovered' if detail.get('historical_validation') else 'reconstructed')
    evidence = safe_case_path(case, detail['detail_path'])
    if not evidence.is_relative_to(evidence_root):
        raise SafetyError('Unsafe committed fragmented validation evidence path')
    for entry in entries:
        for name in (entry['path'], entry.get('manifest_path')):
            if name and not safe_case_path(case, name).is_relative_to(output_root):
                raise SafetyError('Unsafe committed fragmented publication path')
    sid = detail.get('source_id')
    if sid not in pool.sources:
        raise SafetyError('Insufficient committed source proof to recreate fragmented artifacts')
    pool.check(sid)
    if evidence.exists():
        ensure_generated_file(case, detail['detail_path'], detail.get('evidence_sha256'))
    for entry in entries:
        for name, digest, size in ((entry['path'], entry['sha256'], entry['size']),
                                   (entry.get('manifest_path'), entry.get('manifest_sha256'), None)):
            if name and safe_case_path(case, name).exists():
                ensure_generated_file(case, name, digest, expected_size=size)
    updated = json.loads(json.dumps(detail))
    segments = {segment.get('output'): segment for segment in updated.get('segments', [])}
    metadata_changed = not evidence.exists()
    for entry in updated['output_evidence']:
        output = safe_case_path(case, entry['path'])
        segment = segments.get(entry['path'], {})
        if not output.exists():
            sid, ranges = _fragment_replay_ranges(pool, updated, segment, entry)
            def replay(path):
                pool.check(sid)
                with path.open('xb') as stream:
                    for offset, length in ranges:
                        while length:
                            count = min(length, 1024*1024)
                            stream.write(pool.read(sid, offset, count))
                            offset += count
                            length -= count
                pool.check(sid)
            ensure_generated_file(case, entry['path'], entry['sha256'],
                                  expected_size=entry['size'], rebuild=replay)
        manifest_name = entry.get('manifest_path')
        if manifest_name and not safe_case_path(case, manifest_name).exists():
            sid, ranges = _fragment_replay_ranges(pool, updated, segment, entry)
            if entry['size'] > 256*1024*1024:
                raise SafetyError('Fragmented output exceeds bounded manifest recreation limit')
            raw = output.read_bytes()
            prefix = walk_prefix(raw, ranges[0][1], allow_later_start=True)
            if prefix.end != len(raw) or not prefix.fragments:
                raise SafetyError('Committed fragmented output cannot recreate its ordered manifest')
            manifest = _fragment_manifest(pool, sid, ranges[0][0], ranges[1][0], raw, prefix)
            checked = segment.get('validation', {}).get('decode_checked')
            decoded = checked if isinstance(checked, bool) else (
                segment.get('reconstruct_only') is not True and
                segment.get('status') in {'decoded_prefix', 'decoded_segment'})
            manifest.update(decode_checked=decoded,
                            validation_scope='strict_decode' if decoded else 'structural_reconstruction_only')
            if entry.get('manifest_status') == 'backfilled_from_existing_publication':
                manifest['status'] = entry['manifest_status']
            else:
                manifest['source_sha256'] = pool.sources[sid].get('sha256')
            # New metadata pointers keep an interrupted repair retryable even
            # when canonical serialization differs from the missing document.
            sidecar = output.with_name(output.name+'.manifest-'+uuid.uuid4().hex+'.json')
            payload = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True)+'\n').encode()
            digest = hashlib.sha256(payload).hexdigest()
            relative = str(sidecar.relative_to(case.root))
            ensure_generated_file(case, relative, digest, expected_size=len(payload),
                                  rebuild=lambda path: path.write_bytes(payload))
            entry.update(manifest_path=relative, manifest_sha256=digest)
            metadata_changed = True
    if metadata_changed:
        evidence_root = 'attempts' if updated.get('historical_validation') else 'work'
        relative = evidence_root+'/fragment-restored-'+uuid.uuid4().hex+'.json'
        updated['detail_path'] = relative
        updated.pop('evidence_sha256', None)
        payload = (json.dumps(updated, indent=2, sort_keys=True, ensure_ascii=True)+'\n').encode()
        digest = hashlib.sha256(payload).hexdigest()
        ensure_generated_file(case, relative, digest, expected_size=len(payload),
                              rebuild=lambda path: path.write_bytes(payload))
        updated['evidence_sha256'] = digest
        pool.check(sid)
        case.result(job['key'], job['status'], updated, kind='fragmented')
        job.update(case.job('fragmented', job['key']))


def _initialization_locations(case, pool, discovery_task):
    """Stream indexed locations before reading metadata for eligible jobs."""
    has_anchors = case.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='verified_anchors'").fetchone()
    for sid, source in sorted(pool.sources.items()):
        # Completed jobs may skip all media reads, but never source identity checks.
        pool.check(sid)
        constrained = bool(has_anchors and case.db.execute(
            'SELECT 1 FROM verified_anchors WHERE source_id=? LIMIT 1', (sid,)).fetchone())
        if constrained:
            rows = case.db.execute('SELECT offset FROM verified_anchors WHERE source_id=? ORDER BY offset', (sid,))
        else:
            rows = case.db.execute("SELECT offset FROM atoms WHERE source_id=? AND kind='ftyp' ORDER BY offset", (sid,))
        previous = next(rows, None)
        while previous is not None:
            discovery_task.advance()
            current = int(previous[0])
            following = next(rows, None)
            boundary = int(following[0]) if following is not None else source['size']
            previous = following
            yield sid, source, current, boundary


def _read_initialization(pool, sid, source, current, boundary):
    """Read one bounded, permitted initialization after checking job eligibility."""
    try:
        head_size = min(16, boundary-current)
        if not pool.permits(sid, current, head_size):
            return None
        head = pool.read(sid, current, head_size)
        # Use the small header only to bound the subsequent read, never to
        # accept a whole box without the canonical parser's length checks.
        ftyp_size = int.from_bytes(head[:4], 'big')
        if ftyp_size == 1:
            ftyp_size = int.from_bytes(head[8:16], 'big')
        if head[4:8] != b'ftyp' or not 8 <= ftyp_size <= 4096:
            return None
        moov_at = current+ftyp_size
        moov_head_size = min(16, boundary-moov_at)
        if moov_head_size <= 0 or not pool.permits(sid, moov_at, moov_head_size):
            return None
        moov_head = pool.read(sid, moov_at, moov_head_size)
        moov_size = int.from_bytes(moov_head[:4], 'big')
        if moov_size == 1:
            moov_size = int.from_bytes(moov_head[8:16], 'big')
        size = ftyp_size+moov_size
        if moov_head[4:8] != b'moov' or not ftyp_size+8 <= size <= MAX_INIT or current+size > boundary:
            return None
        if not pool.permits(sid, current, size):
            return None
        data = pool.read(sid, current, size)
        init = parse_initialization(data)
    except (FormatError, struct.error):
        # Ordinary MP4 metadata remains owned by the sample-table strategy.
        return None
    if current+size == boundary and source['kind'] != 'image':
        return None  # A header is discovery evidence, not a media job.
    return init


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
    if options.max_segments is not None and (isinstance(options.max_segments, bool) or
            not isinstance(options.max_segments, int) or options.max_segments <= 0):
        raise SafetyError('--max-segments must be a positive integer when supplied')


def _bind_search_policy(case, tools, options, retry: bool, allocation_scope) -> bool:
    """Keep evidence settings immutable; permit explicit search expansion on retry."""
    immutable = {'version': __version__, 'max_record_bytes': options.max_record_bytes,
                 'timeout': options.timeout, 'tools': tools}
    reconstruct_only = bool(getattr(options, 'reconstruct_only', False))
    policy = {'max_segments': options.max_segments, 'indexed_starts': 'through_next_initialization',
              'allocation_scope': allocation_scope}
    policy['candidate_tiers'] = 'unclaimed_first'
    policy['include_recovered'] = bool(getattr(options, 'include_recovered', False))
    if reconstruct_only:
        policy['reconstruct_only'] = True
    row = case.db.execute("SELECT value FROM settings WHERE name='fragmented_settings'").fetchone()
    prior = json.loads(row[0]) if row else None
    legacy = prior is not None and 'max_segments' in prior
    old_policy_row = case.db.execute("SELECT value FROM settings WHERE name='fragmented_search_policy'").fetchone()
    old_policy = json.loads(old_policy_row[0]) if old_policy_row else None
    if legacy:
        old_policy = {'max_segments': prior['max_segments'], 'indexed_starts': 'bounded_record_window'}
        prior = {key: value for key, value in prior.items() if key != 'max_segments'}
    mode_changed = old_policy is not None and bool(old_policy.get('reconstruct_only', False)) != reconstruct_only
    if prior is not None and prior != immutable:
        stable = lambda value: {key: item for key, item in value.items() if key != 'tools'}
        if not (retry and mode_changed and stable(prior) == stable(immutable)):
            raise SafetyError('Case settings changed for fragmented_settings; use a new case')
    changed = old_policy is not None and old_policy != policy
    if changed:
        old_limit = old_policy['max_segments']
        expanded = (options.max_segments is None or
                    old_limit is not None and options.max_segments >= old_limit)
        if not retry or not expanded:
            raise SafetyError('Fragmented search policy changed; expand it with --retry or use a new case')
    if legacy or changed:
        backup = case.root/'work'/('fragmented-settings-'+uuid.uuid4().hex+'.json')
        write_json(backup, {'fragmented_settings': json.loads(row[0]),
                            'fragmented_search_policy': old_policy})
        case.db.execute('UPDATE settings SET value=? WHERE name=?',
                        (json.dumps(immutable, sort_keys=True, separators=(',', ':')), 'fragmented_settings'))
        case.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                        ('fragmented_search_policy', json.dumps(policy, sort_keys=True, separators=(',', ':'))))
        case.event('fragmented_search_policy_expanded', {'before': old_policy, 'after': policy,
                                                       'mode_changed': mode_changed,
                                                       'backup': str(backup.relative_to(case.root))})
    else:
        case.bind('fragmented_settings', immutable)
        case.bind('fragmented_search_policy', policy)
    return changed


def _fragment_manifest(pool, sid, initialization_offset, start, retained, prefix):
    """Exact ordered source-to-output spans; no decoder or chronology inference."""
    init_size = len(prefix.initialization.data)
    pieces = []
    def append(kind, output_offset, size, logical_offset, **identity):
        if size <= 0:
            return
        extents = pool.provenance(sid, logical_offset, size)
        for extent in extents:
            row = pool.physical[extent['source_id']]
            extent['source_identity'] = {key: row.get(key) for key in ('dev', 'ino', 'size', 'mtime_ns', 'sha256')}
        pieces.append(dict(ordinal=len(pieces), kind=kind, source_id=sid,
                           logical_offset=logical_offset, length=size, output_offset=output_offset,
                           sha256=hashlib.sha256(memoryview(retained)[output_offset:output_offset+size]).hexdigest(),
                           physical_extents=extents, **identity))
    append('initialization', 0, init_size, initialization_offset)
    cursor = init_size
    for fragment in prefix.fragments:
        append('container', cursor, fragment.start-cursor, start+cursor-init_size)
        counts = defaultdict(int)
        for sample in fragment.samples:
            counts[sample.track_id] += 1
        append('media', fragment.start, fragment.end-fragment.start,
               start+fragment.start-init_size, sequence=fragment.sequence,
               content='moof_mdat_container_and_encoded_media',
               sample_counts=dict(counts), decode_times=dict(fragment.decode_times))
        cursor = fragment.end
    append('container', cursor, len(retained)-cursor, start+cursor-init_size)
    return {'schema': 1, 'kind': 'ordered-fragment-manifest',
            'output_sha256': hashlib.sha256(retained).hexdigest(), 'output_size': len(retained),
            'decode_checked': False, 'ordered_fragments': pieces}


def _fragment_manifest_path(output: Path) -> Path:
    """Keep previous sidecars intact while a new evidence pointer is pending."""
    sidecar = output.with_suffix('.json')
    if sidecar.exists() or sidecar.is_symlink():
        sidecar = output.with_name(output.name+'.manifest-'+uuid.uuid4().hex+'.json')
    return sidecar


def _backfill_fragment_manifests(case, pool):
    """Add ordered evidence for existing outputs without recopying or decoding."""
    for job in _fragment_jobs(case):
        detail = job['detail']
        changed = False
        segments = {segment.get('output'): segment for segment in detail.get('segments', [])}
        for entry in detail.get('output_evidence', []):
            if entry.get('manifest_path'):
                continue
            output = case.root / entry['path']
            segment = segments.get(entry['path'], {})
            checked = segment.get('validation', {}).get('decode_checked')
            decoded = checked if isinstance(checked, bool) else (
                segment.get('reconstruct_only') is not True and (
                    segment.get('status') in {'decoded_prefix', 'decoded_segment'} or
                    detail.get('decode_checked') is True))
            validation_scope = ('strict_decode' if decoded else
                                'structural_reconstruction_only' if segment.get('reconstruct_only') is True else
                                'historical_validation_unknown')
            manifest = {'schema': 1, 'kind': 'ordered-fragment-manifest',
                        'status': 'historical_manifest_unavailable',
                        'output_sha256': entry['sha256'], 'output_size': entry['size'],
                        'decode_checked': decoded, 'validation_scope': validation_scope}
            try:
                sid, init_at, start = detail.get('source_id'), detail.get('logical_offset'), segment.get('logical_start')
                if sid not in pool.sources or not isinstance(init_at, int) or not isinstance(start, int):
                    raise FormatError('historical_source_coordinates_unavailable')
                if not segment.get('source_ranges'):
                    raise FormatError('historical_physical_proof_unavailable')
                if entry['size'] > 256*1024*1024:
                    raise FormatError('historical_output_exceeds_bounded_manifest_parser')
                with output.open('rb') as stream:
                    raw = stream.read(entry['size']+1)
                if len(raw) != entry['size'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
                    raise SafetyError('Historical fragmented output changed during manifest backfill')
                roots = boxes(raw)
                ftyp, moov = next(roots), next(roots)
                if ftyp.kind != b'ftyp' or moov.kind != b'moov' or moov.end > MAX_INIT:
                    raise FormatError('historical_initialization_unavailable')
                prefix = walk_prefix(raw, moov.end, allow_later_start=True)
                if prefix.end != len(raw) or not prefix.fragments:
                    raise FormatError('historical_fragment_layout_not_fully_represented')
                # Reuse saved physical proof rather than infer another association.
                expected = pool.provenance(sid, init_at, moov.end) + pool.provenance(sid, start, len(raw)-moov.end)
                signature = lambda spans: [(v['source_id'], v['source_offset'], v['length']) for v in spans]
                if signature(expected) != signature(segment['source_ranges']):
                    raise FormatError('historical_physical_proof_does_not_match_coordinates')
                manifest = _fragment_manifest(pool, sid, init_at, start, raw, prefix)
                manifest.update(status='backfilled_from_existing_publication',
                                decode_checked=decoded, validation_scope=validation_scope)
            except (FormatError, StopIteration, KeyError, TypeError, ValueError) as error:
                manifest['reason'] = str(error) or 'historical_layout_unavailable'
            sidecar = _fragment_manifest_path(output)
            write_json(sidecar, manifest)
            entry.update(manifest_path=str(sidecar.relative_to(case.root)),
                         manifest_sha256=file_sha256(sidecar),
                         manifest_status=manifest.get('status', 'available'))
            changed = True
        if changed:
            evidence_root = case.root / ('attempts' if detail.get('historical_validation') else 'work')
            evidence_root.mkdir(exist_ok=True)
            evidence = evidence_root / ('fragment-manifest-backfill-'+uuid.uuid4().hex+'.json')
            detail['detail_path'] = str(evidence.relative_to(case.root))
            write_json(evidence, detail)
            detail['evidence_sha256'] = file_sha256(evidence)
            case.result(job['key'], job['status'], detail, kind='fragmented')


def _fragment_retry_pending(job, reconstruct_only: bool) -> bool:
    """Retained publications cannot complete failed requested-mode coverage."""
    detail = job['detail']
    outputs = set(detail.get('outputs', []))
    if job['status'] == 'search_limited' or not outputs:
        return True
    accepted = [segment for segment in detail.get('segments', [])
                if segment.get('output') in outputs]
    if any(bool(segment.get('reconstruct_only', False)) != reconstruct_only for segment in accepted):
        return True
    # Match the search loop's interval coverage. Old failed attempts stay as
    # evidence, but do not reopen starts already accepted in the requested mode.
    covered = iter(sorted((segment['logical_start'], segment['logical_end']) for segment in accepted))
    interval = next(covered, None)
    pending = sorted(segment['logical_start'] for segment in detail.get('segments', [])
                     if segment.get('status') in {'decode_failed', 'search_limited'} and
                     segment.get('output') not in outputs)
    for start in pending:
        while interval is not None and interval[1] <= start:
            interval = next(covered, None)
        if interval is None or start < interval[0]:
            return True
    return False


def recover_fragmented(case, pool, tools: dict, options, limit=None, retry: bool = False) -> int:
    """Recover bounded fMP4 sections through the canonical read and job owners.

    An initialization's first section retains prefix semantics. Restart sections
    need independent IDR starts and explicit per-track decode times. They remain
    separate files and never bridge unavailable bytes or unexplained boundaries.
    """
    max_bytes = options.max_record_bytes
    max_segments = options.max_segments
    timeout = options.timeout
    reconstruct_only = bool(getattr(options, 'reconstruct_only', False))
    include_recovered = bool(getattr(options, 'include_recovered', False))
    check_options(options)
    for saved in _fragment_jobs(case):
        restore_fragmented_job(case, pool, saved)
    verify_completed(case)
    _backfill_fragment_manifests(case, pool)
    allocation_scope = pool.allocation_scope.digest if pool.allocation_scope is not None else None
    policy_changed = _bind_search_policy(case, tools, options, retry, allocation_scope)
    tiers = CandidateTiers(case, pool)
    fragment_cache = FragmentCache()
    search_policy = {'max_segments': max_segments, 'indexed_starts': 'through_next_initialization',
                     'allocation_scope': allocation_scope}
    search_policy['candidate_tiers'] = 'unclaimed_first'
    search_policy['include_recovered'] = include_recovered
    if reconstruct_only:
        search_policy['reconstruct_only'] = True
    if policy_changed:
        for saved in case.get_jobs('fragmented'):
            prior = saved['detail']
            previous_scope = prior.get('search_policy', {}).get('allocation_scope')
            if prior.get('outputs') and previous_scope != allocation_scope:
                # The scope may exclude the initialization entirely, so retain
                # this distinction even when discovery cannot revisit the job.
                prior['historical_allocation_scope'] = previous_scope
                prior['current_allocation_scope'] = allocation_scope
                prior['historical_outputs'] = list(prior['outputs'])
                for segment in prior.get('segments', []):
                    segment.setdefault('allocation_scope', previous_scope)
                case.result(saved['key'], saved['status'], prior, kind='fragmented')
    processed = 0
    visited_sources = set()
    with phase('Fragmented initialization locations examined', unit='candidates') as discovery_task, phase('Fragmented jobs visited', unit='jobs') as job_task:
        for sid, source, offset, boundary in _initialization_locations(case, pool, discovery_task):
            visited_sources.add(sid)
            key = f'{sid}:{offset}'
            old = case.job('fragmented', key)
            if old is not None:
                job_task.advance()
            job_policy_changed = (old is not None and not old['detail'].get('historical_validation') and
                                  old['detail'].get('search_policy') != search_policy)
            if old is not None and not (policy_changed or job_policy_changed or retry and
                    _fragment_retry_pending(old, reconstruct_only)):
                pool.check(sid)
                continue
            if limit is not None and processed >= limit:
                break
            init = _read_initialization(pool, sid, source, offset, boundary)
            if init is None:
                continue
            if old is None:
                job_task.advance()
            end = boundary
            init_end = offset+len(init.data)
            if end <= init_end or max_bytes <= len(init.data):
                case.result(key, 'search_limited', {'source_id': sid, 'logical_offset': offset,
                            'reason': 'record_window_excludes_media', 'outputs': [],
                            'search_policy': search_policy}, kind='fragmented')
                processed += 1
                continue
            pool.check(sid)
            attempt = case.root / 'work' / ('fragment-'+uuid.uuid4().hex)
            attempt.mkdir()
            association = ('raw_image_candidate' if source['kind'] == 'image' else
                           'filesystem_metadata' if source['kind'] == 'logical' else 'recovered_file')
            detail = {'source_id': sid, 'logical_offset': offset, 'initialization_sha256': init.sha256,
                      'association': association, 'window_end': min(end, offset+max_bytes),
                      'search_end': end, 'next_initialization': boundary,
                      'tracks': {str(k): asdict(v) for k, v in init.tracks.items()},
                      'outputs': [], 'output_evidence': [], 'segments': [], 'gaps': [],
                      'reconstruct_only': reconstruct_only, 'decode_checked': False,
                      'timing_note': 'Decode times are media ticks, not wall-clock dates.',
                      'search_tiers': {'policy': 'unclaimed_first', 'searched': [1],
                                       'include_recovered': include_recovered,
                                       'second_tier_unsearched': True,
                                       'uniqueness_scope': 'searched_tiers_only'}}
            # Existing validated clips are immutable. Retry searches uncovered
            # starts without replacing a previously published bounded section.
            if old is not None and old['detail'].get('outputs'):
                for name in ('outputs', 'output_evidence', 'segments', 'gaps'):
                    detail[name] = list(old['detail'].get(name, []))
                for name in ('historical_allocation_scope', 'current_allocation_scope', 'historical_outputs'):
                    if name in old['detail']:
                        detail[name] = old['detail'][name]
            read_bytes = 0
            parse_stats_before = dict(fragment_cache.stats)

            def read_section(at: int, size: int) -> bytes:
                nonlocal read_bytes
                if size < 0 or size > max_bytes-len(init.data):
                    raise FormatError('invalid_section_read_size')
                raw = media_pool.read(sid, at, size)
                read_bytes += size
                section_task.heartbeat(read_bytes=read_bytes, starts_considered=considered)
                return raw

            considered = 0
            with phase('Fragmented section starts considered', unit='candidates') as section_task:
                for tier in (1, 2):
                    if tier == 2:
                        if not include_recovered or detail['outputs'] or detail.get('limit_reason'):
                            break
                        detail['search_tiers']['searched'].append(2)
                        detail['search_tiers']['second_tier_unsearched'] = False
                    media_pool = tiers.view(owner=('fragmented', key)) if tier == 1 else pool
                    published = iter(sorted((segment['logical_start'], segment['logical_end'])
                                            for segment in detail['segments'] if segment.get('output') and
                                            bool(segment.get('reconstruct_only', False)) == reconstruct_only))
                    published_range = next(published, None)
                    # Intervals describe availability, without shifting logical coordinates.
                    intervals = iter(media_pool.available_ranges(sid, init_end, end))
                    interval = next(intervals, None)
                    consumed = init_end
                    for start in _candidate_starts(case, sid, init_end, end):
                        # A view must not pin the previous full read window
                        # while a later candidate allocates its own window.
                        retained = None
                        while published_range is not None and published_range[1] <= start:
                            published_range = next(published, None)
                        if published_range is not None and published_range[0] <= start < published_range[1]:
                            consumed = max(consumed, published_range[1])
                            continue
                        if start < consumed:
                            continue
                        while interval is not None and interval[0]+interval[1] <= start:
                            interval = next(intervals, None)
                        if interval is None:
                            break
                        if start < interval[0]:
                            detail['gaps'].append({'start': start, 'end': interval[0], 'reason': 'unavailable_source_bytes'})
                            continue
                        if max_segments is not None and considered >= max_segments:
                            detail['limit_reason'] = 'segment_attempt_budget'
                            break
                        # Count malformed and non-IDR starts before any prefilter read.
                        considered += 1
                        section_task.update(considered, read_bytes=read_bytes)
                        pair_data = b''
                        readable_end = min(end, interval[0]+interval[1], start+max_bytes-len(init.data))
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
                                first = fragment_cache.parse(pair_data, init)
                                if not _restart_is_idr(first, init):
                                    continue
                                if any(base is None for base in first.decode_times.values()):
                                    # A track already present cannot acquire its missing
                                    # restart origin from a later fragment. Tracks absent
                                    # here may still appear later with explicit timing.
                                    counts = defaultdict(int)
                                    for sample in first.samples:
                                        counts[sample.track_id] += 1
                                    detail['segments'].append({
                                        'logical_start': start, 'logical_end': start+len(pair_data),
                                        'later_section': True, 'fragment_count': 1,
                                        'sample_counts': dict(counts), 'allocation_scope': allocation_scope,
                                        'status': 'blocked', 'reason': 'missing_restart_track_timing',
                                        'stop_reason': 'missing_restart_track_timing'})
                                    continue
                            except (FormatError, struct.error):
                                continue
                        def read_payload(at, size):
                            return read_section(start+at, size)

                        data, prefix = walk_prefix_reader(init, read_payload, readable_end-start,
                                                        allow_later_start=later, first_pair=pair_data,
                                                        cache=fragment_cache)
                        if (not later and not prefix.fragments and
                                prefix.stop_reason.startswith('missing_initial_fragments_sequence_starts_at_')):
                            # A logical file may begin at a surviving later fragment. Keep
                            # the same restart checks and label; never call it a prefix.
                            later = True
                            data, prefix = walk_prefix_reader(init, read_payload, readable_end-start,
                                                            allow_later_start=True, first_pair=pair_data,
                                                            cache=fragment_cache)
                        segment = {'logical_start': start, 'logical_end': start+prefix.end-len(init.data),
                                   'stop_reason': prefix.stop_reason, 'fragment_count': len(prefix.fragments),
                                   'sample_counts': prefix.counts, 'later_section': later,
                                   'allocation_scope': allocation_scope, 'candidate_tier': tier,
                                   'read_window_end': readable_end,
                                   'window_limited': readable_end < min(end, interval[0]+interval[1]),
                                   'source_ranges': pool.provenance(sid, offset, len(init.data)),
                                   'starts': prefix.starts}
                        error = prefix.eligibility_error()
                        if not error and later and not _restart_has_timing(prefix):
                            error = 'missing_restart_track_timing'
                        if error:
                            segment.update(status='blocked', reason=error)
                            detail['segments'].append(segment)
                            continue
                        retained = memoryview(data)[:prefix.end]
                        source_length = prefix.end-len(init.data)
                        segment['source_ranges'] += pool.provenance(sid, start, source_length)
                        if len(retained) > options.max_output_bytes:
                            segment.update(status='search_limited', reason='output_size_budget')
                            detail['segments'].append(segment)
                            continue
                        if shutil.disk_usage(case.root).free < len(retained)*2+1024*1024:
                            raise SafetyError('Insufficient free space for fragmented candidate and publication')
                        candidate = attempt / f'section-{start}-tier-{tier}.mp4'
                        with phase('Write fragmented candidate', total=len(retained), unit='bytes') as write_task, candidate.open('xb') as stream:
                            stream.write(retained)
                            write_task.update(len(retained))
                            stream.flush()
                            os.fsync(stream.fileno())
                        checked = ({'ok': True, 'decode_checked': False,
                                    'scope': 'structural_reconstruction_only',
                                    'reason': 'structural_reconstruction_only'} if reconstruct_only else
                                   validate_prefix(candidate, data, prefix, tools, timeout))
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
                            with phase('Publish fragmented section', total=len(retained), unit='bytes') as publish_task, output.open('xb') as stream:
                                stream.write(retained)
                                publish_task.update(len(retained))
                                stream.flush()
                                os.fsync(stream.fileno())
                        manifest = _fragment_manifest(pool, sid, offset, start, retained, prefix)
                        manifest['decode_checked'] = not reconstruct_only
                        manifest['validation_scope'] = 'structural_reconstruction_only' if reconstruct_only else 'strict_decode'
                        manifest['source_sha256'] = source.get('sha256')
                        sidecar = _fragment_manifest_path(output)
                        write_json(sidecar, manifest)
                        relative = str(output.relative_to(case.root))
                        segment.update(status=('reconstructed_segment' if later else 'reconstructed_prefix') if reconstruct_only else
                                              ('decoded_segment' if later else 'decoded_prefix'),
                                       reason='structural_reconstruction_only' if reconstruct_only else 'packet_hashes_timing_and_decode_passed',
                                       reconstruct_only=reconstruct_only, output=relative,
                                       first_sequence=prefix.fragments[0].sequence,
                                       last_sequence=prefix.fragments[-1].sequence,
                                       durations_seconds={str(tid): (clock-prefix.starts[tid])/init.tracks[tid].timescale
                                                          for tid, clock in prefix.clocks.items()})
                        if start > consumed:
                            detail['gaps'].append({'start': consumed, 'end': start,
                                                   'reason': 'not_exported_between_sections'})
                        detail['segments'] = [saved for saved in detail['segments'] if saved.get('output') != relative]
                        detail['output_evidence'] = [saved for saved in detail['output_evidence'] if saved['path'] != relative]
                        if relative not in detail['outputs']:
                            detail['outputs'].append(relative)
                        detail['output_evidence'].append({'path': relative, 'size': len(retained), 'sha256': digest,
                                                               'manifest_path': str(sidecar.relative_to(case.root)),
                                                               'manifest_sha256': file_sha256(sidecar)})
                        detail['segments'].append(segment)
                        consumed = max(consumed, segment['logical_end'])
                        # Commit publication evidence before it can demote candidate references.
                        detail['search_policy'] = search_policy
                        publication = attempt / f'publication-{start}.json'
                        detail['detail_path'] = str(publication.relative_to(case.root))
                        write_json(publication, detail)
                        detail['evidence_sha256'] = file_sha256(publication)
                        case.result(key, segment['status'], detail, kind='fragmented')
                        tiers.refresh(('fragmented', key))
                if detail.get('limit_reason'):
                    section_task.outcome('limited', detail=detail['limit_reason'])
            pool.check(sid)
            detail['candidate_starts_considered'] = considered
            detail['media_read_bytes_charged'] = read_bytes
            detail['parse_counters'] = {name: value-parse_stats_before[name]
                                        for name, value in fragment_cache.stats.items()}
            detail['section_window_bytes'] = max_bytes
            detail['search_policy'] = search_policy
            if detail.get('limit_reason'):
                status = 'search_limited'
            elif detail['outputs']:
                unchecked = reconstruct_only or any(s.get('reconstruct_only', False) for s in detail['segments'] if s.get('output'))
                detail['decode_checked'] = not unchecked
                later_outputs = any(s['later_section'] for s in detail['segments'] if s.get('output'))
                status = ('reconstructed_segments' if later_outputs else 'reconstructed_prefix') if unchecked else (
                          'decoded_segments' if later_outputs else 'decoded_prefix')
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
    # Reused jobs do not enter the per-job read/finish checks. Recheck every
    # visited source even when discovery ends or the pending-job limit breaks.
    for sid in visited_sources:
        pool.check(sid)
    return processed
