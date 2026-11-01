"""Automated payload matching, offset rewriting, and scattered-extent search.

Matching is a hypothesis test, not proof of identity. Surviving metadata supplies
sample lengths/timing; NAL framing rejects most wrong associations. Default
publication also requires independent full decodes and exact packet/frame counts;
reconstruction-only mode explicitly retains non-decoded evidence. Alternatives and
search limits are retained instead of being hidden behind a confidence score.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping as MappingView
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
import hashlib
from itertools import accumulate, islice
import errno
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import struct
import sqlite3
import tempfile
from typing import Iterable

from . import __version__
from .bmff import Chunk, FormatError, Movie, make_box, parse_movie, rewrite_offsets, valid_sample
from .storage import Case, ReaderPool, SafetyError, file_sha256, json_text, write_json
from .validate import VALIDATION_REVISION, doctor, validate
from .progress import phase

LOG = logging.getLogger(__name__)
FTYP = make_box(b'ftyp', b'isom' + struct.pack('>I', 512) + b'isomiso6')
Mapping = MappingView[tuple[int, int], tuple[int, int]]
MAX_DEEP_METADATA = 32 * 1024 * 1024


def _quick_sample_ranges(chunk: Chunk) -> tuple[tuple[int, int], ...]:
    """Keep the sparse screen's exact sample choices as relative byte ranges."""
    count = len(chunk.sizes)
    indexes = (sorted({0, 1, 2, count//2, count-2, count-1}) if count > 6 else range(count))
    sizes = iter(chunk.sizes)
    offset = previous = 0
    ranges = []
    for index in indexes:
        offset += sum(islice(sizes, index - previous))
        size = next(sizes)
        ranges.append((offset, size))
        offset += size
        previous = index + 1
    return tuple(ranges)


@dataclass(frozen=True)
class Options:
    max_candidates: int | None = None
    max_padding: int | None = None
    timeout: float = 300.0
    max_output_bytes: int = 16 * 1024**3
    deep: bool = False
    reconstruct_only: bool = False
    include_recovered: bool = False
    max_groups: int = 200_000
    max_hits: int | None = None
    max_assemblies: int | None = None
    max_anchor_checks: int | None = None

    def check(self) -> None:
        for k, v in asdict(self).items():
            if k in {'deep', 'reconstruct_only', 'include_recovered'} or (v is None and k in {'max_candidates', 'max_padding', 'max_hits', 'max_assemblies', 'max_anchor_checks'}):
                continue
            if not math.isfinite(v) or (v < 0 if k == 'max_padding' else v <= 0):
                raise SafetyError(f'{k} must be a positive finite value (padding may be zero)')


class _TranslationPlan:
    """Movie invariants shared by uniform-offset hypotheses; no source bytes cached."""
    def __init__(self, movie: Movie):
        self.movie = movie
        self.offsets = {(c.track, c.index): c.offset for c in movie.chunks}
        self.ordered = sorted(self.offsets.items())
        self.minimum = min(self.offsets.values())
        self.maximum = max(self.offsets[(c.track, c.index)] + c.size for c in movie.chunks)
        video = [c for c in movie.chunks if movie.tracks[c.track].handler == b'vide']
        if len(video) > 12:
            indexes = sorted({0, 1, 2, len(video)//2, len(video)-3, len(video)-2, len(video)-1})
            video = [video[i] for i in indexes]
        self.quick_chunks = video
        self.quick_samples = {(c.track, c.index): _quick_sample_ranges(c) for c in video}

    def mapping(self, sid: int, translation: int):
        return _TranslatedMapping(self, sid, translation)


class _TranslatedMapping(MappingView):
    """Read-only mapping with exactly the same assignments as its eager dict."""
    def __init__(self, plan: _TranslationPlan, sid: int, translation: int):
        self.plan, self.sid, self.translation = plan, sid, translation

    def __getitem__(self, key):
        return self.sid, self.translation + self.plan.offsets[key]

    def __iter__(self):
        return iter(self.plan.offsets)

    def __len__(self):
        return len(self.plan.offsets)

    def check_source(self, pool: ReaderPool):
        if (self.sid not in pool.sources or self.translation + self.plan.minimum < 0 or
                self.translation + self.plan.maximum > pool.sources[self.sid]['size']):
            raise FormatError('chunk outside source')
        pool.check(self.sid)


def mapping_digest(movie: Movie, mapping: Mapping) -> str:
    if isinstance(mapping, _TranslatedMapping) and mapping.plan.movie is movie:
        rows = [(t, c, mapping.sid, mapping.translation + off)
                for (t, c), off in mapping.plan.ordered]
    else:
        rows = sorted((t, c, sid, off) for (t, c), (sid, off) in mapping.items())
    value = [movie.digest, rows]
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def source_ranges(movie: Movie, mapping: Mapping) -> list[tuple[int, int, int]]:
    return [(mapping[(c.track, c.index)][0], mapping[(c.track, c.index)][1], c.size)
            for c in movie.chunks]


def _mapping_ranges(movie: Movie, mapping: Mapping, pool: ReaderPool) -> list[tuple[int, int, int]]:
    expected = {(c.track, c.index) for c in movie.chunks}
    if set(mapping) != expected:
        raise FormatError('incomplete chunk assignment')
    ranges = sorted(source_ranges(movie, mapping))
    for sid, off, n in ranges:
        if sid not in pool.sources or off < 0 or off+n > pool.sources[sid]['size']:
            raise FormatError('chunk outside source')
    return ranges


def validate_ranges(movie: Movie, mapping: Mapping, pool: ReaderPool) -> None:
    ranges = _mapping_ranges(movie, mapping, pool)
    physical = []
    for sid, off, n in ranges:
        for extent in pool.provenance(sid,off,n):
            key = pool.physical_key(extent['source_id'])
            physical.append((*key,extent['source_offset'],extent['length']))
    for a, b in zip(ranges, ranges[1:]):
        if a[0] == b[0] and a[1]+a[2] > b[1]:
            raise FormatError('source byte ranges reused or overlapped')
    physical.sort()
    for a,b in zip(physical,physical[1:]):
        if a[:2] == b[:2] and a[2]+a[3] > b[2]:
            raise FormatError('physical source bytes reused or overlapped')


def check_samples(movie: Movie, mapping: Mapping, pool: ReaderPool, quick: bool = False,
                  chunks: Iterable[Chunk] | None = None, *, unavailable_raises: bool = False) -> bool:
    """Check AVC/HEVC access-unit framing against *metadata* sample boundaries."""
    plan = None
    if quick and chunks is None and isinstance(mapping, _TranslatedMapping) and mapping.plan.movie is movie:
        plan = mapping.plan
        selected = plan.quick_chunks
    else:
        selected = list(movie.chunks if chunks is None else chunks)
        selected = [c for c in selected if movie.tracks[c.track].handler == b'vide']
    if quick and len(selected) > 12:
        indexes = sorted({0, 1, 2, len(selected)//2, len(selected)-3, len(selected)-2, len(selected)-1})
        selected = [selected[i] for i in indexes]
    checked = 0
    with (phase('Validate sample boundaries', total=sum(len(c.sizes) for c in selected), unit='samples')
          if not quick and chunks is None else nullcontext()) as task:
        for chunk in selected:
            t = movie.tracks[chunk.track]
            sid, base = mapping[(chunk.track, chunk.index)]
            if quick:
                relative_ranges = (plan.quick_samples[(chunk.track, chunk.index)]
                                   if plan is not None else _quick_sample_ranges(chunk))
                sample_ranges = ((base + offset, size) for offset, size in relative_ranges)
            else:
                sample_ranges = zip(accumulate(chunk.sizes, initial=base), chunk.sizes)
            chunk_end = base + chunk.size if not quick else 0
            window = memoryview(b'')
            window_start = base
            for off, n in sample_ranges:
                try:
                    if quick:
                        raw = pool.read(sid, off, n)
                    else:
                        if off + n > window_start + len(window):
                            window = memoryview(pool.read(sid, off, min(
                                max(n, 4*1024*1024), chunk_end-off)))
                            window_start = off
                        raw = window[off-window_start:off-window_start+n]
                except FormatError:
                    if unavailable_raises:
                        raise
                    if task is not None:
                        task.outcome('failed', detail='sample bytes unavailable')
                    return False
                checked += 1
                if task is not None and checked % 4096 == 0:
                    task.update(checked)
                if not valid_sample(raw, t.codec, t.nal_length):
                    if task is not None:
                        task.update(checked)
                        task.outcome('failed', detail='sample boundaries do not match')
                    return False
            # Cached bytes still require the source-identity gate after validation.
            pool.check(sid)
        if task is not None:
            task.update(checked)
    return True


def _link_unavailable(error: OSError) -> bool:
    return error.errno in {errno.EPERM, errno.EXDEV, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}


def _publish_link_or_copy(source: Path, destination: Path) -> str:
    try:
        os.link(source, destination)
        return 'case_internal_hardlink'
    except OSError as e:
        if not _link_unavailable(e):
            raise
    # exFAT is common on recovery disks and does not support hard links. This is
    # an explicit, logged storage fallback, not a change to matching semantics.
    LOG.warning('Hard links unavailable; copying case artifact (extra disk space): %s', destination)
    if shutil.disk_usage(destination.parent).free < source.stat().st_size + 64*1024*1024:
        raise SafetyError('Insufficient space for publication copy on a filesystem without hard links')
    try:
        with source.open('rb') as r, destination.open('xb') as w:
            with phase('Publish candidate copy', total=source.stat().st_size, unit='bytes') as task:
                while data := r.read(4*1024*1024):
                    w.write(data)
                    task.advance(len(data))
                task.heartbeat(detail='flushing publication')
                w.flush()
                os.fsync(w.fileno())
    except BaseException:
        if destination.exists():
            destination.unlink()
        raise
    return 'independent_copy'


def reconstruct(movie: Movie, mapping: Mapping, pool: ReaderPool, dest: Path,
                max_output_bytes: int) -> dict:
    """Copy original encoded samples and relocate chunk offsets; no re-encoding."""
    validate_ranges(movie, mapping, pool)
    zero = {(c.track, c.index): 0 for c in movie.chunks}
    preliminary = rewrite_offsets(movie, zero)
    start = len(FTYP) + len(preliminary) + 16
    size = start + movie.media_size
    if size > max_output_bytes:
        raise SafetyError(f'Candidate exceeds --max-output-bytes ({size} bytes needed)')
    free = shutil.disk_usage(dest.parent).free
    if free < size + 64*1024*1024:
        raise SafetyError(f'Insufficient space: need {size + 64*1024*1024} free bytes')
    new_offsets = {}
    cursor = start
    for c in movie.chunks:
        new_offsets[(c.track, c.index)] = cursor
        cursor += c.size
    moov = rewrite_offsets(movie, new_offsets)
    if len(moov) != len(preliminary):
        raise RuntimeError('offset rewrite changed metadata length unexpectedly')
    h = hashlib.sha256()
    media_hash = hashlib.sha256()
    entries = []
    ordered = []
    with phase('Reconstruct encoded media', total=movie.media_size, unit='bytes') as task, dest.open('xb') as output:
        def write(data: bytes) -> None:
            output.write(data)
            h.update(data)
        for label, raw in (('ftyp', FTYP), ('moov', moov), ('mdat_header', struct.pack('>I4sQ', 1, b'mdat', movie.media_size + 16))):
            ordered.append({'ordinal': len(ordered), 'kind': 'container', 'container': label,
                            'output_offset': output.tell(), 'length': len(raw),
                            'sha256': hashlib.sha256(raw).hexdigest(), 'generated': True,
                            'source_id': None, 'physical_extents': []})
            write(raw)
        for c in movie.chunks:
            sid, off = mapping[(c.track, c.index)]
            remaining = c.size
            current = off
            chunk_hash = hashlib.sha256()
            while remaining:
                raw = pool.read(sid, current, min(remaining, 4*1024*1024))
                write(raw)
                media_hash.update(raw)
                chunk_hash.update(raw)
                current += len(raw)
                remaining -= len(raw)
                task.advance(len(raw))
            source = pool.sources[sid]
            entries.append({'track': c.track, 'chunk': c.index, 'samples': len(c.sizes),
                            'source_id': sid, 'source_path': source['path'], 'source_offset': off,
                            'source_sha256_at_scan': source['sha256'], 'size': c.size,
                            'output_offset': new_offsets[(c.track, c.index)],
                            'sha256': chunk_hash.hexdigest(),
                            'physical_extents':pool.provenance(sid,off,c.size)})
            for extent in entries[-1]['physical_extents']:
                row = pool.physical[extent['source_id']]
                extent['source_identity'] = {key: row.get(key) for key in ('dev', 'ino', 'size', 'mtime_ns', 'sha256')}
            ordered.append(dict(entries[-1], ordinal=len(ordered), kind='media',
                                logical_offset=off, length=c.size, generated=False))
        task.heartbeat(detail='flushing candidate')
        output.flush()
        os.fsync(output.fileno())
    return {'sha256': h.hexdigest(), 'media_sha256': media_hash.hexdigest(), 'bytes': size,
            'chunks': entries, 'ordered_fragments': ordered, 'metadata_sha256': movie.digest,
            'note': 'Encoded samples are copied; container offsets are rebuilt. Identity and wall-clock chronology are not proven.'}


def _record_attempt(case, movie, md, stage, result):
    case.db.execute('''INSERT INTO attempts(movie_digest,mapping_digest,stage,status,detail)
                    VALUES(?,?,?,?,?) ON CONFLICT(movie_digest,mapping_digest) DO UPDATE SET
                    stage=excluded.stage,status=excluded.status,detail=excluded.detail''',
                    (movie.digest, md, stage, result['status'], json_text(result)))
    case.db.commit()


def _tier_eligible(movie, mapping, pool):
    if not getattr(pool, 'tiers', None) or not pool.tiers.has_recovered:
        return True
    if isinstance(mapping, _TranslatedMapping):
        # A whole-payload reference crossing accepted bytes belongs to tier two.
        return pool.candidate_permits(mapping.sid, mapping.translation + mapping.plan.minimum,
                                      mapping.plan.maximum - mapping.plan.minimum)
    return all(pool.candidate_permits(sid, offset, length)
               for sid, offset, length in source_ranges(movie, mapping))


def _search_scope(case):
    value = getattr(case, '_search_tiers', None)
    if value is not None and value['searched'] == [1]:
        value['second_tier_unsearched'] = case._candidate_tiers.has_recovered
    return value


def _scoped_result(case, key, status, detail):
    previous = case.job('ordinary', key)
    history = list(detail.get('historical_publications', []))
    if previous:
        old = previous['detail']
        history += old.get('historical_publications', [])
        if len(old.get('candidates', [])) == 1:
            candidate = old['candidates'][0]
            history += [{'path': path, 'sha256': candidate['sha256'], 'candidate': candidate['path']}
                        for path in old.get('outputs', [])]
    retained = []
    for entry in history:
        if entry['path'] not in detail.get('outputs', []) and entry not in retained:
            retained.append(entry)
    if retained:
        detail['historical_publications'] = retained
        detail['historical_outputs'] = [entry['path'] for entry in retained]
    if _search_scope(case) is not None:
        detail['search_tiers'] = dict(_search_scope(case))
    case.result(key, status, detail)


def _refresh_tiers(case):
    tiers = getattr(case, '_candidate_tiers', None)
    if tiers is not None:
        tiers.refresh()


def attempt(case: Case, movie: Movie, mapping: Mapping, pool: ReaderPool, tools: dict,
            opt: Options, stage: str) -> dict:
    if not _tier_eligible(movie, mapping, pool):
        return {'status': 'rejected', 'stage': stage, 'reason': 'recovered_reference_second_tier'}
    md = mapping_digest(movie, mapping)
    try:
        if isinstance(mapping, _TranslatedMapping) and mapping.plan.movie is movie:
            mapping.check_source(pool)
            ranges = ()
        else:
            ranges = _mapping_ranges(movie, mapping, pool)
        # A quick negative proves only that this assembly cannot work. Check
        # every referenced source, including those outside the sparse screen,
        # without resolving every chunk's provenance before that rejection.
        for sid in {sid for sid, _, _ in ranges}:
            pool.check(sid)
    except FormatError as error:
        return {'status': 'rejected', 'mapping_digest': md, 'stage': stage, 'reason': str(error)}
    prev = case.db.execute('SELECT * FROM attempts WHERE movie_digest=? AND mapping_digest=?',
                           (movie.digest, md)).fetchone()
    if prev:
        old = json.loads(prev['detail'])
        same_tools = (not old.get('historical_validation') and
                      old.get('validation_revision') == VALIDATION_REVISION and
                      old.get('reconstruct_only', False) == opt.reconstruct_only and old.get('tools') == tools and
                      old.get('timeout') == opt.timeout)
        if same_tools and old.get('status') in {'valid', 'rejected'}:
            if old.get('status') == 'rejected' and old.get('negative_screen'):
                return old
            try:
                if old.get('status') == 'rejected' and not check_samples(
                        movie, mapping, pool, quick=True, unavailable_raises=True):
                    promoted = dict(old, reason='NAL_sample_boundaries_do_not_match', negative_screen=True,
                                    range_validation='not_evaluated', content_evaluated=True)
                    _record_attempt(case, movie, md, stage, promoted)
                    return promoted
                validate_ranges(movie, mapping, pool)
            except FormatError as error:
                return {'status': 'rejected', 'mapping_digest': md, 'stage': stage, 'reason': str(error)}
        if same_tools and old.get('status') == 'valid':
            p = case.root / old['path']
            if not p.is_file() or p.is_symlink() or file_sha256(p) != old['sha256']:
                raise SafetyError(f'Previously validated candidate changed or disappeared: {p}')
            return old
        if same_tools and old.get('status') == 'rejected':
            return dict(old, content_evaluated=True)
    result = {'status': 'rejected', 'mapping_digest': md, 'stage': stage,
              'tools': tools, 'timeout': opt.timeout, 'validation_revision': VALIDATION_REVISION,
              'reconstruct_only': opt.reconstruct_only}
    temp = case.root / 'work' / f'{movie.digest}-{md}.mp4'
    target = case.root / 'candidates' / f'{movie.digest[:24]}-{md[:24]}.mp4'
    if temp.exists() or temp.is_symlink():
        if temp.is_symlink():
            raise SafetyError('Work artifact is a symlink')
        # This exact deterministic file is our interrupted temporary output.
        temp.unlink()
    cacheable = True
    try:
        quick_ok = check_samples(movie, mapping, pool, quick=True, unavailable_raises=True)
        if not quick_ok:
            result.update(reason='NAL_sample_boundaries_do_not_match', negative_screen=True,
                          range_validation='not_evaluated', content_evaluated=True)
        else:
            validate_ranges(movie, mapping, pool)
        if quick_ok and not check_samples(movie, mapping, pool):
            result.update(reason='some_NAL_samples_do_not_match', content_evaluated=True)
        elif quick_ok:
            proof = reconstruct(movie, mapping, pool, temp, opt.max_output_bytes)
            checked = ({'ok': True, 'reason': 'structural_reconstruction_only',
                        'decode_checked': False, 'scope': 'structural_reconstruction_only'}
                       if opt.reconstruct_only else validate(temp, movie, tools, opt.timeout))
            result['validation'] = checked
            result['content_evaluated'] = True
            result['reason'] = checked['reason']
            if checked['ok']:
                if target.exists():
                    if target.is_symlink() or file_sha256(target) != proof['sha256']:
                        raise SafetyError(f'Existing candidate does not match: {target}')
                    temp.unlink()
                else:
                    duplicate = None
                    for row in case.db.execute("SELECT detail FROM attempts WHERE movie_digest=? AND status='valid'", (movie.digest,)):
                        prior = json.loads(row['detail'])
                        if prior.get('sha256') == proof['sha256']:
                            p = case.root / prior['path']
                            if p.is_file() and not p.is_symlink() and file_sha256(p) == proof['sha256']:
                                duplicate = p
                                break
                    if duplicate:
                        try:
                            os.link(duplicate, target)
                            temp.unlink()
                        except OSError as e:
                            if not _link_unavailable(e):
                                raise
                            LOG.warning('Hard links unavailable; keeping an independent duplicate candidate: %s', target)
                            os.replace(temp, target)
                    else:
                        os.replace(temp, target)
                result.update(status='valid', path=str(target.relative_to(case.root)),
                              sha256=proof['sha256'], media_sha256=proof['media_sha256'])
                write_json(target.with_suffix('.json'), {'version': __version__, 'stage': stage,
                            'movie': movie.summary(), 'provenance': proof, 'ordered_fragments': proof['ordered_fragments'], 'search_tiers': _search_scope(case), 'validation': checked})
            elif checked.get('probe', {}).get('timeout') or checked.get('decode', {}).get('timeout'):
                result['status'] = 'limited'
    except FormatError as e:
        # Availability/policy failures can change when the scope expands; they
        # are not evidence that these encoded bytes fail the sample model.
        result['reason'] = str(e)
        cacheable = False
    finally:
        if temp.exists():
            temp.unlink()
    if not cacheable:
        return result
    _record_attempt(case, movie, md, stage, result)
    return result


def _ordered_manifest(case, path, evidence):
    """Backfill byte order from canonical chunk evidence; never decode or recopy media."""
    if evidence.get('ordered_fragments'):
        return evidence
    proof = evidence.get('provenance', {})
    chunks = proof.get('chunks', [])
    if not chunks or any(not all(key in chunk for key in ('output_offset', 'size', 'source_offset', 'sha256')) for chunk in chunks):
        return dict(evidence, historical_manifest_unavailable='missing_ordered_chunk_provenance')
    size = path.stat().st_size
    ordered = []
    physical = case.physical_sources()
    def span_hash(stream, offset, length):
        stream.seek(offset)
        digest = hashlib.sha256()
        remaining = length
        while remaining:
            raw = stream.read(min(remaining, 1024 * 1024))
            if not raw:
                raise SafetyError('Published output truncated during manifest backfill')
            digest.update(raw)
            remaining -= len(raw)
        return digest.hexdigest()
    cursor = 0
    with path.open('rb') as source:
        for chunk in sorted(chunks, key=lambda item: item['output_offset']):
            offset, length = chunk['output_offset'], chunk['size']
            if offset < cursor or length <= 0 or offset + length > size:
                raise SafetyError('Invalid historical output chunk evidence')
            if offset > cursor:
                ordered.append({'ordinal': len(ordered), 'kind': 'container', 'generated': True,
                                'output_offset': cursor, 'length': offset-cursor,
                                'sha256': span_hash(source, cursor, offset-cursor),
                                'source_id': None, 'physical_extents': []})
            entry = dict(chunk, ordinal=len(ordered), kind='media', generated=False,
                         logical_offset=chunk['source_offset'], length=length)
            if span_hash(source, offset, length) != entry['sha256']:
                raise SafetyError('Historical chunk hash does not match publication')
            for extent in entry.get('physical_extents', []):
                row = physical[extent['source_id']]
                ReaderPool._check_identity(row)
                extent['source_identity'] = {key: row.get(key) for key in ('dev','ino','size','mtime_ns','sha256')}
            ordered.append(entry)
            cursor = offset + length
        if cursor < size:
            ordered.append({'ordinal': len(ordered), 'kind': 'container', 'generated': True,
                            'output_offset': cursor, 'length': size-cursor,
                            'sha256': span_hash(source, cursor, size-cursor),
                            'source_id': None, 'physical_extents': []})
    return dict(evidence, ordered_fragments=ordered)


def _backfill_candidate_manifest(case, candidate):
    path = case.root / candidate['path']
    sidecar = path.with_suffix('.json')
    if sidecar.is_symlink():
        raise SafetyError('Candidate manifest is a symlink')
    evidence = json.loads(sidecar.read_text()) if sidecar.is_file() else {}
    updated = _ordered_manifest(case, path, evidence)
    if updated != evidence:
        write_json(sidecar, updated)
    return updated


def _backfill_ordinary_publications(case):
    for job in case.get_jobs('ordinary'):
        detail = job['detail']
        publications = list(detail.get('historical_publications', []))
        candidates = detail.get('candidates', [])
        if len(candidates) == 1:
            publications += [{'path': output, 'sha256': candidates[0]['sha256'],
                              'candidate': candidates[0]['path']} for output in detail.get('outputs', [])]
        for entry in publications:
            output = case.root / entry['path']
            candidate = {'path': entry['candidate'], 'sha256': entry['sha256']}
            for path in (output, case.root / candidate['path']):
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(case.root) or file_sha256(path) != entry['sha256']:
                    raise SafetyError('Published output changed or disappeared')
            evidence = _backfill_candidate_manifest(case, candidate)
            sidecar = output.with_suffix('.json')
            if sidecar.is_symlink():
                raise SafetyError('Published manifest is a symlink')
            published = json.loads(sidecar.read_text()) if sidecar.is_file() else {}
            updated = dict(published)
            for field in ('ordered_fragments', 'historical_manifest_unavailable'):
                if field in evidence:
                    updated[field] = evidence[field]
            if updated != published:
                write_json(sidecar, updated)


def _publish(case: Case, movie: Movie, successes: list[dict], status: str) -> list[str]:
    """Published output is a case-internal hard link, never a source-file link."""
    final = case.root / 'reconstructed' / f'{movie.digest[:24]}.mp4'
    sidecar = final.with_suffix('.json')
    if final.is_symlink() or sidecar.is_symlink():
        raise SafetyError('Published artifact is a symlink')
    if status not in {'matched', 'reconstructed'} or len(successes) != 1:
        return []
    candidate = case.root / successes[0]['path']
    evidence = _backfill_candidate_manifest(case, successes[0])
    if evidence.get('search_tiers') != _search_scope(case):
        evidence['search_tiers'] = _search_scope(case)
        write_json(candidate.with_suffix('.json'), evidence)
    if final.exists():
        if file_sha256(final) != successes[0]['sha256']:
            # Preserve the prior publication; the accepted candidate already has
            # its own canonical bytes and sidecar and can be the current output.
            return [successes[0]['path']]
        storage_mode = 'retained_existing_publication'
    else:
        storage_mode = _publish_link_or_copy(candidate, final)
    evidence = _backfill_candidate_manifest(case, successes[0])
    write_json(sidecar, {'status': status, 'candidate': successes[0]['path'], 'storage_mode': storage_mode,
                         'sha256': successes[0]['sha256'], 'provenance': evidence.get('provenance', {}),
                         'ordered_fragments': evidence.get('ordered_fragments', evidence.get('provenance', {}).get('ordered_fragments', [])),
                         'historical_manifest_unavailable': evidence.get('historical_manifest_unavailable'),
                         'decode_checked': not successes[0].get('reconstruct_only', False),
                         'validation_scope': 'structural_reconstruction_only' if successes[0].get('reconstruct_only') else 'strict_decode',
                         'search_tiers': _search_scope(case),
                         'meaning': 'One byte-distinct candidate passed within searched tiers; unsearched tiers may contain alternatives, and historical identity is not proven.'})
    return [str(final.relative_to(case.root))]


def finish(case: Case, movie: Movie, candidates: list[dict], limited: bool,
           stage: str, run_key: str, extra: dict | None = None) -> str:
    # Identical encoded media is not an ambiguous second reconstruction. Keep
    # individual attempt records so alternative source locations are not erased.
    unique = {}
    for c in candidates:
        if c.get('status') == 'valid':
            unique.setdefault(c['media_sha256'], c)
    good = list(unique.values())
    if len(good) > 1:
        status = 'ambiguous'
    elif limited:
        status = 'search_limited'
    elif len(good) == 1:
        status = 'reconstructed' if good[0].get('reconstruct_only') else 'matched'
    else:
        status = 'unresolved'
    previous = case.job('ordinary', movie.digest)
    history = list(previous['detail'].get('historical_publications', [])) if previous else []
    if previous and previous['detail'].get('outputs') and len(previous['detail'].get('candidates', [])) == 1:
        candidate = previous['detail']['candidates'][0]
        for path in previous['detail']['outputs']:
            entry = {'path': path, 'sha256': candidate['sha256'], 'candidate': candidate['path']}
            if entry not in history:
                history.append(entry)
    outputs = _publish(case, movie, good, status)
    detail = {'run_key': run_key, 'stage': stage, 'candidates': good, 'search_limited': limited,
              'outputs': outputs,
              'decode_checked': bool(good) and all(not c.get('reconstruct_only', False) for c in good),
              'scope': 'Uniqueness applies only to the documented, completed hypotheses searched.'}
    retained = [entry for entry in history if entry['path'] not in outputs]
    if retained:
        detail['historical_publications'] = retained
        detail['historical_outputs'] = [entry['path'] for entry in retained]
    if extra:
        detail.update(extra)
    if _search_scope(case) is not None:
        detail['search_tiers'] = dict(_search_scope(case))
    _scoped_result(case, movie.digest, status, detail)
    _refresh_tiers(case)
    LOG.info('%s %s: %d byte-distinct candidates (%s)', movie.digest[:12], status, len(good),
             'structural reconstruction; no decode check' if any(c.get('reconstruct_only') for c in good)
             else 'decode checked' if good else 'no accepted candidate; no decode claim')
    return status


@contextmanager
def _search_store():
    # Search cardinality must not become a RAM ceiling or a hypothesis cutoff.
    with tempfile.TemporaryDirectory(prefix='videorebuild-search-') as directory:
        db = sqlite3.connect(Path(directory) / 'search.sqlite3')
        try:
            yield db
        finally:
            db.close()


def whole_payload(case: Case, movie: Movie, pool: ReaderPool, tools: dict,
                  opt: Options, run_key: str) -> tuple[str, list[dict], bool]:
    successes = {}
    limited = False
    tested = 0
    plan = _TranslationPlan(movie)
    metadata_pool = pool.tiers.pool if getattr(pool, 'tiers', None) else pool
    with _search_store() as search:
        search.execute('CREATE TABLE translations (sid INTEGER,base INTEGER,PRIMARY KEY(sid,base)) WITHOUT ROWID')
        search.execute('CREATE TABLE origins (sid INTEGER,base INTEGER,PRIMARY KEY(sid,base)) WITHOUT ROWID')
        locations = case.db.execute('''SELECT a.* FROM atoms a JOIN sources s ON s.id=a.source_id
                                     WHERE a.movie_digest=? AND s.complete=1''', (movie.digest,))
        for location in locations:
            sid, moff = location['source_id'], location['offset']
            if sid not in pool.sources or not metadata_pool.permits(sid, moff, location['size']):
                continue
            if pool.sources[sid]['kind'] in ('file', 'logical'):
                search.execute('INSERT OR IGNORE INTO origins VALUES(?,0)', (sid,))
            for row in case.db.execute("SELECT offset,size FROM atoms WHERE source_id=? AND kind='ftyp' AND offset<=? ORDER BY offset DESC", (sid, moff)):
                if metadata_pool.permits(sid, row['offset'], row['size']):
                    search.execute('INSERT OR IGNORE INTO origins VALUES(?,?)', (sid, row['offset']))
                    break
        with phase('Original offset locations examined', unit='candidates') as task:
            for sid, origin in search.execute('SELECT sid,base FROM origins ORDER BY sid,base'):
                if not _tier_eligible(movie, plan.mapping(sid, origin), pool):
                    continue
                task.advance()
                search.execute('INSERT INTO translations VALUES(?,?)', (sid, origin))
                tested += 1
                mapping = plan.mapping(sid, origin)
                result = attempt(case, movie, mapping, pool, tools, opt, 'original_offsets')
                if result['status'] == 'valid':
                    successes.setdefault(result['media_sha256'], result)
                limited |= result['status'] == 'limited'
        query = '''SELECT a.source_id,a.offset,a.header FROM atoms a JOIN sources s ON s.id=a.source_id
                   WHERE a.kind='mdat' AND s.complete=1 AND a.payload_size>=?'''
        parameters = [movie.span]
        if opt.max_padding is not None:
            query += ' AND a.payload_size<=?'
            parameters.append(movie.span+opt.max_padding)
        query += ' ORDER BY a.payload_size,a.source_id,a.offset'
        if opt.max_padding is not None:
            for row in case.db.execute('''SELECT a.* FROM atoms a JOIN sources s ON s.id=a.source_id
                    WHERE a.kind='mdat' AND s.complete=1 AND a.payload_size>?''', (movie.span+opt.max_padding,)):
                if row['source_id'] in pool.sources and pool.permits(row['source_id'], row['offset'], row['header']):
                    limited = True
                    break
        # Finish the case read before attempts commit. A live case cursor pins
        # the WAL snapshot and prevents checkpoint recycling across the search.
        search.execute('CREATE TABLE candidates (position INTEGER PRIMARY KEY,sid INTEGER,offset INTEGER,header INTEGER)')
        search.executemany('INSERT INTO candidates(sid,offset,header) VALUES(?,?,?)',
                           case.db.execute(query, parameters))
        examined = 0
        with phase('Whole mdat locations examined', unit='candidates') as task:
            for sid, offset, header in search.execute('SELECT sid,offset,header FROM candidates ORDER BY position'):
                if sid not in pool.sources or not pool.permits(sid, offset, header):
                    continue
                translation = offset+header-movie.chunks[0].offset
                if not _tier_eligible(movie, plan.mapping(sid, translation), pool):
                    continue
                if opt.max_candidates is not None and examined >= opt.max_candidates:
                    limited = True
                    break
                examined += 1
                task.update(examined)
                translation = offset+header-movie.chunks[0].offset
                inserted = search.execute('INSERT OR IGNORE INTO translations VALUES(?,?)', (sid, translation)).rowcount
                if not inserted:
                    continue
                tested += 1
                mapping = plan.mapping(sid, translation)
                result = attempt(case, movie, mapping, pool, tools, opt, 'whole_mdat')
                if result['status'] == 'valid':
                    successes.setdefault(result['media_sha256'], result)
                limited |= result['status'] == 'limited'
            if limited:
                task.outcome('limited', detail='candidate search or validation limit reached')
    status = finish(case, movie, list(successes.values()), limited, 'whole_payload', run_key,
                    {'tested_hypotheses': tested, 'max_trailing_padding': opt.max_padding})
    return status, list(successes.values()), limited


@dataclass
class Group:
    """A video chunk and adjacent audio chunks, kept contiguous within an extent."""
    movie: Movie
    index: int
    chunks: tuple[Chunk, ...]
    start: int
    span: int

    @property
    def key(self) -> tuple[str, int]:
        return self.movie.digest, self.index

    def mapping(self, sid: int, base: int) -> Mapping:
        return {(c.track, c.index): (sid, base+c.offset-self.start) for c in self.chunks}

    def anchors(self) -> list[tuple[int, int]]:
        """(sample_size-4, sample-relative offset) search keys, never evidence alone."""
        result = []
        for c in self.chunks:
            track = self.movie.tracks[c.track]
            if track.handler != b'vide' or track.nal_length != 4:
                continue
            chosen = set(range(min(8, len(c.sizes)))) | set(range(max(0, len(c.sizes)-4), len(c.sizes)))
            pos = c.offset-self.start
            for i, size in enumerate(c.sizes):
                if i in chosen and 8 <= size < 16*1024*1024:
                    result.append((size-4, pos))
                pos += size
        return sorted(set(result))


def groups_for(movie: Movie) -> list[Group]:
    groups = []
    current = []
    has_video = False
    for c in movie.chunks:
        is_video = movie.tracks[c.track].handler == b'vide'
        if is_video and has_video:
            groups.append(tuple(current))
            current = []
            has_video = False
        current.append(c)
        has_video |= is_video
    if current:
        groups.append(tuple(current))
    return [Group(movie, i, cs, cs[0].offset, cs[-1].offset+cs[-1].size-cs[0].offset)
            for i, cs in enumerate(groups)]


def _group_hash(group: Group, mapping: Mapping, pool: ReaderPool) -> str:
    h = hashlib.sha256()
    for c in group.chunks:
        sid, off = mapping[(c.track, c.index)]
        remaining = c.size
        while remaining:
            raw = pool.read(sid, off, min(remaining, 4*1024*1024))
            h.update(raw)
            off += len(raw)
            remaining -= len(raw)
    return h.hexdigest()


def _anchor_locations(pool, maximum_sample, wanted, opt, state, task):
    """Canonical bounded NAL-chain discovery, independent of target batches."""
    high = ((maximum_sample - 4) >> 16) & 255
    # Zero-width matching preserves overlapping length prefixes at block edges.
    pattern = re.compile(fr'(?=\x00[\x00-\x{high:02x}][\s\S]{{2}})(?!\x00{{4}})'.encode('ascii'))

    def totals(sid, block_base, data, start):
        pos = start
        total = 0
        source_size = pool.sources[sid]['size']
        for _ in range(65536):
            if opt.max_anchor_checks is not None and state['nal_checks'] >= opt.max_anchor_checks:
                state.update(limited=True, stop_reason='nal_check_budget')
                return
            state['nal_checks'] += 1
            if state['nal_checks'] % 4096 == 0:
                task.heartbeat(nal_checks=state['nal_checks'], anchor_checks=state['anchor_checks'])
            if pos + 5 > source_size:
                return
            local = pos - block_base
            head_size = min(6, source_size - pos)
            try:
                head = (data[local:local+head_size]
                        if 0 <= local and local+head_size <= len(data)
                        else pool.read(sid, pos, head_size))
            except FormatError:
                return
            n = int.from_bytes(head[:4], 'big')
            if n < 1 or total+4+n > maximum_sample or pos+4+n > source_size:
                return
            first = head[4]
            avc = not (first & 128) and 1 <= (first & 31) <= 23
            hevc = (n >= 2 and len(head) >= 6 and not (first & 128) and
                    ((first >> 1) & 63) <= 40 and (head[5] & 7))
            if not (avc or hevc):
                return
            total += n + 4
            if wanted(total - 4):
                yield total - 4
            pos += n + 4

    scanned = 0
    for sid in pool.sources:
        pool.check(sid)
        try:
            for base, data, owned in pool.iter_blocks(sid, overlap=16):
                at = 0
                for match in pattern.finditer(data):
                    at = match.start()
                    if at >= owned or at+4 > len(data):
                        continue
                    for length in totals(sid, base, data, base+at):
                        yield sid, base+at, length
                        if state['limited']:
                            break
                    if state['limited']:
                        break
                scanned += min(owned, at+1) if state['limited'] else owned
                task.update(scanned, anchor_checks=state['anchor_checks'], nal_checks=state['nal_checks'],
                            located_extents=state.get('located_extents', 0), discovered_locations=state.get('locations', 0))
                if state['limited']:
                    return
        finally:
            pool.check(sid)


class _DeferredMovies:
    """Repeatable metadata stream backed by selected digests, not retained BLOBs."""
    def __init__(self, case, store):
        self.case, self.store = case, store
        store.execute('CREATE TABLE deferred(digest TEXT PRIMARY KEY) WITHOUT ROWID')

    def add(self, digest):
        self.store.execute('INSERT OR IGNORE INTO deferred VALUES(?)', (digest,))

    def __iter__(self):
        for (digest,) in self.store.execute('SELECT digest FROM deferred ORDER BY digest'):
            row = self.case.db.execute('SELECT data FROM movies WHERE digest=?', (digest,)).fetchone()
            yield parse_movie(row[0])

    def __bool__(self):
        return self.store.execute('SELECT 1 FROM deferred LIMIT 1').fetchone() is not None


def _movie_batches(movies, max_groups):
    batch = []
    count = size = 0
    for movie in movies:
        n = len(groups_for(movie))
        movie_size = len(getattr(movie, 'data', b''))
        if batch and (count + n > max_groups or size + movie_size > MAX_DEEP_METADATA):
            yield batch
            batch, count, size = [], 0, 0
        batch.append(movie)
        count += n
        size += movie_size
    if batch:
        yield batch


def deep_search(case: Case, movies: list[Movie] | _DeferredMovies, pool: ReaderPool, tools: dict,
                opt: Options, run_key: str, *, incomplete: set[str] | None = None) -> None:
    batches = _movie_batches(movies, opt.max_groups)
    first = next(batches, None)
    if first is None:
        return
    second = next(batches, None)
    capped = any(value is not None for value in (opt.max_anchor_checks, opt.max_hits, opt.max_assemblies))
    if second is None or capped:
        for batch in (first, second):
            if batch:
                _deep_batch(case, batch, pool, tools, opt, run_key, incomplete or set())
        for batch in batches:
            _deep_batch(case, batch, pool, tools, opt, run_key, incomplete or set())
        return
    del first, second, batches
    # Discovery is scoped to this reader and invocation. SQLite keeps all
    # wanted sizes and physical locations on disk; no persistent stale cache.
    with _search_store() as discovery:
        discovery.execute('CREATE TABLE lengths(length INTEGER PRIMARY KEY) WITHOUT ROWID')
        discovery.execute('CREATE TABLE locations(length INTEGER,sid INTEGER,start INTEGER,PRIMARY KEY(length,sid,start)) WITHOUT ROWID')
        for movie in movies:
            groups = groups_for(movie)
            if any(not group.anchors() for group in groups):
                continue
            for group in groups:
                discovery.executemany('INSERT OR IGNORE INTO lengths VALUES(?)', ((length,) for length, _ in group.anchors()))
        maximum = discovery.execute('SELECT max(length) FROM lengths').fetchone()[0]
        state = dict(nal_checks=0, anchor_checks=0, limited=False, stop_reason=None, locations=0)
        if maximum is not None:
            # Anchor sizes are below 16 MiB by contract: exact membership costs
            # at most 2 MiB instead of a SQLite query for every NAL in a chain.
            length_bits = bytearray((maximum >> 3) + 1)
            for (length,) in discovery.execute('SELECT length FROM lengths'):
                length_bits[length >> 3] |= 1 << (length & 7)
            available = sum(length for sid in pool.sources for _, length in pool.available_ranges(sid))
            with phase('Deep search available bytes', total=available, unit='bytes') as task:
                wanted = lambda length: bool(length_bits[length >> 3] & (1 << (length & 7)))
                for sid, start, length in _anchor_locations(pool, maximum+4, wanted, opt, state, task):
                    state['locations'] += discovery.execute('INSERT OR IGNORE INTO locations VALUES(?,?,?)', (length, sid, start)).rowcount
                    if state['locations'] % 4096 == 0:
                        task.heartbeat(discovered_locations=state['locations'], store_pages=discovery.execute('PRAGMA page_count').fetchone()[0])
        for batch in _movie_batches(movies, opt.max_groups):
            # Zero-hit sources also underpin negative discovery conclusions.
            for sid in pool.sources:
                pool.check(sid)
            _deep_batch(case, batch, pool, tools, opt, run_key, incomplete or set(), discovery=discovery,
                        discovery_nal_checks=state['nal_checks'])
            for sid in pool.sources:
                pool.check(sid)


def _deep_batch(case: Case, movies: list[Movie], pool: ReaderPool, tools: dict,
                opt: Options, run_key: str, incomplete: set[str], *, discovery=None, discovery_nal_checks=0) -> None:
    groups = {}
    per_movie = {}
    excluded = set()
    anchors = defaultdict(list)
    for movie in movies:
        gs = groups_for(movie)
        if any(not g.anchors() for g in gs):
            _scoped_result(case, movie.digest, 'search_limited' if movie.digest in incomplete else 'unsupported', {'run_key': run_key,
                        'reason': 'deep search requires eligible 4-byte NAL-length samples in every extent', 'search_tiers': _search_scope(case)})
            excluded.add(movie.digest)
            continue
        per_movie[movie.digest] = gs
        for g in gs:
            groups[g.key] = g
            for length, relative in g.anchors():
                anchors[length].append((g.key, relative))
    if not groups:
        return
    with _search_store() as search:
        search.execute('CREATE TABLE visited (movie TEXT,g INTEGER,sid INTEGER,start INTEGER,PRIMARY KEY(movie,g,sid,start)) WITHOUT ROWID')
        search.execute('CREATE TABLE hits (movie TEXT,g INTEGER,fingerprint TEXT,sid INTEGER,start INTEGER,PRIMARY KEY(movie,g,sid,start)) WITHOUT ROWID')
        search.execute('CREATE INDEX hits_order ON hits(movie,g,fingerprint,sid,start)')
        counts_by_group = {key: 0 for key in groups}
        capped = set()
        state = dict(anchor_checks=0, nal_checks=discovery_nal_checks, limited=False, stop_reason=None, located_extents=0)
        LOG.info('Deep search: %d extents, %d anchor sizes across %d sources', len(groups), len(anchors), len(pool.sources))
        available = (sum(length for sid in pool.sources for _, length in pool.available_ranges(sid))
                     if discovery is None else None)
        label = 'Deep search available bytes' if discovery is None else 'Deep anchor associations'
        with phase(label, total=available, unit='bytes' if discovery is None else 'locations') as scan_task:
            if discovery is None:
                locations = _anchor_locations(pool, max(anchors)+4, anchors.__contains__, opt, state, scan_task)
            else:
                def replay():
                    for length in sorted(anchors):
                        for sid, start in discovery.execute('SELECT sid,start FROM locations WHERE length=? ORDER BY sid,start', (length,)):
                            pool.check(sid)
                            yield sid, start, length
                locations = replay()
            visited_locations = 0
            try:
                for sid, at, length in locations:
                    visited_locations += 1
                    source = pool.sources[sid]
                    for key, relative in anchors.get(length, ()):
                        if key in capped:
                            continue
                        group = groups[key]
                        start = at - relative
                        if start < 0 or start + group.span > source['size']:
                            continue
                        if any(not pool.permits(sid, start+c.offset-group.start, c.size) for c in group.chunks):
                            continue
                        if not search.execute('INSERT OR IGNORE INTO visited VALUES(?,?,?,?)', (*key, sid, start)).rowcount:
                            continue
                        state['anchor_checks'] += 1
                        if opt.max_anchor_checks is not None and state['anchor_checks'] > opt.max_anchor_checks:
                            state.update(limited=True, stop_reason='anchor_check_budget')
                            break
                        mapping = group.mapping(sid, start)
                        if not check_samples(group.movie, mapping, pool, quick=True, chunks=group.chunks):
                            continue
                        if not check_samples(group.movie, mapping, pool, chunks=group.chunks):
                            continue
                        fingerprint = _group_hash(group, mapping, pool)
                        if opt.max_hits is not None and counts_by_group[key] >= opt.max_hits:
                            capped.add(key)
                            continue
                        if not counts_by_group[key]:
                            state['located_extents'] += 1
                        counts_by_group[key] += 1
                        search.execute('INSERT INTO hits VALUES(?,?,?,?,?)', (*key, fingerprint, sid, start))
                    if discovery is not None and visited_locations % 4096 == 0:
                        scan_task.update(visited_locations, anchor_checks=state['anchor_checks'], located_extents=state['located_extents'])
                    if state['limited']:
                        break
            finally:
                locations.close()
            if discovery is not None:
                scan_task.update(visited_locations, anchor_checks=state['anchor_checks'], located_extents=state['located_extents'])
            if state['limited'] or capped:
                scan_task.outcome('limited', detail=state['stop_reason'] or 'hit capacity reached')
        for movie in movies:
            if movie.digest in excluded:
                continue
            gs = per_movie[movie.digest]
            counts = [counts_by_group[g.key] for g in gs]
            limited = state['limited'] or movie.digest in incomplete or any(g.key in capped for g in gs)
            details = {'extents': len(gs), 'located_extents': sum(n > 0 for n in counts),
                       'hit_counts': counts, 'anchor_checks': state['anchor_checks'],
                       'nal_checks': state['nal_checks'], 'stop_reason': state['stop_reason'],
                       'shared_discovery': discovery is not None,
                       'model': 'Each video chunk plus adjacent audio remains contiguous; no within-extent sector permutation.'}
            if any(n == 0 for n in counts):
                finish(case, movie, [], limited, 'scattered_extents', run_key, details)
                continue
            successes = {}
            tried = 0
            pruning = {'partial_assignments_checked': 0, 'partial_assignments_rejected': 0,
                       'content_prefixes_checked': 0, 'content_prefixes_rejected': 0,
                       'content_tuples_considered': 0, 'physical_witness_attempts': 0}
            budget_reached = False
            with phase('Deep assemblies attempted', unit='candidates') as assembly_task:
                content_choices = _content_assemblies(search, gs, pool, pruning, assembly_task)
                try:
                    for witnesses in content_choices:
                        for choices in witnesses:
                            if opt.max_assemblies is not None and tried >= opt.max_assemblies:
                                limited = budget_reached = True
                                assembly_task.outcome('limited', detail='assembly budget reached')
                                break
                            mapping = {}
                            for g, (sid, start) in zip(gs, choices):
                                mapping.update(g.mapping(sid, start))
                            result = attempt(case, movie, mapping, pool, tools, opt, 'scattered_extents')
                            pruning['physical_witness_attempts'] += 1
                            evaluated = result['status'] in {'valid', 'limited'} or result.get('content_evaluated')
                            # Rejected test/dummy attempts without proof may require
                            # another witness. Only evaluated content consumes the cap.
                            if evaluated:
                                tried += 1
                            assembly_task.update(tried, **pruning)
                            if result['status'] == 'valid':
                                successes.setdefault(result['media_sha256'], result)
                            limited |= result['status'] == 'limited'
                            if evaluated:
                                break
                        if budget_reached:
                            break
                finally:
                    content_choices.close()
                assembly_task.update(tried, **pruning)
            details.update(pruning)
            details['tested_assemblies'] = tried
            finish(case, movie, list(successes.values()), limited, 'scattered_extents', run_key, details)



def _group_physical_ranges(group, choice, pool):
    sid, start = choice
    for chunk in group.chunks:
        for part in pool.provenance(sid, start + chunk.offset - group.start, chunk.size):
            key = pool.physical_key(part['source_id'])
            yield (*key, part['source_offset'], part['source_offset'] + part['length'])


def _content_assemblies(search, groups, pool, stats, task):
    distinct = [search.execute('SELECT count(DISTINCT fingerprint) FROM hits WHERE movie=? AND g=?', group.key).fetchone()[0]
                for group in groups]
    order = sorted(range(len(groups)), key=lambda i: distinct[i])
    cursors = []
    selected = []
    # One active feasible witness, with per-group deltas: no full-prefix snapshots.
    witness_choices, witness_parts, occupied = [], [], []
    depth = 0
    while depth >= 0:
        while len(witness_choices) > depth:
            witness_choices.pop()
            for extent in witness_parts.pop():
                occupied.pop(bisect_left(occupied, extent))
        if len(cursors) <= depth:
            cursors.append(search.execute('SELECT fingerprint,count(*) FROM hits WHERE movie=? AND g=? GROUP BY fingerprint ORDER BY fingerprint', groups[order[depth]].key))
        choice = next(cursors[depth], None)
        if choice is None:
            cursors.pop().close()
            if len(selected) > depth:
                selected.pop()
            depth -= 1
            continue
        if len(selected) > depth:
            selected[depth] = choice
        else:
            selected.append(choice)
        stats['content_prefixes_checked'] += 1
        indices = order[:depth+1]
        # Usually the existing witness can be extended without revisiting any
        # older provenance. Greedy failure is never proof of impossibility.
        extension = _assemblies(search, [groups[order[depth]]], pool, counts=[choice[1]],
                                fingerprints=[choice[0]], stats=stats, task=task, initial_occupied=occupied)
        try:
            added = next(extension, None)
        finally:
            extension.close()
        if added is not None:
            witness_choices.append(added[0])
            parts = list(_group_physical_ranges(groups[order[depth]], added[0], pool))
            witness_parts.append(parts)
            for extent in parts:
                occupied.insert(bisect_left(occupied, extent), extent)
        else:
            physical = _assemblies(search, [groups[i] for i in indices], pool,
                                   counts=[item[1] for item in selected],
                                   fingerprints=[item[0] for item in selected], stats=stats, task=task)
            try:
                witness = next(physical, None)
            finally:
                physical.close()
            if witness is None:
                stats['content_prefixes_rejected'] += 1
                task.heartbeat(**stats)
                continue
            # A new location may require reassigning any earlier group. The new
            # jointly feasible witness also proves each shorter content prefix.
            witness_choices = list(witness)
            witness_parts = [list(_group_physical_ranges(groups[i], item, pool)) for i, item in zip(indices, witness)]
            occupied = sorted(extent for parts in witness_parts for extent in parts)
        if depth+1 == len(groups):
            stats['content_tuples_considered'] += 1
            def restore(choices):
                result = [None] * len(groups)
                for index, location in zip(indices, choices):
                    result[index] = location
                return tuple(result)
            first = tuple(witness_choices)
            def alternatives():
                yield restore(first)
                # Only needed if the first witness became unavailable before
                # its content could be evaluated; retain all alternative proofs.
                physical = _assemblies(search, [groups[i] for i in indices], pool,
                                       counts=[item[1] for item in selected],
                                       fingerprints=[item[0] for item in selected], stats=stats, task=task)
                try:
                    for other in physical:
                        if other != first:
                            yield restore(other)
                finally:
                    physical.close()
            yield alternatives()
        else:
            depth += 1


def _assemblies(search, groups, pool=None, *, counts=None, stats=None, task=None, fingerprints=None, initial_occupied=()):
    """Enumerate all feasible physical assignments without expanding conflicts."""
    order = sorted(range(len(groups)), key=lambda i: counts[i]) if counts is not None else list(range(len(groups)))
    cursors = []
    choices = [None] * len(groups)
    inserted = []
    occupied = list(initial_occupied)
    stats = stats if stats is not None else {'partial_assignments_checked': 0, 'partial_assignments_rejected': 0}
    depth = 0
    while depth >= 0:
        if depth == len(groups):
            yield tuple(choices)
            depth -= 1
            continue
        # Undo this depth's previous choice before advancing its cursor.
        if len(inserted) > depth:
            for extent in inserted.pop():
                occupied.pop(bisect_left(occupied, extent))
        if len(cursors) <= depth:
            index = order[depth]
            query = 'SELECT sid,start FROM hits WHERE movie=? AND g=?'
            params = groups[index].key
            if fingerprints is not None:
                query += ' AND fingerprint=?'
                params += (fingerprints[index],)
            cursors.append(search.execute(query + ' ORDER BY fingerprint,sid,start', params))
        choice = next(cursors[depth], None)
        if choice is None:
            cursors.pop().close()
            choices[order[depth]] = None
            depth -= 1
            continue
        stats['partial_assignments_checked'] += 1
        added = []
        try:
            group = groups[order[depth]]
            if pool is not None:
                for extent in _group_physical_ranges(group, choice, pool):
                    at = bisect_left(occupied, extent)
                    if ((at and occupied[at-1][:2] == extent[:2] and occupied[at-1][3] > extent[2]) or
                            (at < len(occupied) and occupied[at][:2] == extent[:2] and extent[3] > occupied[at][2])):
                        raise FormatError('physical source bytes reused or overlapped')
                    occupied.insert(at, extent)
                    added.append(extent)
        except FormatError:
            for extent in added:
                occupied.pop(bisect_left(occupied, extent))
            stats['partial_assignments_rejected'] += 1
            if task is not None and stats['partial_assignments_checked'] % 4096 == 0:
                task.heartbeat(**stats)
            continue
        if task is not None and stats['partial_assignments_checked'] % 4096 == 0:
            task.heartbeat(**stats)
        choices[order[depth]] = choice
        inserted.append(added)
        depth += 1


def recover(case: Case, opt: Options, limit: int | None = None, retry: bool = False, *, allocation_scope=None) -> None:
    opt.check()
    tools = {'mode': 'structural_reconstruction_only'} if opt.reconstruct_only else doctor()
    sources = case.sources()
    if not sources:
        raise SafetyError('No completed source scans; run scan or run first')
    run_key = hashlib.sha256(json_text({'version': __version__, 'matching_revision': 3, 'validation_revision': VALIDATION_REVISION,
                         'allocation_scope': allocation_scope.digest if allocation_scope is not None else None,
                         'options': asdict(opt), 'tools': tools,
                         'sources': [(sid,s['sha256'],s['path'],s.get('extents'),s.get('binding'))
                                     for sid,s in sorted(sources.items())]}).encode()).hexdigest()
    case.event('recovery_started', {'run_key': run_key, 'tools': tools, 'options': asdict(opt),
                 'validation_revision': VALIDATION_REVISION})
    incomplete = set()
    processed = 0
    with phase('Ordinary catalogue jobs visited', unit='jobs') as jobs_task, ReaderPool(sources,physical_sources=case.physical_sources(), allocation_scope=allocation_scope) as pool, _search_store() as pending:
        # Snapshot only selected identities on disk, then load one metadata
        # BLOB at a time with a completed point query. Streaming the case SELECT
        # through attempt writes pins its WAL for the entire recovery.
        pending.execute('CREATE TABLE selected(digest TEXT PRIMARY KEY) WITHOUT ROWID')
        pending.executemany('INSERT INTO selected VALUES(?)', case.db.execute('''SELECT m.digest FROM movies m
              WHERE EXISTS(SELECT 1 FROM atoms a JOIN sources s ON a.source_id=s.id
                           WHERE a.movie_digest=m.digest AND s.complete=1) ORDER BY m.digest'''))
        deferred = _DeferredMovies(case, pending)
        for sid in sources:
            pool.check(sid)
        _backfill_ordinary_publications(case)
        from .candidate_tiers import CandidateTiers
        pending.execute('CREATE TABLE visited_movies(digest TEXT PRIMARY KEY) WITHOUT ROWID')
        base_pool = pool
        tiers = CandidateTiers(case, base_pool)
        case._candidate_tiers = tiers
        case._search_tiers = {'policy': 'unclaimed_first', 'searched': [1],
                             'include_recovered': opt.include_recovered,
                             'second_tier_unsearched': tiers.has_recovered,
                             'uniqueness_scope': 'searched_tiers_only'}
        pool = tiers.view()
        for (digest,) in pending.execute('SELECT digest FROM selected ORDER BY digest'):
            row = case.db.execute('''SELECT m.*,r.status AS old_status,r.detail AS old_detail FROM movies m
                  LEFT JOIN jobs r ON r.kind='ordinary' AND r.key=m.digest WHERE m.digest=?''', (digest,)).fetchone()
            if not any(base_pool.permits(location['source_id'], location['offset'], location['size'])
                       for location in case.db.execute('SELECT source_id,offset,size FROM atoms WHERE movie_digest=?', (row['digest'],))):
                continue
            jobs_task.advance(processed_movies=processed)
            old_detail = json.loads(row['old_detail']) if row['old_detail'] else {}
            if old_detail.get('run_key') == run_key:
                if row['old_status'] != 'awaiting_deep' and (not retry or row['old_status'] in {'matched', 'reconstructed'}):
                    candidates = old_detail.get('candidates', [])
                    for c in candidates:
                        p = case.root / c['path']
                        if not p.is_file() or p.is_symlink() or file_sha256(p) != c['sha256']:
                            raise SafetyError(f'Previously validated candidate changed or disappeared: {p}')
                        _backfill_candidate_manifest(case, c)
                    for output in old_detail.get('outputs', []):
                        p = case.root / output
                        if (not p.is_file() or p.is_symlink() or len(candidates) != 1 or
                                file_sha256(p) != candidates[0]['sha256']):
                            raise SafetyError(f'Published output changed or disappeared: {p}')
                        published_manifest = p.with_suffix('.json')
                        evidence = json.loads(published_manifest.read_text()) if published_manifest.is_file() else {}
                        candidate_evidence = _backfill_candidate_manifest(case, candidates[0])
                        for field in ('ordered_fragments', 'historical_manifest_unavailable'):
                            if field in candidate_evidence:
                                evidence[field] = candidate_evidence[field]
                        write_json(published_manifest, evidence)
                    continue
            if limit is not None and processed >= limit:
                break
            processed += 1
            pending.execute('INSERT OR IGNORE INTO visited_movies VALUES(?)', (digest,))
            if row['error']:
                _scoped_result(case, row['digest'], 'unsupported', {'run_key': run_key, 'reason': row['error'], 'search_tiers': dict(_search_scope(case))})
                continue
            movie = parse_movie(row['data'])
            if movie.media_size > opt.max_output_bytes:
                _scoped_result(case, movie.digest, 'search_limited', {'run_key': run_key, 'reason': 'output_size_budget', 'search_tiers': dict(_search_scope(case))})
                continue
            LOG.info('Matching %s: %d chunks, %.2f MiB media', movie.digest[:12], len(movie.chunks), movie.media_size / 1024**2)
            owner_pool = tiers.view(('ordinary', movie.digest))
            status, candidates, whole_limited = whole_payload(case, movie, owner_pool, tools, opt, run_key)
            if opt.deep and status in {'unresolved', 'search_limited'} and not candidates:
                if whole_limited:
                    incomplete.add(movie.digest)
                current = case.job('ordinary', movie.digest)
                waiting = dict(current['detail']) if current else {}
                waiting.update(run_key=run_key, reason='whole-payload search finished; deep pass not yet completed',
                               search_tiers=dict(_search_scope(case)))
                _scoped_result(case, movie.digest, 'awaiting_deep', waiting)
                if old_detail.get('outputs') or old_detail.get('historical_publications'):
                    # A resumed target may revalidate its own accepted extents.
                    # Other targets' accepted bytes remain excluded by this view.
                    deep_search(case, [movie], owner_pool, tools, opt, run_key, incomplete=incomplete)
                else:
                    deferred.add(movie.digest)
        if deferred:
            deep_search(case, deferred, pool, tools, opt, run_key, incomplete=incomplete)
        if opt.include_recovered and tiers.has_recovered:
            # Only targets without any first-tier result may search recovered references.
            case._search_tiers = dict(case._search_tiers, searched=[1, 2], second_tier_unsearched=False)
            pending.execute('DELETE FROM deferred')
            fallback = deferred
            for (digest,) in pending.execute('SELECT digest FROM visited_movies ORDER BY digest'):
                job = case.job('ordinary', digest)
                if not job or job['detail'].get('run_key') != run_key or job['detail'].get('candidates'):
                    continue
                row = case.db.execute('SELECT data,error FROM movies WHERE digest=?', (digest,)).fetchone()
                if row['error']:
                    continue
                movie = parse_movie(row['data'])
                status, candidates, whole_limited = whole_payload(case, movie, base_pool, tools, opt, run_key)
                if opt.deep and not candidates and status in {'unresolved', 'search_limited'}:
                    if whole_limited:
                        incomplete.add(digest)
                    fallback.add(digest)
            if fallback:
                deep_search(case, fallback, base_pool, tools, opt, run_key, incomplete=incomplete)
        jobs_task.heartbeat(processed_movies=processed)
    case.event('recovery_complete', {'run_key': run_key, 'processed_movies': processed})
