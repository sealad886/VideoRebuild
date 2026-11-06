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
from contextlib import ExitStack, contextmanager, nullcontext
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
import time
from typing import Iterable

from . import __version__, nal_search
from .artifacts import ensure_generated_file, safe_case_path
from .bmff import Chunk, FormatError, Movie, make_box, parse_movie, rewrite_offsets, valid_sample
from .storage import Case, ReaderPool, SafetyError, file_sha256, json_text, write_json
from .validate import VALIDATION_REVISION, doctor, validate
from .progress import phase

LOG = logging.getLogger(__name__)
FTYP = make_box(b'ftyp', b'isom' + struct.pack('>I', 512) + b'isomiso6')
Mapping = MappingView[tuple[int, int], tuple[int, int]]
MAX_DEEP_METADATA = 32 * 1024 * 1024
MATCHING_REVISION = 5


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
    search_workers: int = 1

    def check(self) -> None:
        if type(self.search_workers) is not int or not 1 <= self.search_workers <= 8:
            raise SafetyError('search_workers must be an integer from 1 to 8')
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
    resume = getattr(case, '_ordinary_resume', None)
    checkpoint = resume.result_checkpoint(key, status, detail) if resume is not None else None
    if checkpoint is None:
        case.result(key, status, detail)
    else:
        case.result(key, status, detail, checkpoint=checkpoint)
        resume.committed(checkpoint)


def _refresh_tiers(case, owner=None):
    tiers = getattr(case, '_candidate_tiers', None)
    if tiers is not None:
        tiers.refresh(owner=owner)


def _translation_key(movie, sid, translation, pool):
    base = getattr(pool, 'discovery_pool', pool)
    keys = getattr(base, '_translation_source_keys', None)
    if keys is None:
        keys = base._translation_source_keys = {}
    if sid not in keys:
        row = pool.sources[sid]
        descriptor = {key: row.get(key) for key in
                      ('path', 'size', 'mtime_ns', 'ino', 'sha256', 'extents', 'binding')}
        from .structural_index import _plain
        keys[sid] = hashlib.sha256(json_text(_plain(descriptor)).encode()).hexdigest()
    descriptor = ['translated_negative', MATCHING_REVISION, VALIDATION_REVISION,
                  movie.digest, sid, translation, keys[sid],
                  getattr(getattr(pool, 'allocation_scope', None), 'digest', None)]
    return hashlib.sha256(json_text(descriptor).encode()).hexdigest(), descriptor


def attempt(case: Case, movie: Movie, mapping: Mapping, pool: ReaderPool, tools: dict,
            opt: Options, stage: str) -> dict:
    if not _tier_eligible(movie, mapping, pool):
        return {'status': 'rejected', 'stage': stage, 'reason': 'recovered_reference_second_tier'}
    prechecked_quick = None
    translated = isinstance(mapping, _TranslatedMapping) and mapping.plan.movie is movie
    work = getattr(case, '_matching_work', None)
    if work is None:
        work = case._matching_work = dict(translated_compact_lookups=0, translated_cold_negative_screens=0,
                                         translated_canonical_rows=0)
    if translated:
        try:
            mapping.check_source(pool)
        except FormatError as error:
            return {'status': 'rejected', 'stage': stage, 'reason': str(error)}
        compact, descriptor = _translation_key(movie, mapping.sid, mapping.translation, pool)
        work['translated_compact_lookups'] += 1
        alias = 'translated:' + compact
        cached = case.db.execute('SELECT value FROM settings WHERE name=?', (alias,)).fetchone()
        negative = None if cached else case.db.execute(
            'SELECT detail FROM attempts WHERE movie_digest=? AND mapping_digest=?', (movie.digest, compact)).fetchone()
        if negative:
            old = json.loads(negative[0])
            if (old.get('validation_revision') == VALIDATION_REVISION and
                    old.get('reconstruct_only', False) == opt.reconstruct_only and
                    old.get('tools') == tools and old.get('timeout') == opt.timeout and old.get('negative_screen')):
                return old
        if cached:
            md = cached[0]
        else:
            try:
                prechecked_quick = check_samples(movie, mapping, pool, quick=True, unavailable_raises=True)
            except FormatError as error:
                return {'status': 'rejected', 'stage': stage, 'reason': str(error)}
            if not prechecked_quick:
                work['translated_cold_negative_screens'] += 1
                result = dict(status='rejected', mapping_digest=compact, mapping_kind='translated_negative',
                              translated_descriptor=descriptor, stage=stage, tools=tools, timeout=opt.timeout,
                              validation_revision=VALIDATION_REVISION, reconstruct_only=opt.reconstruct_only,
                              reason='NAL_sample_boundaries_do_not_match', negative_screen=True,
                              range_validation='not_evaluated', content_evaluated=True)
                _record_attempt(case, movie, compact, stage, result)
                return result
            md = mapping_digest(movie, mapping)
            work['translated_canonical_rows'] += len(movie.chunks)
            case.db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (alias, md))
    else:
        md = mapping_digest(movie, mapping)
        # An eager uniform mapping can seed the same compact alias. This costs no
        # extra asymptotic work and keeps canonical evidence shared by callers.
        first = movie.chunks[0]
        if (first.track, first.index) in mapping:
            sid, off = mapping[(first.track, first.index)]
            translation = off-first.offset
            if sid in pool.sources and all((c.track, c.index) in mapping and
                    mapping[(c.track, c.index)] == (sid, translation+c.offset) for c in movie.chunks):
                compact, _ = _translation_key(movie, sid, translation, pool)
                case.db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', ('translated:'+compact, md))
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
            _restore_ordinary_candidate(case, pool, movie.digest, old, movie=movie, mapping=mapping)
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
        quick_ok = (prechecked_quick if prechecked_quick is not None else
                    check_samples(movie, mapping, pool, quick=True, unavailable_raises=True))
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



