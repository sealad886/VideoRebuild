"""Export independently decoded excerpts from an indexed, mapped MP4."""
from __future__ import annotations

from bisect import bisect_right
import hashlib
import json
from pathlib import Path
import tempfile

from .artifacts import ensure_generated_file, safe_case_path
from .bmff import FormatError, iter_sample_nals, parse_movie, valid_sample
from .engine import reconstruct
from .ordinary_clip import OrdinaryClip
from .progress import phase
from .storage import ReaderPool, SafetyError, _candidate_header, file_sha256, write_json
from .validate import VALIDATION_REVISION, doctor, validate

KIND = 'ordinary_salvage'
SEARCH_REVISION = 1
MAX_CLIP_BYTES = 256 * 1024**2
MAX_SAMPLE_BYTES = 16 * 1024**2


def _locate(case, pool, sid):
    # Follow declared top-level boundaries so a moov or mdat signature inside
    # unrelated media cannot establish the history of the logical file.
    position = 0
    size = pool.sources[sid]['size']
    metadata = None
    metadata_extent = None
    payloads = []
    for _ in range(4096):
        if position == size:
            break
        header = _candidate_header(lambda at, n: pool.read(sid, at, n), position, size)
        if header is None:
            raise FormatError('unavailable or invalid top-level MP4 header')
        length, kind, width = header
        if length > size - position or (position == 0 and kind != b'ftyp') or kind == b'moof':
            raise FormatError('unsupported mapped ordinary MP4 boundaries')
        if kind == b'moov':
            if metadata is not None:
                raise FormatError('multiple top-level moov boxes')
            row = case.db.execute('''SELECT movie_digest FROM atoms
                WHERE source_id=? AND kind='moov' AND offset=? AND size=?''',
                                  (sid, position, length)).fetchone()
            if not row or not row[0]:
                raise FormatError('complete indexed ordinary moov required')
            raw = pool.read(sid, position, length)
            if hashlib.sha256(raw).hexdigest() != row[0]:
                raise SafetyError('Mapped MP4 metadata changed since indexing')
            metadata = parse_movie(raw)
            metadata_extent = {'logical_offset': position, 'length': length,
                               'physical_extents': pool.provenance(sid, position, length)}
        elif kind == b'mdat':
            payloads.append((position + width, position + length))
        position += length
    if position != size or metadata is None or not payloads:
        raise FormatError('mapped MP4 needs bounded top-level moov and mdat boxes')
    clip = OrdinaryClip(metadata)
    for chunk in clip.track.chunks:
        region = bisect_right(payloads, chunk.offset, key=lambda span: span[0]) - 1
        if region < 0 or chunk.offset + chunk.size > payloads[region][1]:
            raise FormatError('video sample table references bytes outside known mdat payloads')
    return clip, metadata_extent


def _idr(raw, track):
    hevc = track.codec == b'hvc1'
    types = {(nal[0] >> 1) & 63 if hevc else nal[0] & 31
             for nal in iter_sample_nals(raw, track.codec, track.nal_length)}
    slices = types & (set(range(32)) if hevc else {1, 2, 3, 4, 5, 19, 20, 21})
    return bool(slices) and slices <= ({19, 20} if hevc else {5})


def _good_gops(clip, pool, sid, detail):
    """Find bounded structurally intact GOPs using acquisition-gated reads."""
    track = clip.track
    first = None
    size = 0
    unlisted_sync = isinstance(clip.keyframes, range)
    nominated = iter(clip.keyframes)
    sync = next(nominated, None)
    for chunk in track.chunks:
        offset = chunk.offset
        for index, length in enumerate(chunk.sizes, chunk.sample_start):
            raw = b''
            if length <= MAX_SAMPLE_BYTES:
                try:
                    raw = pool.read(sid, offset, length)
                except FormatError:
                    detail['unavailable_samples'] += 1
            else:
                detail['oversized_samples'] += 1
            offset += length
            framed = bool(raw) and valid_sample(raw, track.codec, track.nal_length)
            if raw and not framed:
                detail['invalid_framing_samples'] += 1
            if index == sync:
                sync = next(nominated, None)
                idr = framed and _idr(raw, track)
                if idr or not unlisted_sync:
                    if first is not None:
                        yield first, index, size
                    first = index if idr else None
                    size = 0
            if not framed or size + length > MAX_CLIP_BYTES:
                first = None
            elif first is not None:
                size += length
    if first is not None:
        yield first, track.sample_count, size


