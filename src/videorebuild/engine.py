"""Automated payload matching, offset rewriting, and scattered-extent search.

Matching is a hypothesis test, not proof of identity. Surviving metadata supplies
sample lengths/timing; NAL framing rejects most wrong associations; independent
full decodes and exact packet/frame counts gate publication. Alternatives and
search limits are retained instead of being hidden behind a confidence score.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
import hashlib
import errno
import itertools
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import struct
import time
from typing import Iterable

from . import __version__
from .bmff import Chunk, FormatError, Movie, make_box, parse_movie, rewrite_offsets, valid_sample
from .storage import Case, ReaderPool, SafetyError, file_sha256, json_text, write_json
from .validate import doctor, validate

LOG = logging.getLogger(__name__)
FTYP = make_box(b'ftyp', b'isom' + struct.pack('>I', 512) + b'isomiso6')
Mapping = dict[tuple[int, int], tuple[int, int]]


@dataclass(frozen=True)
class Options:
    max_candidates: int = 512
    max_padding: int = 1024 * 1024
    timeout: float = 300.0
    max_output_bytes: int = 16 * 1024**3
    deep: bool = False
    max_groups: int = 200_000
    max_hits: int = 32
    max_assemblies: int = 64
    max_anchor_checks: int = 2_000_000

    def check(self) -> None:
        for k, v in asdict(self).items():
            if k == 'deep':
                continue
            if not math.isfinite(v) or (v < 0 if k == 'max_padding' else v <= 0):
                raise SafetyError(f'{k} must be a positive finite value (padding may be zero)')


def mapping_digest(movie: Movie, mapping: Mapping) -> str:
    value = [movie.digest, sorted((t, c, sid, off) for (t, c), (sid, off) in mapping.items())]
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def source_ranges(movie: Movie, mapping: Mapping) -> list[tuple[int, int, int]]:
    return [(mapping[(c.track, c.index)][0], mapping[(c.track, c.index)][1], c.size)
            for c in movie.chunks]


def validate_ranges(movie: Movie, mapping: Mapping, pool: ReaderPool) -> None:
    expected = {(c.track, c.index) for c in movie.chunks}
    if set(mapping) != expected:
        raise FormatError('incomplete chunk assignment')
    ranges = sorted(source_ranges(movie, mapping))
    physical = []
    for sid, off, n in ranges:
        if sid not in pool.sources or off < 0 or off+n > pool.sources[sid]['size']:
            raise FormatError('chunk outside source')
        for extent in pool.provenance(sid,off,n):
            row = pool.physical[extent['source_id']]
            physical.append((row['dev'],row['ino'],extent['source_offset'],extent['length']))
    for a, b in zip(ranges, ranges[1:]):
        if a[0] == b[0] and a[1]+a[2] > b[1]:
            raise FormatError('source byte ranges reused or overlapped')
    physical.sort()
    for a,b in zip(physical,physical[1:]):
        if a[:2] == b[:2] and a[2]+a[3] > b[2]:
            raise FormatError('physical source bytes reused or overlapped')


def check_samples(movie: Movie, mapping: Mapping, pool: ReaderPool, quick: bool = False,
                  chunks: Iterable[Chunk] | None = None) -> bool:
    """Check AVC/HEVC access-unit framing against *metadata* sample boundaries."""
    selected = list(movie.chunks if chunks is None else chunks)
    selected = [c for c in selected if movie.tracks[c.track].handler == b'vide']
    if quick and len(selected) > 12:
        indexes = sorted({0, 1, 2, len(selected)//2, len(selected)-3, len(selected)-2, len(selected)-1})
        selected = [selected[i] for i in indexes]
    for chunk in selected:
        t = movie.tracks[chunk.track]
        sid, base = mapping[(chunk.track, chunk.index)]
        sample_indexes = range(len(chunk.sizes))
        if quick and len(sample_indexes) > 6:
            sample_indexes = {0, 1, 2, len(chunk.sizes)//2, len(chunk.sizes)-2, len(chunk.sizes)-1}
        off = base
        chunk_end = base + sum(chunk.sizes)
        window = memoryview(b'')
        window_start = base
        for i, n in enumerate(chunk.sizes):
            if i in sample_indexes:
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
                    return False
                if not valid_sample(raw, t.codec, t.nal_length):
                    return False
            off += n
        # Cached bytes still require the source-identity gate after validation.
        pool.check(sid)
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
            shutil.copyfileobj(r, w, length=4*1024*1024)
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
    with dest.open('xb') as output:
        def write(data: bytes) -> None:
            output.write(data)
            h.update(data)
        write(FTYP)
        write(moov)
        write(struct.pack('>I4sQ', 1, b'mdat', movie.media_size + 16))
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
            source = pool.sources[sid]
            entries.append({'track': c.track, 'chunk': c.index, 'samples': len(c.sizes),
                            'source_id': sid, 'source_path': source['path'], 'source_offset': off,
                            'source_sha256_at_scan': source['sha256'], 'size': c.size,
                            'output_offset': new_offsets[(c.track, c.index)],
                            'sha256': chunk_hash.hexdigest(),
                            'physical_extents':pool.provenance(sid,off,c.size)})
        output.flush()
        os.fsync(output.fileno())
    return {'sha256': h.hexdigest(), 'media_sha256': media_hash.hexdigest(), 'bytes': size,
            'chunks': entries, 'metadata_sha256': movie.digest,
            'note': 'Encoded samples are copied; container offsets are rebuilt. Identity and wall-clock chronology are not proven.'}


def attempt(case: Case, movie: Movie, mapping: Mapping, pool: ReaderPool, tools: dict,
            opt: Options, stage: str) -> dict:
    md = mapping_digest(movie, mapping)
    prev = case.db.execute('SELECT * FROM attempts WHERE movie_digest=? AND mapping_digest=?',
                           (movie.digest, md)).fetchone()
    if prev:
        old = json.loads(prev['detail'])
        same_tools = (not old.get('historical_validation') and old.get('tools') == tools and
                      old.get('timeout') == opt.timeout)
        if same_tools and old.get('status') == 'valid':
            p = case.root / old['path']
            if not p.is_file() or p.is_symlink() or file_sha256(p) != old['sha256']:
                raise SafetyError(f'Previously validated candidate changed or disappeared: {p}')
            return old
        if same_tools and old.get('status') == 'rejected':
            return old
    result = {'status': 'rejected', 'mapping_digest': md, 'stage': stage,
              'tools': tools, 'timeout': opt.timeout}
    temp = case.root / 'work' / f'{movie.digest}-{md}.mp4'
    target = case.root / 'candidates' / f'{movie.digest[:24]}-{md[:24]}.mp4'
    if temp.exists() or temp.is_symlink():
        if temp.is_symlink():
            raise SafetyError('Work artifact is a symlink')
        # This exact deterministic file is our interrupted temporary output.
        temp.unlink()
    try:
        validate_ranges(movie, mapping, pool)
        if not check_samples(movie, mapping, pool, quick=True):
            result['reason'] = 'NAL_sample_boundaries_do_not_match'
        elif not check_samples(movie, mapping, pool):
            result['reason'] = 'some_NAL_samples_do_not_match'
        else:
            proof = reconstruct(movie, mapping, pool, temp, opt.max_output_bytes)
            checked = validate(temp, movie, tools, opt.timeout)
            result['validation'] = checked
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
                            'movie': movie.summary(), 'provenance': proof, 'validation': checked})
            elif checked.get('probe', {}).get('timeout') or checked.get('decode', {}).get('timeout'):
                result['status'] = 'limited'
    except FormatError as e:
        result['reason'] = str(e)
    finally:
        if temp.exists():
            temp.unlink()
    case.db.execute('''INSERT INTO attempts(movie_digest,mapping_digest,stage,status,detail)
                    VALUES(?,?,?,?,?) ON CONFLICT(movie_digest,mapping_digest) DO UPDATE SET
                    stage=excluded.stage,status=excluded.status,detail=excluded.detail''',
                    (movie.digest, md, stage, result['status'], json_text(result)))
    case.db.commit()
    return result


def _publish(case: Case, movie: Movie, successes: list[dict], status: str) -> list[str]:
    """Published output is a case-internal hard link, never a source-file link."""
    final = case.root / 'reconstructed' / f'{movie.digest[:24]}.mp4'
    sidecar = final.with_suffix('.json')
    if final.is_symlink() or sidecar.is_symlink():
        raise SafetyError('Published artifact is a symlink')
    if final.exists():
        # Detach the old publication name; candidate bytes and history are retained.
        final.unlink()
    if sidecar.exists():
        sidecar.unlink()
    if status != 'matched' or len(successes) != 1:
        return []
    candidate = case.root / successes[0]['path']
    storage_mode = _publish_link_or_copy(candidate, final)
    write_json(sidecar, {'status': status, 'candidate': successes[0]['path'], 'storage_mode': storage_mode,
                         'sha256': successes[0]['sha256'],
                         'meaning': 'One byte-distinct candidate passed within the completed search model; not proof of historical identity.'})
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
        status = 'matched'
    else:
        status = 'unresolved'
    outputs = _publish(case, movie, good, status)
    detail = {'run_key': run_key, 'stage': stage, 'candidates': good, 'search_limited': limited,
              'outputs': outputs, 'scope': 'Uniqueness applies only to the documented, completed hypotheses searched.'}
    if extra:
        detail.update(extra)
    case.result(movie.digest, status, detail)
    LOG.info('%s %s: %d byte-distinct decode-checked candidates', movie.digest[:12], status, len(good))
    return status


def whole_payload(case: Case, movie: Movie, pool: ReaderPool, tools: dict,
                  opt: Options, run_key: str) -> tuple[str, list[dict], bool]:
    successes = []
    limited = False
    tested = set()
    # Test coherent original-relative layouts first, but still check other mdat
    # candidates so metadata duplicates with different media are not auto-chosen.
    origins = []
    locations = case.db.execute('''SELECT a.source_id,a.offset FROM atoms a JOIN sources s ON s.id=a.source_id
                                 WHERE a.movie_digest=? AND s.complete=1''', (movie.digest,)).fetchall()
    for location in locations:
        sid, moff = location['source_id'], location['offset']
        if pool.sources[sid]['kind'] in ('file','logical'):
            origins.append((sid, 0))
        # The nearest preceding ftyp is a location hypothesis, never proof.
        row = case.db.execute('SELECT offset FROM atoms WHERE source_id=? AND kind="ftyp" AND offset<=? ORDER BY offset DESC LIMIT 1',
                              (sid, moff)).fetchone()
        if row:
            origins.append((sid, row['offset']))
    for sid, origin in sorted(set(origins)):
        mapping = {(c.track, c.index): (sid, origin+c.offset) for c in movie.chunks}
        md = mapping_digest(movie, mapping)
        if md in tested:
            continue
        tested.add(md)
        r = attempt(case, movie, mapping, pool, tools, opt, 'original_offsets')
        if r['status'] == 'valid':
            successes.append(r)
        limited |= r['status'] == 'limited'
    first = movie.chunks[0].offset
    rows = case.db.execute('''SELECT a.* FROM atoms a JOIN sources s ON s.id=a.source_id
                    WHERE a.kind='mdat' AND s.complete=1 AND a.payload_size BETWEEN ? AND ?
                    ORDER BY a.payload_size,a.source_id,a.offset LIMIT ?''',
                    (movie.span, movie.span+opt.max_padding, opt.max_candidates+1)).fetchall()
    if len(rows) > opt.max_candidates:
        limited = True
        rows = rows[:opt.max_candidates]
    for i, r in enumerate(rows):
        sid = r['source_id']
        base = r['offset'] + r['header']
        mapping = {(c.track, c.index): (sid, base+c.offset-first) for c in movie.chunks}
        md = mapping_digest(movie, mapping)
        if md in tested:
            continue
        tested.add(md)
        a = attempt(case, movie, mapping, pool, tools, opt, 'whole_mdat')
        if a['status'] == 'valid':
            successes.append(a)
        limited |= a['status'] == 'limited'
        if (i+1) % 100 == 0:
            LOG.info('%s: tested %d/%d mdat candidates', movie.digest[:12], i+1, len(rows))
    status = finish(case, movie, successes, limited, 'whole_payload', run_key,
                    {'tested_hypotheses': len(tested), 'max_trailing_padding': opt.max_padding})
    return status, successes, limited


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


def deep_search(case: Case, movies: list[Movie], pool: ReaderPool, tools: dict,
                opt: Options, run_key: str) -> None:
    groups = {}
    per_movie = {}
    excluded = set()
    anchors = defaultdict(list)
    for movie in movies:
        gs = groups_for(movie)
        if len(groups) + len(gs) > opt.max_groups:
            excluded.add(movie.digest)
            case.result(movie.digest, 'search_limited', {'run_key': run_key,
                        'reason': 'global_group_budget', 'max_groups': opt.max_groups})
            continue
        if any(not g.anchors() for g in gs):
            case.result(movie.digest, 'unsupported', {'run_key': run_key,
                        'reason': 'deep search requires eligible 4-byte NAL-length samples in every extent'})
            excluded.add(movie.digest)
            continue
        per_movie[movie.digest] = gs
        for g in gs:
            groups[g.key] = g
            for length, relative in g.anchors():
                anchors[length].append((g.key, relative))
    if not groups:
        return
    # A sample may contain SEI/SPS/PPS plus one or more slices. Its first NAL
    # length need not equal sample_size-4. Search plausible first-length prefixes
    # and accumulate bounded NAL chains until an expected total size is reached.
    # This also finds multi-NAL keyframes in one-frame chunks.
    maximum_sample = max(anchors) + 4
    high = ((maximum_sample - 4) >> 16) & 255
    # Sparse images contain long zero-filled gaps. Reject zero NAL lengths in
    # the regex rather than entering Python once for every byte in those gaps.
    # Keep this zero-width so adjacent/overlapping length prefixes remain visible.
    pattern = re.compile(fr'(?=\x00[\x00-\x{high:02x}][\s\S]{{2}})(?!\x00{{4}})'.encode('ascii'))

    def totals(sid: int, block_base: int, data: bytes, start: int):
        nonlocal nal_checks, global_limited, stop_reason
        pos = start
        total = 0
        source_size = pool.sources[sid]['size']
        # Match the ordinary sample validator's NAL-count ceiling. A smaller
        # search-only ceiling silently loses otherwise eligible access units.
        for _ in range(65536):
            if nal_checks >= opt.max_anchor_checks:
                global_limited = True
                stop_reason = 'nal_check_budget'
                return
            nal_checks += 1
            if pos + 5 > source_size:
                return
            local = pos - block_base
            head_size = min(6, source_size-pos)
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
            total += n+4
            if total-4 in anchors:
                yield total-4
            pos += n+4
    hits = {key: {} for key in groups}
    visited = set()
    capped = set()
    checks = 0
    nal_checks = 0
    global_limited = False
    stop_reason = None
    last_log = time.monotonic()
    LOG.info('Deep search: %d extents, %d anchor sizes across %d sources', len(groups), len(anchors), len(pool.sources))
    for sid, source in pool.sources.items():
        pool.check(sid)
        for base, data, owned in pool.iter_blocks(sid,overlap=16):
            for match in pattern.finditer(data):
                at = match.start()
                if at >= owned or at+4 > len(data):
                    continue
                for length in totals(sid, base, data, base+at):
                    for key, relative in anchors.get(length, ()):
                        if key in capped:
                            continue
                        group = groups[key]
                        start = base + at - relative
                        identity = (key, sid, start)
                        if start < 0 or start + group.span > source['size'] or identity in visited:
                            continue
                        # Bounded by max_anchor_checks; not an unbounded image hit cache.
                        visited.add(identity)
                        checks += 1
                        if checks > opt.max_anchor_checks:
                            global_limited = True
                            stop_reason = 'anchor_check_budget'
                            break
                        mapping = group.mapping(sid, start)
                        if not check_samples(group.movie, mapping, pool, quick=True, chunks=group.chunks):
                            continue
                        if not check_samples(group.movie, mapping, pool, chunks=group.chunks):
                            continue
                        fingerprint = _group_hash(group, mapping, pool)
                        if fingerprint in hits[key]:
                            continue
                        if len(hits[key]) >= opt.max_hits:
                            capped.add(key)
                            continue
                        hits[key][fingerprint] = (sid, start)
                    if global_limited:
                        break
                if global_limited:
                    break
            if time.monotonic() - last_log > 5:
                LOG.info('Deep scan %s: %.1f%%; %d checks, %d located extents',
                         Path(source['path']).name, 100*min(base+owned, source['size'])/max(1, source['size']),
                         checks, sum(bool(h) for h in hits.values()))
                last_log = time.monotonic()
            if global_limited:
                break
        pool.check(sid)
        if global_limited:
            break
    for movie in movies:
        if movie.digest in excluded:
            continue
        gs = per_movie[movie.digest]
        counts = [len(hits[g.key]) for g in gs]
        limited = global_limited or any(g.key in capped for g in gs)
        details = {'extents': len(gs), 'located_extents': sum(n > 0 for n in counts),
                   'hit_counts': counts, 'anchor_checks': checks,
                   'nal_checks': nal_checks, 'stop_reason': stop_reason,
                   'model': 'Each video chunk plus adjacent audio remains contiguous; no within-extent sector permutation.'}
        if any(n == 0 for n in counts):
            finish(case, movie, [], limited, 'scattered_extents', run_key, details)
            continue
        combinations = math.prod(counts)
        limited |= combinations > opt.max_assemblies
        details['possible_assemblies'] = combinations
        successes = []
        tried = 0
        alternatives = [list(hits[g.key].values()) for g in gs]
        for choices in itertools.islice(itertools.product(*alternatives), opt.max_assemblies):
            mapping = {}
            for g, (sid, start) in zip(gs, choices):
                mapping.update(g.mapping(sid, start))
            r = attempt(case, movie, mapping, pool, tools, opt, 'scattered_extents')
            tried += 1
            if r['status'] == 'valid':
                successes.append(r)
            limited |= r['status'] == 'limited'
        details['tested_assemblies'] = tried
        finish(case, movie, successes, limited, 'scattered_extents', run_key, details)


def recover(case: Case, opt: Options, limit: int | None = None, retry: bool = False) -> None:
    opt.check()
    tools = doctor()
    sources = case.sources()
    if not sources:
        raise SafetyError('No completed source scans; run scan or run first')
    run_key = hashlib.sha256(json_text({'version': __version__, 'options': asdict(opt), 'tools': tools,
                         'sources': [(sid,s['sha256'],s['path'],s.get('extents'),s.get('binding'))
                                     for sid,s in sorted(sources.items())]}).encode()).hexdigest()
    case.event('recovery_started', {'run_key': run_key, 'tools': tools, 'options': asdict(opt)})
    deferred = []
    processed = 0
    # Do not load all moov BLOBs into memory. A separate cursor streams them.
    rows = case.db.execute('''SELECT m.*,r.status AS old_status,r.detail AS old_detail FROM movies m
          LEFT JOIN jobs r ON r.kind='ordinary' AND r.key=m.digest
          WHERE EXISTS(SELECT 1 FROM atoms a JOIN sources s ON a.source_id=s.id
                       WHERE a.movie_digest=m.digest AND s.complete=1) ORDER BY m.digest''')
    with ReaderPool(sources,physical_sources=case.physical_sources()) as pool:
        for sid in sources:
            pool.check(sid)
        for row in rows:
            old_detail = json.loads(row['old_detail']) if row['old_detail'] else {}
            if old_detail.get('run_key') == run_key:
                if row['old_status'] != 'awaiting_deep' and (not retry or row['old_status'] == 'matched'):
                    candidates = old_detail.get('candidates', [])
                    for c in candidates:
                        p = case.root / c['path']
                        if not p.is_file() or p.is_symlink() or file_sha256(p) != c['sha256']:
                            raise SafetyError(f'Previously validated candidate changed or disappeared: {p}')
                    for output in old_detail.get('outputs', []):
                        p = case.root / output
                        if (not p.is_file() or p.is_symlink() or len(candidates) != 1 or
                                file_sha256(p) != candidates[0]['sha256']):
                            raise SafetyError(f'Published output changed or disappeared: {p}')
                    continue
            if limit is not None and processed >= limit:
                break
            processed += 1
            if row['error']:
                case.result(row['digest'], 'unsupported', {'run_key': run_key, 'reason': row['error']})
                continue
            movie = parse_movie(row['data'])
            if movie.media_size > opt.max_output_bytes:
                case.result(movie.digest, 'search_limited', {'run_key': run_key, 'reason': 'output_size_budget'})
                continue
            LOG.info('Matching %s: %d chunks, %.2f MiB media', movie.digest[:12], len(movie.chunks), movie.media_size / 1024**2)
            status, _, _ = whole_payload(case, movie, pool, tools, opt, run_key)
            if opt.deep and status == 'unresolved':
                case.result(movie.digest, 'awaiting_deep', {'run_key': run_key,
                            'reason': 'whole-payload search finished; deep pass not yet completed'})
                deferred.append(movie)
                # Bound retained metadata as well as the later deep group index.
                if sum(len(m.data) for m in deferred) > 32 * 1024*1024:
                    deep_search(case, deferred, pool, tools, opt, run_key)
                    deferred.clear()
        if deferred:
            deep_search(case, deferred, pool, tools, opt, run_key)
    case.event('recovery_complete', {'run_key': run_key, 'processed_movies': processed})