def _restore_ordinary_candidate(case, pool, digest, candidate, peers=(), *, movie=None, mapping=None):
    """Restore accepted bytes from a retained peer or their exact source mapping."""
    pool = getattr(pool, 'discovery_pool', pool)
    if mapping is not None and safe_case_path(case, candidate['path']).is_file():
        return ensure_generated_file(case, candidate['path'], candidate['sha256'])
    rebuilt_manifest = None
    retained = None

    def rebuild(destination):
        nonlocal rebuilt_manifest
        target_movie, selected = movie, mapping
        if target_movie is None:
            row = case.db.execute('SELECT data FROM movies WHERE digest=?', (digest,)).fetchone()
            if row is None:
                raise SafetyError('Missing indexed metadata for generated ordinary artifact')
            target_movie = parse_movie(row[0])
        if target_movie.digest != digest:
            raise SafetyError('Generated ordinary artifact metadata does not match its saved digest')
        if retained is not None:
            if retained.get('metadata_sha256', digest) != digest:
                raise SafetyError('Generated ordinary artifact provenance names different metadata')
            chunks = {(chunk.track, chunk.index): chunk for chunk in target_movie.chunks}
            saved = {}
            for entry in retained['chunks']:
                key = (entry['track'], entry['chunk'])
                sid, offset = entry['source_id'], entry['source_offset']
                if (key in saved or key not in chunks or type(sid) is not int or
                        type(offset) is not int or offset < 0 or entry['size'] != chunks[key].size or
                        sid not in pool.sources):
                    raise SafetyError('Incomplete or invalid generated ordinary artifact provenance')
                saved[key] = entry
            if set(saved) != set(chunks):
                raise SafetyError('Incomplete generated ordinary artifact chunk provenance')
            retained_mapping = {key: (entry['source_id'], entry['source_offset']) for key, entry in saved.items()}
            if selected is not None and any(selected[key] != value for key, value in retained_mapping.items()):
                raise SafetyError('Generated ordinary artifact mapping differs from retained provenance')
            selected = retained_mapping
        elif selected is None:
            raise SafetyError('Missing source mapping provenance for generated ordinary artifact')
        if candidate.get('mapping_digest') and mapping_digest(target_movie, selected) != candidate['mapping_digest']:
            raise SafetyError('Generated ordinary artifact mapping does not match its saved digest')
        source_ids = {sid for sid, _ in selected.values()}
        for sid in source_ids:
            pool.check(sid)
        zero = {(chunk.track, chunk.index): 0 for chunk in target_movie.chunks}
        size = len(FTYP) + len(rewrite_offsets(target_movie, zero)) + 16 + target_movie.media_size
        proof = reconstruct(target_movie, selected, pool, destination, size)
        if retained is not None:
            for entry in proof['chunks']:
                old = saved[(entry['track'], entry['chunk'])]
                if entry['sha256'] != old['sha256']:
                    raise SafetyError('Generated ordinary artifact source chunk hash changed')
                if old.get('physical_extents'):
                    fields = ('logical_offset', 'source_id', 'source_path', 'source_offset', 'length')
                    current = [{name: part.get(name) for name in fields} for part in entry['physical_extents']]
                    previous = [{name: part.get(name) for name in fields} for part in old['physical_extents']]
                    if current != previous:
                        raise SafetyError('Generated ordinary artifact source ranges changed')
        for sid in source_ids:
            pool.check(sid)
        rebuilt_manifest = {'version': __version__, 'stage': candidate.get('stage'),
                            'movie': target_movie.summary(), 'provenance': proof,
                            'ordered_fragments': proof['ordered_fragments'],
                            'validation': candidate.get('validation', {})}

    try:
        for name in (candidate['path'], *peers):
            sidecar = safe_case_path(case, str(Path(name).with_suffix('.json')))
            if not sidecar.is_file():
                continue
            evidence = json.loads(sidecar.read_text())
            proof = evidence.get('provenance', {})
            if proof.get('chunks'):
                retained = proof
                break
        referenced = {sid for sid, _ in mapping.values()} if mapping is not None else set()
        for entry in retained.get('chunks', []) if retained else ():
            sid = entry['source_id']
            source = pool.sources.get(sid)
            if source is None:
                raise SafetyError('Generated ordinary artifact source is unavailable')
            if (entry.get('source_path', source['path']) != source['path'] or
                    entry.get('source_sha256_at_scan', source['sha256']) != source['sha256']):
                raise SafetyError('Generated ordinary artifact source evidence changed')
            referenced.add(sid)
            for part in entry.get('physical_extents', []):
                physical = pool.physical.get(part['source_id'])
                if physical is None or part.get('source_path', physical['path']) != physical['path']:
                    raise SafetyError('Generated ordinary artifact physical source is unavailable')
                identity = part.get('source_identity', {})
                # Device IDs may change when the same source volume remounts.
                if any(identity.get(name, physical.get(name)) != physical.get(name)
                       for name in ('ino', 'size', 'mtime_ns', 'sha256')):
                    raise SafetyError('Generated ordinary artifact physical source evidence changed')
                referenced.add(part['source_id'])
        for sid in referenced:
            pool.check(sid)
        path = ensure_generated_file(case, candidate['path'], candidate['sha256'], peers=peers, rebuild=rebuild)
        for sid in referenced:
            pool.check(sid)
        sidecar = safe_case_path(case, str(Path(candidate['path']).with_suffix('.json')))
        if not sidecar.exists():
            evidence = None
            for name in peers:
                saved_sidecar = safe_case_path(case, str(Path(name).with_suffix('.json')))
                if saved_sidecar.is_file():
                    saved_evidence = json.loads(saved_sidecar.read_text())
                    if saved_evidence.get('provenance', {}).get('chunks'):
                        evidence = saved_evidence
                        break
            if evidence is None:
                evidence = rebuilt_manifest
            if evidence is not None:
                write_json(sidecar, evidence)
        return path
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise SafetyError('Incomplete or invalid source mapping provenance for generated ordinary artifact') from error