def _runs(gops, metadata_bytes):
    run = []
    byte_count = 0
    # The projected table can add one co64 entry per original chunk. Leave room
    # for rebuilt metadata, then enforce the precise output limit in reconstruct.
    budget = MAX_CLIP_BYTES - metadata_bytes - 1024
    for gop in gops:
        if run and (run[-1][1] != gop[0] or byte_count + gop[2] > budget):
            yield run
            run, byte_count = [], 0
        run.append(gop)
        byte_count += gop[2]
    if run:
        yield run


def _saved_result(case, sid):
    prior = case.job(KIND, str(sid))
    if prior is None:
        return None
    for entry in prior['detail'].get('segments', []):
        ensure_generated_file(case, entry['path'], entry['sha256'], expected_size=entry['bytes'])
        ensure_generated_file(case, entry['manifest'], entry['manifest_sha256'])
    return prior


def _deterministic_rejection(result):
    if result['reason'] == 'strict_decode_failed':
        decode = result['decode']
        # POSIX keeps the low byte of FFmpeg's AVERROR_INVALIDDATA exit code.
        return (decode['returncode'] == 183
                and 'Invalid data found when processing input' in decode['stderr'])
    return result['reason'] in {'clip_geometry_rejected', 'track_count_mismatch',
                                'stream_or_sample_count_mismatch', 'decoded_video_frame_count_mismatch'}


def _export(case, pool, sid, clip, metadata_extent, first, stop, movie, tools, timeout, *, full):
    mapping = {(chunk.track, chunk.index): (sid, chunk.offset) for chunk in movie.chunks}
    work = safe_case_path(case, 'work')
    with tempfile.TemporaryDirectory(prefix='ordinary-salvage-', dir=work) as temp:
        stage = Path(temp) / 'clip.mp4'
        proof = reconstruct(movie, mapping, pool, stage, MAX_CLIP_BYTES)
        result = validate(stage, movie, tools, timeout)
        if not result['ok'] or full:
            return None, result

        stem = f'ordinary-{sid}-{clip.source_digest}-{first}-{stop}'
        target = f'salvaged/{stem}.mp4'
        manifest = f'salvaged/{stem}.json'
        manifest_stage = Path(temp) / 'clip.json'
        write_json(manifest_stage, {
            **proof, 'schema': 1, 'kind': KIND, 'clip': target,
            'source_moov_sha256': clip.source_digest, 'source_moov_extent': metadata_extent,
            'source_track_id': clip.track.track_id,
            'sample_start': first, 'sample_stop': stop, 'source_decode_start': clip.decode_start(first),
            'source_decode_stop': clip.decode_start(stop),
            'track_timescale': clip.track.timescale, 'timing': 'decode_timeline_rebased_to_clip_start',
            'audio': 'not_exported', 'association': 'filesystem_metadata_not_historical_identity',
            'validation': {'ok': True, 'reason': result['reason'], 'counts': result['counts'],
                           'revision': VALIDATION_REVISION,
                           'tool_versions': {name: tool['version'] for name, tool in tools.items()}},
        })
        manifest_sha256 = file_sha256(manifest_stage)
        pool.check(sid)
        ensure_generated_file(case, target, proof['sha256'], expected_size=proof['bytes'],
                              peers=(stage.relative_to(case.root),))
        ensure_generated_file(case, manifest, manifest_sha256, peers=(manifest_stage.relative_to(case.root),))
        return {'path': target, 'sha256': proof['sha256'], 'bytes': proof['bytes'], 'manifest': manifest,
                'manifest_sha256': manifest_sha256, 'sample_start': first, 'sample_stop': stop}, result


