"""Derived first-tier references; accepted physical bytes remain in second tier.

Publications and their canonical job evidence are authoritative. The settings
entry is an inspectable derived cache, never a reason to trust missing outputs.
"""
from bisect import bisect_right
from itertools import takewhile
import json
from pathlib import Path

from .ranges import Extent
from .storage import ReaderPool, SafetyError, file_sha256, json_text


def _merge(values):
    result = []
    for start, end in sorted(values):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


class CandidateTiers:
    def __init__(self, case, pool):
        self.case, self.pool = case, pool
        self.records = []
        self.generation = 0
        self._verified = {}
        self._jobs = {}
        self._indexes = {}
        self._manifests = {}
        self.refresh()

    def _path(self, name):
        path = self.case.root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(self.case.root):
            raise SafetyError(f'Missing or unsafe accepted publication: {path}')
        return path

    def _verify(self, name, digest):
        path = self._path(name)
        s = path.stat()
        stamp = (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        identity = (str(path), digest)
        if self._verified.get(identity) != stamp:
            if not isinstance(digest, str) or file_sha256(path) != digest:
                raise SafetyError(f'Changed accepted publication: {path}')
            after = path.stat()
            if stamp != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise SafetyError(f'Accepted publication changed while hashing: {path}')
            self._verified[identity] = stamp

    def _extent(self, owner, part):
        sid = part['source_id']
        row = self.pool.physical.get(sid)
        if row is None or part.get('source_path', row['path']) != row['path']:
            raise SafetyError('Accepted physical extent source is unavailable')
        if sid not in self._checked_sources:
            self.pool._check_identity(row)
            self._checked_sources.add(sid)
        offset, length = part['source_offset'], part['length']
        if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0 or offset + length > row['size']:
            raise SafetyError('Invalid accepted physical extent')
        supplied = part.get('source_identity')
        identity = {key: row[key] for key in ('size', 'mtime_ns', 'dev', 'ino')}
        if supplied is not None and any(supplied.get(key) != value for key, value in identity.items()):
            raise SafetyError('Accepted physical extent identity changed')
        return {'owner': list(owner), 'source_id': sid, 'source_identity': identity,
                'offset': offset, 'length': length}

    def _job_records(self, job):
        detail = job['detail']
        outputs = detail.get('outputs', [])
        records, verified = [], []
        owner = (job['kind'], job['key'])
        self._manifests.pop(owner, None)
        if detail.get('historical_validation'):
            return records, verified
        if job['kind'] == 'ordinary':
            publications = list(detail.get('historical_publications', []))
            candidates = detail.get('candidates', [])
            if outputs and job['status'] in {'matched', 'reconstructed'} and len(candidates) == 1:
                candidate = candidates[0]
                publications.extend({'path': output, 'sha256': candidate['sha256'],
                                     'candidate': candidate['path']} for output in outputs)
            ingested = set()
            for publication in publications:
                verified.append((publication['path'], publication['sha256']))
                candidate_path = publication['candidate']
                verified.append((candidate_path, publication['sha256']))
                marker = (candidate_path, publication['sha256'])
                if marker in ingested:
                    continue
                ingested.add(marker)
                manifest_path = self._path(str(Path(candidate_path).with_suffix('.json')))
                manifest = json.loads(manifest_path.read_text())
                state = manifest_path.stat()
                self._manifests.setdefault(owner, []).append((manifest_path,
                    (state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns)))
                for chunk in manifest.get('provenance', {}).get('chunks', []):
                    for part in chunk.get('physical_extents', []):
                        records.append(self._extent(owner, part))
        elif job['kind'] == 'fragmented':
            evidence = {entry['path']: entry for entry in detail.get('output_evidence', [])}
            for segment in detail.get('segments', []):
                output = segment.get('output')
                if output not in outputs:
                    continue
                if output not in evidence:
                    raise SafetyError('Missing accepted fragmented output evidence')
                verified.append((output, evidence[output]['sha256']))
                if evidence[output].get('manifest_path'):
                    verified.append((evidence[output]['manifest_path'], evidence[output]['manifest_sha256']))
                for part in segment.get('source_ranges', []):
                    records.append(self._extent(owner, part))
        return records, verified

    def refresh(self):
        records = []
        self._checked_sources = set()
        current = {}
        # Read small update tokens first. Unchanged metadata and physical
        # records stay in memory; publications still get fresh stat checks.
        jobs = list(self.case.db.execute('SELECT kind,key,updated FROM jobs ORDER BY kind,key'))
        for kind, key, updated in jobs:
            owner = (kind, key)
            cached = self._jobs.get(owner)
            if cached is not None and cached[0] == updated:
                _, saved, verified = cached
                for manifest, expected in self._manifests.get(owner, []):
                    state = manifest.stat()
                    stamp = (state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns)
                    if manifest.is_symlink() or stamp != expected:
                        raise SafetyError(f'Changed accepted reconstruction evidence: {manifest}')
                for record in saved:
                    sid = record['source_id']
                    if sid not in self._checked_sources:
                        self.pool._check_identity(self.pool.physical[sid])
                        self._checked_sources.add(sid)
            else:
                job = self.case.job(kind, key)
                saved, verified = self._job_records(job)
            for output, digest in verified:
                self._verify(output, digest)
            records.extend(saved)
            current[owner] = (updated, saved, verified)
        records.sort(key=lambda item: (item['owner'], item['source_id'], item['offset'], item['length']))
        if records != self.records:
            value = json_text({'revision': 1, 'references': records})
            self.case.db.execute("INSERT INTO settings(name,value) VALUES('candidate_tiers',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (value,))
            self.case.db.commit()
            self.records = records
            self.generation += 1
            self._indexes.clear()
        elif not self._jobs:
            # Empty initial state also invalidates any stale persisted cache.
            self.case.db.execute("INSERT INTO settings(name,value) VALUES('candidate_tiers',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (json_text({'revision': 1, 'references': records}),))
            self.case.db.commit()
        self._jobs = current

    @property
    def has_recovered(self):
        return bool(self.records)

    def view(self, owner=None):
        return _TierView(self, owner)


class _TierView:
    def __init__(self, tiers, owner):
        self.tiers, self.owner = tiers, owner
        self._generation = -1
        self._intervals = {}

    def __getattr__(self, name):
        return getattr(self.tiers.pool, name)

    def _index(self):
        if self._generation != self.tiers.generation:
            shared = self.tiers._indexes.get(self.owner)
            if shared is not None:
                self._intervals = shared
                self._generation = self.tiers.generation
                return shared
            values = {}
            for record in self.tiers.records:
                if self.owner is not None and tuple(record['owner']) == self.owner:
                    continue
                key = self.tiers.pool.physical_key(record['source_id'])
                values.setdefault(key, []).append((record['offset'], record['offset'] + record['length']))
            self._intervals = {key: _merge(spans) for key, spans in values.items()}
            self.tiers._indexes[self.owner] = self._intervals
            self._generation = self.tiers.generation
        return self._intervals

    def _blocked(self, sid, offset, length, *, candidate_only=False):
        intervals = self._index()
        if candidate_only:
            # A bounding candidate can span unavailable holes between actual
            # chunks. Tier preference tests physical intersections only; base
            # allocation/rescue requirements still gate every actual read.
            if sid in self.extents:
                extents = self.extents[sid]
                index = max(0, bisect_right(extents, offset, key=lambda e: e.logical_offset) - 1)
                logical_end = offset + length
                overlaps = takewhile(lambda e: e.logical_offset < logical_end,
                                     (extents[i] for i in range(index, len(extents))))
                parts = (Extent(max(offset, e.logical_offset), e.source_id,
                                e.source_offset + max(offset, e.logical_offset) - e.logical_offset,
                                min(logical_end, e.logical_offset + e.length) - max(offset, e.logical_offset))
                         for e in overlaps if e.logical_offset + e.length > offset)
            else:
                parts = (Extent(offset, sid, offset, length),) if length else ()
        else:
            parts = self.tiers.pool._ranges(sid, offset, length)
        for part in parts:
            values = intervals.get(self.tiers.pool.physical_key(part.source_id), [])
            start, end = part.source_offset, part.source_offset + part.length
            index = max(0, bisect_right(values, start, key=lambda item: item[0]) - 1)
            while index < len(values) and values[index][0] < end:
                lo, hi = max(start, values[index][0]), min(end, values[index][1])
                if hi > lo:
                    yield part.logical_offset + lo - start, hi - lo
                index += 1

    def candidate_permits(self, sid, offset, length):
        if sid not in self.sources or offset < 0 or length < 0 or offset + length > self.sources[sid]['size']:
            return False
        self.tiers.pool.check(sid)
        return next(self._blocked(sid, offset, length, candidate_only=True), None) is None

    def permits(self, sid, offset, length):
        if not self.tiers.pool.permits(sid, offset, length):
            return False
        return next(self._blocked(sid, offset, length), None) is None

    def available_ranges(self, sid, start=0, end=None):
        for offset, length in self.tiers.pool.available_ranges(sid, start, end):
            cursor = offset
            for blocked, count in self._blocked(sid, offset, length):
                if blocked > cursor:
                    yield cursor, blocked - cursor
                cursor = max(cursor, blocked + count)
            if cursor < offset + length:
                yield cursor, offset + length - cursor

    def iter_blocks(self, sid, overlap=16):
        yield from ReaderPool.iter_blocks(self, sid, overlap)