def restore_ordinary_job(case, pool, job):
    """Recreate absent generated media while retaining the saved recovery decision."""
    detail = job['detail']
    candidates = detail.get('candidates', [])
    groups = [(candidate, tuple(detail.get('outputs', [])) if len(candidates) == 1 else ())
              for candidate in candidates]
    groups.extend(({'path': entry['candidate'], 'sha256': entry['sha256']}, (entry['path'],))
                  for entry in detail.get('historical_publications', []))
    missing = []
    for candidate, outputs in groups:
        paths = [safe_case_path(case, name) for name in (candidate['path'], *outputs)]
        if any(not path.is_file() for path in paths):
            missing.append((candidate, outputs))
    if not missing:
        return
    pool = getattr(pool, 'discovery_pool', pool)
    for candidate, outputs in missing:
        _restore_ordinary_candidate(case, pool, job['key'], candidate, outputs)
        for output in outputs:
            if output != candidate['path']:
                ensure_generated_file(case, output, candidate['sha256'], peers=(candidate['path'],))


def _verify_job_artifacts(case, pool, digest, detail):
    """A scheduling receipt never replaces saved candidate/publication integrity."""
    restore_ordinary_job(case, pool, {'key': digest, 'detail': detail})
    candidates = detail.get('candidates', [])
    for candidate in candidates:
        path = case.root / candidate['path']
        if not path.is_file() or path.is_symlink() or file_sha256(path) != candidate['sha256']:
            raise SafetyError(f'Previously validated candidate changed or disappeared: {path}')
        _backfill_candidate_manifest(case, candidate)
    for output in detail.get('outputs', []):
        path = case.root / output
        if (not path.is_file() or path.is_symlink() or len(candidates) != 1 or
                file_sha256(path) != candidates[0]['sha256']):
            raise SafetyError(f'Published output changed or disappeared: {path}')


def _backfill_ordinary_publications(case, pool):
    for job in case.get_jobs('ordinary'):
        restore_ordinary_job(case, pool, job)
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
    """Publish the canonical artifact, with a case-internal link when supported."""
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
        try:
            os.link(candidate, final)
        except OSError as error:
            if not _link_unavailable(error):
                raise
            # The accepted candidate and its manifest already form a complete
            # publication. Filesystems without links must not retain a second
            # media allocation merely to provide a reconstructed/ directory name.
            LOG.warning('Hard links unavailable; publishing the accepted candidate without copying: %s', candidate)
            return [successes[0]['path']]
        storage_mode = 'case_internal_hardlink'
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
    _refresh_tiers(case, owner=('ordinary', movie.digest))
    LOG.info('%s %s: %d byte-distinct candidates (%s)', movie.digest[:12], status, len(good),
             'structural reconstruction; no decode check' if any(c.get('reconstruct_only') for c in good)
             else 'decode checked' if good else 'no accepted candidate; no decode claim')
    return status


@contextmanager
def _search_store(case: Case):
    # Keep cardinality tables on the case volume, even when TMPDIR points to a
    # smaller system disk. Do not change process-wide temp state: a TUI session
    # can operate on more than one case.
    work = case.root / 'work'
    if work.is_symlink() or (work.exists() and not work.is_dir()):
        raise SafetyError(f'Case search work path must be a real directory: {work}')
    work.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='videorebuild-search-', dir=work) as directory:
        db = sqlite3.connect(Path(directory) / 'search.sqlite3')
        try:
            # Main search tables stay disk-backed. Auxiliary SQLite TEMP/sort
            # state must not create anonymous files on the system temp volume.
            db.execute('PRAGMA temp_store=MEMORY')
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
    with _search_store(case) as search, ExitStack() as origin_context:
        from .origin_index import OriginIndex, first_anchor
        mandatory = first_anchor(movie)
        origins = getattr(metadata_pool, 'origin_index', None)
        if mandatory is not None and all(value is None for value in
                (opt.max_candidates, opt.max_anchor_checks, opt.max_hits, opt.max_assemblies)):
            if origins is None:
                origins = origin_context.enter_context(OriginIndex(case, metadata_pool))
            origins.prepare([mandatory])
        else:
            origins = None
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
        if origins is None:
            search.executemany('INSERT INTO candidates(sid,offset,header) VALUES(?,?,?)',
                               case.db.execute(query, parameters))
        else:
            rows = ((sid, offset, header) for sid, offset, header, payload in origins.candidates(*mandatory)
                    if payload >= movie.span and (opt.max_padding is None or payload <= movie.span+opt.max_padding))
            search.executemany('INSERT INTO candidates(sid,offset,header) VALUES(?,?,?)', rows)
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
                    {'tested_hypotheses': tested, 'max_trailing_padding': opt.max_padding,
                     'matching_work': dict(getattr(case, '_matching_work', {})),
                     'origin_index': dict(origins.stats) if origins is not None else None})
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