def _salvage(case, pool, sid, tools, timeout, max_attempts, retry):
    pool.check(sid)
    previous = _saved_result(case, sid)
    resumable = {'salvage_in_progress', 'salvage_search_limited'} if retry else {'salvage_in_progress'}
    if previous and previous['status'] not in resumable:
        return

    detail = {'source_id': sid, 'outputs': [], 'segments': [], 'attempts': 0, 'timed_out': False,
              'unavailable_samples': 0, 'invalid_framing_samples': 0, 'oversized_samples': 0, 'tools': tools}
    try:
        clip, metadata_extent = _locate(case, pool, sid)
    except FormatError as error:
        case.result(str(sid), 'salvage_blocked', {**detail, 'reason': str(error)}, kind=KIND)
        return
    detail['original_video_samples'] = clip.track.sample_count
    context = {'search_revision': SEARCH_REVISION, 'validation_revision': VALIDATION_REVISION,
               'source_moov_sha256': clip.source_digest, 'track_id': clip.track.track_id}
    detail['rejection_context'] = context
    detail['rejected_intervals'] = []
    if previous:
        prior = previous['detail']
        detail['segments'] = prior['segments']
        detail['outputs'] = [item['path'] for item in detail['segments']]
        if prior.get('rejection_context') == context and prior.get('tools') == tools:
            detail['rejected_intervals'] = prior.get('rejected_intervals', [])
    rejected = {tuple(interval) for interval in detail['rejected_intervals']}
    saved = {(item['sample_start'], item['sample_stop']) for item in detail['segments']}
    runs = _runs(_good_gops(clip, pool, sid, detail), clip.metadata_bytes)
    pending = []
    complete_video = False
    exhausted = False
    interrupted_attempt = False
    while True:
        run = pending.pop() if pending else next(runs, None)
        if run is None:
            exhausted = True
            break
        first, stop = run[0][0], run[-1][1]
        if (first, stop) in saved:
            continue
        if (first, stop) in rejected or (len(run) > 1 and any(first <= a < b <= stop for a, b in saved)):
            # Revisit the children without decoding a parent that already failed or overlaps saved clips.
            if len(run) > 1:
                middle = len(run) // 2
                pending.extend((run[middle:], run[:middle]))
            continue
        if detail['attempts'] >= max_attempts:
            break
        full = first == 0 and stop == clip.track.sample_count
        detail['attempts'] += 1
        try:
            movie = clip.project(first, stop, sync_samples=(gop[0] for gop in run))
            if movie.media_size + len(movie.data) + 1024 > MAX_CLIP_BYTES:
                raise FormatError('clip exceeds output limit')
            entry, result = _export(case, pool, sid, clip, metadata_extent, first, stop, movie,
                                    tools, timeout, full=full)
        except FormatError:
            entry, result = None, {'reason': 'clip_geometry_rejected'}
        reason = result['reason']
        decode, probe = result.get('decode', {}), result.get('probe', {})
        timed_out = bool(decode.get('timeout') or probe.get('timeout'))
        interrupted = decode.get('returncode', 0) < 0 or probe.get('returncode', 0) < 0
        # FFmpeg handles some signals itself and returns 255.
        interrupted |= decode.get('returncode') == 255
        if not (timed_out or interrupted) and _deterministic_rejection(result):
            rejected.add((first, stop))
            detail['rejected_intervals'].append((first, stop))
        if full and reason == 'decode_checked':
            complete_video = True
        if entry is not None:
            detail['segments'].append(entry)
            detail['outputs'].append(entry['path'])
            saved.add((first, stop))
            case.result(str(sid), 'salvage_in_progress', detail, kind=KIND)
        elif reason != 'decode_checked' and len(run) > 1 and not (timed_out or interrupted):
            middle = len(run) // 2
            pending.extend((run[middle:], run[:middle]))
        detail['timed_out'] |= timed_out
        interrupted_attempt |= interrupted and not timed_out

    detail['samples_salvaged'] = sum(item['sample_stop'] - item['sample_start'] for item in detail['segments'])
    if complete_video:
        status, reason = 'salvage_not_needed', 'the mapped video track decodes in full'
    elif interrupted_attempt:
        status, reason = 'salvage_search_limited', 'validation process interrupted'
    elif not exhausted or detail['timed_out']:
        status, reason = 'salvage_search_limited', 'validation attempt or time limit reached'
    elif detail['outputs']:
        status, reason = 'salvaged_segments', 'independently decoded video excerpts; source gaps and identity remain unresolved'
    else:
        status, reason = 'salvage_unresolved', 'no bounded, independently decoded video excerpt found'
    if status != 'salvage_search_limited':
        del detail['rejection_context'], detail['rejected_intervals']
    pool.check(sid)
    case.result(str(sid), status, {**detail, 'reason': reason}, kind=KIND)


def salvage_ordinary(case, timeout, max_attempts, retry):
    sources = case.sources()
    physical = case.physical_sources()
    with ReaderPool(sources, physical_sources=physical) as pool:
        eligible = [sid for sid, row in sources.items() if row['kind'] == 'logical'
                    and json.loads(row['binding']).get('association') == 'filesystem_metadata'
                    and all(physical[e.source_id]['kind'] == 'image' for e in pool.extents[sid])]
        if not eligible:
            raise SafetyError('Salvage needs an indexed filesystem-mapped image; run scan --image --extents first')
        tools = doctor()
        with phase('Salvage mapped ordinary MP4s', total=len(eligible), unit='recordings') as task:
            for sid in eligible:
                _salvage(case, pool, sid, tools, timeout, max_attempts, retry)
                task.advance()