def _anchor_locations(pool, maximum_sample, wanted, opt, state, task, *, source_ids=None):
    """Canonical bounded NAL-chain discovery, independent of target batches."""
    high = ((maximum_sample - 4) >> 16) & 255
    # Zero-width matching preserves overlapping length prefixes at block edges.
    pattern = re.compile(fr'(?=\x00[\x00-\x{high:02x}][\s\S]{{2}})(?!\x00{{4}})'.encode('ascii'))

    # Header facts are independent of the sample start and cumulative length.
    # Bound cache memory; cache no source bytes and clear at each read block.
    heads = {}

    def header(sid, block_base, data, pos):
        if pos in heads:
            state['discovery_header_cache_hits'] = state.get('discovery_header_cache_hits', 0) + 1
            return heads[pos]
        state['discovery_header_reads'] = state.get('discovery_header_reads', 0) + 1
        def read(offset, count):
            local = offset-block_base
            if 0 <= local and local+count <= len(data):
                return data[local:local+count]
            return pool.read(sid, offset, count)
        value = nal_search.header(read, pos)
        if len(heads) >= 8192:
            heads.clear()
        heads[pos] = value
        return value

    def totals(sid, block_base, data, start):
        def visit():
            if opt.max_anchor_checks is not None and state['nal_checks'] >= opt.max_anchor_checks:
                state.update(limited=True, stop_reason='nal_check_budget')
                return False
            state['nal_checks'] += 1
            if state['nal_checks'] % 4096 == 0:
                task.heartbeat(nal_checks=state['nal_checks'], anchor_checks=state['anchor_checks'])
            return True
        for length in nal_search.cumulative_lengths(lambda pos: header(sid, block_base, data, pos),
                                                   start, pool.sources[sid]['size'], maximum_sample, visit):
            if wanted(length):
                yield length

    scanned = 0
    for sid in (pool.sources if source_ids is None else source_ids):
        pool.check(sid)
        try:
            for base, data, owned in pool.iter_blocks(sid, overlap=16):
                heads.clear()
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


class _OrdinaryResume:
    """Durable stage receipts; selected metadata and byte-search tables stay bounded."""
    def __init__(self, case, run_key, deep, retry):
        self.case, self.run_key, self.deep = case, run_key, deep
        row = case.db.execute('SELECT complete FROM ordinary_resume_runs WHERE run_key=?', (run_key,)).fetchone()
        self.retry_completed = bool(retry and row and row[0])
        self.tier, self.stage, self.owner = 1, None, False
        if self.retry_completed:
            # Persist the entire explicit retry generation before any limit or
            # interruption. A later invocation resumes its remaining members.
            case.db.execute('''UPDATE ordinary_resume SET state=CASE state
                WHEN 'deep_complete' THEN 'deep_pending'
                WHEN 'preliminary_complete' THEN 'preliminary_retry' ELSE state END
                WHERE run_key=? AND EXISTS(SELECT 1 FROM jobs j WHERE j.kind='ordinary'
                    AND j.key=ordinary_resume.digest AND j.status NOT IN ('matched','reconstructed')
                    AND json_extract(j.detail,'$.run_key')=?)''', (run_key, run_key))
        self._counts = dict(case.db.execute('SELECT state,count(*) FROM ordinary_resume WHERE run_key=? AND tier=1 GROUP BY state', (run_key,)).fetchall())
        case.db.execute('INSERT INTO ordinary_resume_runs VALUES(?,0) ON CONFLICT(run_key) DO UPDATE SET complete=0', (run_key,))
        case.db.commit()

    def __enter__(self):
        self.case._ordinary_resume = self
        return self

    def __exit__(self, *_):
        del self.case._ordinary_resume

    def context(self, tier, stage, owner=False):
        self.tier, self.stage, self.owner = tier, stage, owner

    def get(self, digest, tier=1):
        row = self.case.db.execute('SELECT * FROM ordinary_resume WHERE run_key=? AND tier=? AND digest=?',
                                   (self.run_key, tier, digest)).fetchone()
        return dict(row) if row else None

    def result_checkpoint(self, digest, status, detail):
        if self.stage is None or detail.get('run_key') != self.run_key:
            return None
        previous = self.get(digest, self.tier)
        limited = bool(detail.get('search_limited'))
        if self.stage == 'deep':
            state = 'deep_complete'
            limited = bool(previous and previous['whole_limited'])
        else:
            state = ('deep_pending' if self.deep and status in {'awaiting_deep', 'unresolved', 'search_limited'}
                     and not detail.get('candidates') and detail.get('reason') != 'output_size_budget'
                     else 'preliminary_complete')
        return dict(run_key=self.run_key, tier=self.tier, state=state,
                    whole_limited=limited, owner=self.owner,
                    previous_state=previous['state'] if previous else None)

    def committed(self, checkpoint):
        if checkpoint['tier'] != 1:
            return
        before, after = checkpoint.get('previous_state'), checkpoint['state']
        if before is not None:
            self._counts[before] = self._counts.get(before, 0)-1
        self._counts[after] = self._counts.get(after, 0)+1

    def adopt(self, digest, status, detail):
        # The old durable awaiting_deep result itself proves preliminary work
        # returned. Import only that exact evidence, not arbitrary failed jobs.
        if (status != 'awaiting_deep' or detail.get('run_key') != self.run_key or
                detail.get('stage') != 'whole_payload' or detail.get('candidates') or
                detail.get('reason') != 'whole-payload search finished; deep pass not yet completed'):
            return None
        tier = 2 if detail.get('search_tiers', {}).get('searched') == [1, 2] else 1
        checkpoint = dict(run_key=self.run_key, tier=tier, state='deep_pending',
                          whole_limited=bool(detail.get('search_limited')),
                          owner=bool(detail.get('outputs') or detail.get('historical_publications')))
        if tier == 2:
            # Entering fallback proves the first tier finished without a
            # candidate or an unsearched limit. Import both receipts in the
            # same transaction as the retained waiting result.
            self.case.db.execute('INSERT OR IGNORE INTO ordinary_resume VALUES(?,?,?,?,?,?,?)',
                                 (self.run_key, 1, digest, 'deep_complete', 0,
                                  int(checkpoint['owner']), time.time()))
        self.case.result(digest, status, detail, checkpoint=checkpoint)
        self.committed(checkpoint)
        if tier == 2:
            self.committed(dict(tier=1, state='deep_complete'))
        return self.get(digest)

    def discard(self, digest):
        previous = self.get(digest)
        self.case.db.execute('DELETE FROM ordinary_resume WHERE run_key=? AND digest=?', (self.run_key, digest))
        self.case.db.commit()
        if previous:
            self._counts[previous['state']] -= 1

    def verified(self, digest):
        if self.get(digest) is not None:
            return
        self.case.db.execute('INSERT INTO ordinary_resume VALUES(?,?,?,?,?,?,?)',
                             (self.run_key, 1, digest, 'preliminary_complete', 0, 1, time.time()))
        self.case.db.commit()
        self.committed(dict(tier=1, state='preliminary_complete'))

    def counts(self):
        values = self._counts
        return dict(preliminary_completed=sum(values.values()), deferred_pending=values.get('deep_pending', 0),
                    deep_completed=values.get('deep_complete', 0))

    def complete(self):
        if self.case.db.execute("SELECT 1 FROM ordinary_resume WHERE run_key=? AND state IN ('deep_pending','preliminary_retry') LIMIT 1",
                                (self.run_key,)).fetchone() is not None:
            return
        self.case.db.execute('UPDATE ordinary_resume_runs SET complete=1 WHERE run_key=?', (self.run_key,))
        self.case.db.commit()


class _DeferredMovies:
    """Repeatable metadata stream backed by selected digests, not retained BLOBs."""
    def __init__(self, case, store, resume=None, tier=1):
        self.case, self.store, self.resume, self.tier = case, store, resume, tier
        self.table = 'deferred' if tier == 1 else 'deferred_fallback'
        store.execute(f'CREATE TABLE {self.table}(digest TEXT PRIMARY KEY) WITHOUT ROWID')

    def add(self, digest):
        self.store.execute(f'INSERT OR IGNORE INTO {self.table} VALUES(?)', (digest,))

    def __iter__(self):
        for (digest,) in self.store.execute(f'SELECT digest FROM {self.table} ORDER BY digest'):
            # Each lookup finishes before callers commit a result. Completed
            # members disappear from subsequent passes over this lazy stream.
            if self.resume is not None and self.resume.get(digest, self.tier)['state'] != 'deep_pending':
                continue
            row = self.case.db.execute('SELECT data FROM movies WHERE digest=?', (digest,)).fetchone()
            yield parse_movie(row[0])

    def __bool__(self):
        return self.store.execute(f'SELECT 1 FROM {self.table} LIMIT 1').fetchone() is not None


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
    capped = any(value is not None for value in (opt.max_anchor_checks, opt.max_hits, opt.max_assemblies))
    if capped:
        # Budget semantics remain local and explicit; an incomplete posting set
        # cannot support a necessary-anchor intersection or a negative receipt.
        for batch in _movie_batches(movies, opt.max_groups):
            _deep_batch(case, batch, pool, tools, opt, run_key, incomplete or set())
        return
    from .structural_index import postings
    lengths = set()
    base_pool = getattr(pool, 'discovery_pool', pool)
    catalogue = getattr(case, '_structural_catalogue_lengths', None)
    if catalogue is None and isinstance(getattr(case, 'db', None), sqlite3.Connection):
        catalogue = set()
        # Read one metadata BLOB at a time; finish all catalogue queries before
        # entering byte discovery or committing attempts.
        for (digest,) in case.db.execute('''SELECT digest FROM movies WHERE error IS NULL AND EXISTS(
                SELECT 1 FROM atoms JOIN sources ON atoms.source_id=sources.id
                WHERE atoms.movie_digest=movies.digest AND sources.complete=1) ORDER BY digest'''):
            if not any(row[0] in base_pool.sources and base_pool.permits(*row) for row in case.db.execute(
                    'SELECT source_id,offset,size FROM atoms WHERE movie_digest=?', (digest,))):
                continue
            row = case.db.execute('SELECT data FROM movies WHERE digest=?', (digest,)).fetchone()
            target = parse_movie(row[0])
            catalogue.update(length for group in groups_for(target) for length, _ in group.anchors())
        catalogue = case._structural_catalogue_lengths = frozenset(catalogue)
    for movie in movies:
        groups = groups_for(movie)
        if any(not group.anchors() for group in groups):
            continue
        lengths.update(length for group in groups for length, _ in group.anchors())
    if catalogue is not None and lengths.issubset(catalogue):
        lengths = catalogue
    elif catalogue:
        lengths = frozenset(lengths | catalogue)
        case._structural_catalogue_lengths = lengths
    with phase('Deep structural index', unit='bytes') as task, \
            postings(case, base_pool, lengths, _anchor_locations, opt, task) as (discovery, discovery_stats):
        for batch in _movie_batches(movies, opt.max_groups):
            _deep_batch(case, batch, pool, tools, opt, run_key, incomplete or set(), discovery=discovery,
                        discovery_nal_checks=discovery_stats['nal_checks'], discovery_stats=discovery_stats)
        if not getattr(base_pool, '_recovery_active', False):
            for sid in pool.sources:
                pool.check(sid)


def _deep_batch(case: Case, movies: list[Movie], pool: ReaderPool, tools: dict,
                opt: Options, run_key: str, incomplete: set[str], *, discovery=None, discovery_nal_checks=0, discovery_stats=None) -> None:
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
    with _search_store(case) as search:
        # An indexed Group picks one seed. Its unique posting keys translate
        # to unique bases, so only the paths with multiple seeds need dedup.
        if discovery is None or any(not isinstance(group, Group) for group in groups.values()):
            search.execute('CREATE TABLE visited (movie TEXT,g INTEGER,sid INTEGER,start INTEGER,PRIMARY KEY(movie,g,sid,start)) WITHOUT ROWID')
        search.execute('CREATE TABLE hits (movie TEXT,g INTEGER,fingerprint TEXT,sid INTEGER,start INTEGER,PRIMARY KEY(movie,g,sid,start)) WITHOUT ROWID')
        search.execute('CREATE INDEX hits_order ON hits(movie,g,fingerprint,sid,start)')
        counts_by_group = {key: 0 for key in groups}
        capped = set()
        state = dict(anchor_checks=0, nal_checks=discovery_nal_checks, limited=False, stop_reason=None, located_extents=0,
                     anchor_associations=0, posting_probes=0, posting_rejections=0, duplicate_bases=0)
        LOG.info('Deep search: %d extents, %d anchor sizes across %d sources', len(groups), len(anchors), len(pool.sources))
        available = (sum(length for sid in pool.sources for _, length in pool.available_ranges(sid))
                     if discovery is None else None)
        label = 'Deep search available bytes' if discovery is None else 'Deep anchor associations'
        with phase(label, total=available, unit='bytes' if discovery is None else 'locations') as scan_task:
            if discovery is None:
                locations = ((sid, at, length, anchors.get(length, ())) for sid, at, length in
                             _anchor_locations(pool, max(anchors)+4, anchors.__contains__, opt, state, scan_task))
            else:
                def replay():
                    frequencies = {}
                    for length in anchors:
                        row = discovery.execute('SELECT total FROM frequencies WHERE length=?', (length,)).fetchone()
                        frequencies[length] = row[0] if row else 0
                    for key, group in groups.items():
                        required = group.anchors()
                        # Only real complete Group anchors have the necessary
                        # sample predicate. Unsupported custom models retain OR.
                        intersect = isinstance(group, Group)
                        ordered = sorted(required, key=lambda item: (frequencies[item[0]], item))
                        seeds = ordered[:1] if intersect else ordered
                        for length, relative in seeds:
                            for sid, at in discovery.execute('SELECT sid,start FROM locations WHERE length=? ORDER BY sid,start', (length,)):
                                pool.check(sid)
                                start = at-relative
                                if intersect:
                                    present = True
                                    for other_length, other_relative in ordered[1:]:
                                        state['posting_probes'] += 1
                                        if discovery.execute('SELECT 1 FROM locations WHERE length=? AND sid=? AND start=?',
                                                             (other_length, sid, start+other_relative)).fetchone() is None:
                                            present = False
                                            state['posting_rejections'] += 1
                                            break
                                    if not present:
                                        continue
                                yield sid, start, length, ((key, 0),)
                locations = replay()
            visited_locations = 0
            try:
                for sid, at, length, associations in locations:
                    visited_locations += 1
                    source = pool.sources[sid]
                    for key, relative in associations:
                        state['anchor_associations'] += 1
                        if key in capped:
                            continue
                        group = groups[key]
                        start = at - relative
                        if start < 0 or start + group.span > source['size']:
                            continue
                        if (discovery is None or not isinstance(group, Group)) and not search.execute(
                            'INSERT OR IGNORE INTO visited VALUES(?,?,?,?)', (*key, sid, start)
                        ).rowcount:
                            state['duplicate_bases'] += 1
                            continue
                        if any(not pool.permits(sid, start+c.offset-group.start, c.size) for c in group.chunks):
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
                       'anchor_associations': state['anchor_associations'], 'posting_probes': state['posting_probes'],
                       'posting_rejections': state['posting_rejections'], 'duplicate_bases': state['duplicate_bases'],
                       'discovery': discovery_stats or {},
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



def _group_physical_ranges(group, choice, pool, cache=None):
    sid, start = choice
    key = (group.key, choice)
    if cache is not None and key in cache:
        pool.check(sid)
        yield from cache[key]
        return
    parts = []
    for chunk in group.chunks:
        for part in pool.provenance(sid, start+chunk.offset-group.start, chunk.size):
            physical = pool.physical_key(part['source_id'])
            parts.append((*physical, part['source_offset'], part['source_offset']+part['length']))
    if cache is not None and len(cache) < 4096:
        cache[key] = tuple(parts)
    yield from parts


def _prune_physical_domains(search, groups, pool, stats, footprints):
    # Dense or huge domains retain streaming DFS; no optimization may discard
    # values merely because its own memory/edge budget is exhausted.
    total = sum(search.execute('SELECT count(*) FROM hits WHERE movie=? AND g=?', group.key).fetchone()[0]
                for group in groups)
    if total > 4096 or len(groups) < 2:
        stats['constraint_complete'] = False
        stats['constraint_skipped_large_domains'] = total > 4096
        return
    from .constraints import prune_domains
    domains = []
    for group in groups:
        domain = []
        unavailable = []
        for choice in search.execute('SELECT sid,start FROM hits WHERE movie=? AND g=? ORDER BY sid,start', group.key):
            try:
                parts = tuple(_group_physical_ranges(group, choice, pool, footprints))
            except FormatError:
                # An earlier movie may have demoted these hits after discovery.
                # This scope exclusion is not a byte-validation negative.
                unavailable.append(choice)
                continue
            domain.append((choice, parts))
        search.executemany('DELETE FROM hits WHERE movie=? AND g=? AND sid=? AND start=?',
                           ((*group.key, *choice) for choice in unavailable))
        domains.append(domain)
    pruned, counters = prune_domains(domains)
    stats.update(counters)
    if not counters['constraint_complete']:
        return
    for group, before, after in zip(groups, domains, pruned):
        retained = {choice for choice, _ in after}
        search.executemany('DELETE FROM hits WHERE movie=? AND g=? AND sid=? AND start=?',
                           ((*group.key, *choice) for choice, _ in before if choice not in retained))


def _content_assemblies(search, groups, pool, stats, task):
    footprints = {}
    _prune_physical_domains(search, groups, pool, stats, footprints)
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
                                fingerprints=[choice[0]], stats=stats, task=task, initial_occupied=occupied, footprints=footprints)
        try:
            added = next(extension, None)
        finally:
            extension.close()
        if added is not None:
            witness_choices.append(added[0])
            parts = list(_group_physical_ranges(groups[order[depth]], added[0], pool, footprints))
            witness_parts.append(parts)
            for extent in parts:
                occupied.insert(bisect_left(occupied, extent), extent)
        else:
            physical = _assemblies(search, [groups[i] for i in indices], pool,
                                   counts=[item[1] for item in selected],
                                   fingerprints=[item[0] for item in selected], stats=stats, task=task, footprints=footprints)
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
            witness_parts = [list(_group_physical_ranges(groups[i], item, pool, footprints)) for i, item in zip(indices, witness)]
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
                                       fingerprints=[item[0] for item in selected], stats=stats, task=task, footprints=footprints)
                try:
                    for other in physical:
                        if other != first:
                            yield restore(other)
                finally:
                    physical.close()
            yield alternatives()
        else:
            depth += 1


def _assemblies(search, groups, pool=None, *, counts=None, stats=None, task=None, fingerprints=None, initial_occupied=(), footprints=None):
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
                for extent in _group_physical_ranges(group, choice, pool, footprints):
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
    # Discovery scheduling changes execution, not the searched evidence or model.
    semantic_options = {key: value for key, value in asdict(opt).items() if key != 'search_workers'}
    run_key = hashlib.sha256(json_text({'version': __version__, 'matching_revision': MATCHING_REVISION, 'validation_revision': VALIDATION_REVISION,
                         'allocation_scope': allocation_scope.digest if allocation_scope is not None else None,
                         'options': semantic_options, 'tools': tools,
                         'sources': [(sid,s['sha256'],s['path'],s.get('extents'),s.get('binding'))
                                     for sid,s in sorted(sources.items())]}).encode()).hexdigest()
    case.event('recovery_started', {'run_key': run_key, 'tools': tools, 'options': asdict(opt),
                 'validation_revision': VALIDATION_REVISION})
    incomplete = set()
    processed = 0
    catalogue_complete = True
    resumed_preliminary = 0
    with phase('Ordinary catalogue jobs visited', unit='jobs') as jobs_task, ReaderPool(sources,physical_sources=case.physical_sources(), allocation_scope=allocation_scope) as pool, _search_store(case) as pending, ExitStack() as indexed, _OrdinaryResume(case, run_key, opt.deep, retry) as resume:
        # Snapshot only selected identities on disk, then load one metadata
        # BLOB at a time with a completed point query. Streaming the case SELECT
        # through attempt writes pins its WAL for the entire recovery.
        pending.execute('CREATE TABLE selected(digest TEXT PRIMARY KEY) WITHOUT ROWID')
        pending.executemany('INSERT INTO selected VALUES(?)', case.db.execute('''SELECT m.digest FROM movies m
              WHERE EXISTS(SELECT 1 FROM atoms a JOIN sources s ON a.source_id=s.id
                           WHERE a.movie_digest=m.digest AND s.complete=1) ORDER BY m.digest'''))
        deferred = _DeferredMovies(case, pending, resume)
        for sid in sources:
            pool.check(sid)
        _backfill_ordinary_publications(case, pool)
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
        case._structural_catalogue_lengths = None
        base_pool._recovery_active = True
        if all(value is None for value in (opt.max_candidates, opt.max_anchor_checks, opt.max_hits, opt.max_assemblies)):
            from .origin_index import OriginIndex, first_anchor
            origins = base_pool.origin_index = indexed.enter_context(OriginIndex(case, base_pool))
            profiles = set()
            for (digest,) in pending.execute('SELECT digest FROM selected ORDER BY digest'):
                row = case.db.execute('SELECT data,error FROM movies WHERE digest=?', (digest,)).fetchone()
                if row['error']:
                    continue
                if not any(location[0] in base_pool.sources and base_pool.permits(*location) for location in case.db.execute(
                        'SELECT source_id,offset,size FROM atoms WHERE movie_digest=?', (digest,))):
                    continue
                anchor = first_anchor(parse_movie(row['data']))
                if anchor is not None:
                    profiles.add(anchor)
            origins.prepare(profiles)
        for (digest,) in pending.execute('SELECT digest FROM selected ORDER BY digest'):
            row = case.db.execute('''SELECT m.*,r.status AS old_status,r.detail AS old_detail FROM movies m
                  LEFT JOIN jobs r ON r.kind='ordinary' AND r.key=m.digest WHERE m.digest=?''', (digest,)).fetchone()
            if not any(base_pool.permits(location['source_id'], location['offset'], location['size'])
                       for location in case.db.execute('SELECT source_id,offset,size FROM atoms WHERE movie_digest=?', (row['digest'],))):
                continue
            jobs_task.advance(processed_movies=processed, resumed_preliminary=resumed_preliminary, **resume.counts())
            old_detail = json.loads(row['old_detail']) if row['old_detail'] else {}
            saved = resume.get(digest)
            if saved is not None and old_detail.get('run_key') != run_key:
                # Jobs expose the latest result. Receipts from an older visit
                # to this scope cannot certify a different scope's job row.
                resume.discard(digest)
                saved = None
            if saved is None and opt.deep:
                saved = resume.adopt(digest, row['old_status'], old_detail)
            if saved is not None:
                # Successful publications still pass the existing fresh hash
                # gates below. A resume receipt never substitutes for integrity.
                successful = row['old_status'] in {'matched', 'reconstructed'}
                if not successful:
                    if saved['state'] == 'deep_pending':
                        if limit is not None and processed >= limit:
                            catalogue_complete = False
                            break
                        processed += 1
                        resumed_preliminary += 1
                        pending.execute('INSERT OR IGNORE INTO visited_movies VALUES(?)', (digest,))
                        if saved['whole_limited']:
                            incomplete.add(digest)
                        if saved['owner']:
                            resume.context(1, 'deep', True)
                            movie = parse_movie(row['data'])
                            deep_search(case, [movie], tiers.view(('ordinary', digest)), tools, opt, run_key, incomplete=incomplete)
                        else:
                            deferred.add(digest)
                        continue
                    if saved['state'] != 'preliminary_retry':
                        _verify_job_artifacts(case, base_pool, digest, old_detail)
                        pending.execute('INSERT OR IGNORE INTO visited_movies VALUES(?)', (digest,))
                        resumed_preliminary += 1
                        continue
            if old_detail.get('run_key') == run_key:
                if (not (saved and saved['state'] == 'preliminary_retry') and
                        row['old_status'] != 'awaiting_deep' and (not retry or row['old_status'] in {'matched', 'reconstructed'})):
                    _verify_job_artifacts(case, base_pool, digest, old_detail)
                    if row['old_status'] in {'matched', 'reconstructed'}:
                        resume.verified(digest)
                    continue
            if limit is not None and processed >= limit:
                catalogue_complete = False
                break
            processed += 1
            pending.execute('INSERT OR IGNORE INTO visited_movies VALUES(?)', (digest,))
            resume.context(1, 'preliminary', bool(old_detail.get('outputs') or old_detail.get('historical_publications')))
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
                    resume.context(1, 'deep', True)
                    deep_search(case, [movie], owner_pool, tools, opt, run_key, incomplete=incomplete)
                else:
                    deferred.add(movie.digest)
        if deferred:
            resume.context(1, 'deep')
            deep_search(case, deferred, pool, tools, opt, run_key, incomplete=incomplete)
        if opt.include_recovered and tiers.has_recovered:
            # Recovered references are eligible only after a completed first-tier miss.
            fallback = _DeferredMovies(case, pending, resume, tier=2)
            for (digest,) in pending.execute('SELECT digest FROM visited_movies ORDER BY digest'):
                first_tier = resume.get(digest)
                completed_stage = 'deep_complete' if opt.deep else 'preliminary_complete'
                if first_tier is None or first_tier['state'] != completed_stage:
                    continue
                job = case.job('ordinary', digest)
                if not job or job['detail'].get('run_key') != run_key or job['detail'].get('candidates'):
                    continue
                saved = resume.get(digest, 2)
                if saved is None and (job['status'] == 'search_limited' or job['detail'].get('search_limited')):
                    # A fallback success cannot settle unexamined first-tier alternatives.
                    # An existing tier-two receipt already proves first-tier
                    # eligibility; its own interrupted limits are separate.
                    continue
                row = case.db.execute('SELECT data,error FROM movies WHERE digest=?', (digest,)).fetchone()
                if row['error']:
                    continue
                movie = parse_movie(row['data'])
                case._search_tiers = dict(case._search_tiers, searched=[1, 2], second_tier_unsearched=False)
                if saved is not None:
                    if saved['state'] == 'deep_pending':
                        if saved['whole_limited']:
                            incomplete.add(digest)
                        fallback.add(digest)
                        continue
                    if saved['state'] != 'preliminary_retry':
                        continue
                resume.context(2, 'preliminary')
                status, candidates, whole_limited = whole_payload(case, movie, base_pool, tools, opt, run_key)
                if opt.deep and not candidates and status in {'unresolved', 'search_limited'}:
                    if whole_limited:
                        incomplete.add(digest)
                    current = case.job('ordinary', digest)
                    waiting = dict(current['detail']) if current else {}
                    waiting.update(run_key=run_key, stage='whole_payload',
                                   reason='whole-payload search finished; deep pass not yet completed',
                                   search_limited=whole_limited)
                    _scoped_result(case, digest, 'awaiting_deep', waiting)
                    fallback.add(digest)
            if fallback:
                resume.context(2, 'deep')
                deep_search(case, fallback, base_pool, tools, opt, run_key, incomplete=incomplete)
        for sid in base_pool.sources:
            base_pool.check(sid)
        counts = resume.counts()
        jobs_task.heartbeat(processed_movies=processed, resumed_preliminary=resumed_preliminary, **counts)
        case.event('ordinary_resume_progress', dict(run_key=run_key, resumed_preliminary=resumed_preliminary, **counts))
        if catalogue_complete:
            resume.complete()
    case.event('recovery_complete', {'run_key': run_key, 'processed_movies': processed})
