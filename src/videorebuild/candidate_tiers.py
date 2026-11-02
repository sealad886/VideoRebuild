"""Derived first-tier references; accepted physical bytes remain in second tier.

Publications and their canonical job evidence are authoritative. Normalized
references are an inspectable derived cache, never proof of missing outputs.
"""
from bisect import bisect_right
from itertools import takewhile
import json
import hashlib
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


class _IntervalNode:
    def __init__(self, key):
        self.key = key
        self.priority = hashlib.blake2b(repr(key).encode(), digest_size=16).digest()
        self.left = self.right = None
        self.maximum = key[1]
        self.sole_owner = key[2]

    def update(self):
        self.maximum = max(self.key[1], self.left.maximum if self.left else 0,
                           self.right.maximum if self.right else 0)
        owner = self.key[2]
        self.sole_owner = owner if all(node is None or node.sole_owner == owner
                                      for node in (self.left, self.right)) else None


def _join_intervals(left, right):
    if left is None:
        return right
    if right is None:
        return left
    if left.priority > right.priority:
        left.right = _join_intervals(left.right, right)
        left.update()
        return left
    right.left = _join_intervals(left, right.left)
    right.update()
    return right


class _IntervalIndex:
    """Owner-labelled interval treap; updates have expected logarithmic cost.

    Keeping owners on individual intervals preserves overlapping claims. Queries
    prune subtrees outside the request or containing only the exempt owner.
    """
    def __init__(self, stats):
        self.root = None
        self.stats = stats

    def change(self, key, remove=False):
        def apply(node):
            if node is None:
                return None if remove else _IntervalNode(key)
            if node.key == key:
                return _join_intervals(node.left, node.right) if remove else node
            if key < node.key:
                node.left = apply(node.left)
                if node.left is not None and node.left.priority > node.priority:
                    top = node.left
                    node.left, top.right = top.right, node
                    node.update()
                    top.update()
                    return top
            else:
                node.right = apply(node.right)
                if node.right is not None and node.right.priority > node.priority:
                    top = node.right
                    node.right, top.left = top.left, node
                    node.update()
                    top.update()
                    return top
            node.update()
            return node
        self.root = apply(self.root)
        self.stats['interval_updates'] += 1

    def overlaps(self, start, end, owner, *, merge=True):
        def visit(node):
            if node is None or node.maximum <= start or (owner is not None and node.sole_owner == owner):
                return
            self.stats['interval_nodes_visited'] += 1
            yield from visit(node.left)
            lo, hi, claimed = node.key
            if lo >= end:
                return
            if hi > start and claimed != owner:
                yield max(lo, start), min(hi, end)
            yield from visit(node.right)
        self.stats['interval_queries'] += 1
        if not merge:
            yield from visit(self.root)
            return
        # Union returned overlaps incrementally, without rebuilding an owner's
        # entire physical-source map or retaining all hits in memory.
        pending = None
        for lo, hi in visit(self.root):
            if pending is not None and lo <= pending[1]:
                pending = pending[0], max(hi, pending[1])
            else:
                if pending is not None:
                    yield pending
                pending = lo, hi
        if pending is not None:
            yield pending


class CandidateTiers:
    def __init__(self, case, pool):
        self.case, self.pool = case, pool
        self.generation = 0
        self._verified = {}
        self._jobs = {}
        self._intervals = {}
        self._owner_intervals = {}
        self._record_sources = {}
        self.stats = dict(interval_updates=0, interval_queries=0, interval_nodes_visited=0,
                          owners_updated=0, jobs_checked=0)
        self._manifests = {}
        self._initialized = False
        self.refresh()

    @property
    def records(self):
        """Materialize diagnostic evidence only when explicitly requested."""
        return sorted((record for _, saved, _ in self._jobs.values() for record in saved),
                      key=lambda item: (item['owner'], item['source_id'], item['offset'], item['length']))

    def _update_owner(self, owner, records):
        for physical, start, end in self._owner_intervals.pop(owner, []):
            self._intervals[physical].change((start, end, owner), remove=True)
        grouped = {}
        for record in records:
            physical = self.pool.physical_key(record['source_id'])
            grouped.setdefault(physical, []).append((record['offset'], record['offset'] + record['length']))
        added = []
        for physical, spans in grouped.items():
            index = self._intervals.setdefault(physical, _IntervalIndex(self.stats))
            for start, end in _merge(spans):
                index.change((start, end, owner))
                added.append((physical, start, end))
        if added:
            self._owner_intervals[owner] = added
        self.stats['owners_updated'] += 1

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

    def refresh(self, owner=None):
        """Verify publications and update derived references.

        With the exclusive case writer, a publisher can name its changed job.
        Other accepted publications still receive fresh integrity checks; empty
        catalogue jobs need not be rescanned after every publication. A full
        refresh (startup and external callers) discovers every job change.
        """
        self._checked_sources = set()
        current = {}
        changed = {}
        # Read small update tokens first. Unchanged metadata and physical
        # records stay in memory; publications still get fresh stat checks.
        if owner is None:
            jobs = list(self.case.db.execute('SELECT kind,key,updated FROM jobs ORDER BY kind,key'))
        else:
            jobs = [(kind, key, cached[0]) for (kind, key), cached in self._jobs.items()
                    if (kind, key) != owner]
            updated = self.case.db.execute('SELECT updated FROM jobs WHERE kind=? AND key=?', owner).fetchone()
            if updated is not None:
                jobs.append((*owner, updated[0]))
        self.stats['jobs_checked'] += len(jobs)
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
                for sid in self._record_sources.get(owner, ()):
                    if sid not in self._checked_sources:
                        self.pool._check_identity(self.pool.physical[sid])
                        self._checked_sources.add(sid)
            else:
                job = self.case.job(kind, key)
                saved, verified = self._job_records(job)
                self._record_sources[owner] = {record['source_id'] for record in saved}
                if (cached is None and saved) or (cached is not None and cached[1] != saved):
                    changed[owner] = saved
            for output, digest in verified:
                self._verify(output, digest)
            current[owner] = (updated, saved, verified)
        for owner in self._jobs.keys() - current.keys():
            if self._jobs[owner][1]:
                changed[owner] = []
            self._manifests.pop(owner, None)
            self._record_sources.pop(owner, None)
        if changed or not self._initialized:
            # Rebuild this cache from verified publications at startup. Preserve
            # the old derived JSON before its one-time normalized conversion.
            with self.case.db:
                self.case.db.execute('''CREATE TABLE IF NOT EXISTS candidate_references(
                    kind TEXT NOT NULL, job_key TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    reference TEXT NOT NULL, PRIMARY KEY(kind,job_key,ordinal))''')
                if not self._initialized:
                    self.case.db.execute("INSERT OR IGNORE INTO settings(name,value) SELECT 'candidate_tiers_before_indexed_matching',value FROM settings WHERE name='candidate_tiers'")
                    self.case.db.execute("DELETE FROM settings WHERE name='candidate_tiers'")
                    self.case.db.execute('DELETE FROM candidate_references')
                for owner, records in changed.items():
                    self.case.db.execute('DELETE FROM candidate_references WHERE kind=? AND job_key=?', owner)
                    self.case.db.executemany('INSERT INTO candidate_references VALUES(?,?,?,?)',
                        ((*owner, i, json_text(record)) for i, record in enumerate(records)))
            for owner, records in changed.items():
                self._update_owner(owner, records)
            self.generation += 1
            self._initialized = True
        self._jobs = {key: cached for key, cached in current.items() if cached[1] or cached[2]}
        for key in current.keys() - self._jobs.keys():
            self._record_sources.pop(key, None)

    @property
    def has_recovered(self):
        return bool(self._owner_intervals)

    def view(self, owner=None):
        return _TierView(self, owner)


class _TierView:
    def __init__(self, tiers, owner):
        self.tiers, self.owner = tiers, owner

    @property
    def discovery_pool(self):
        """Immutable eligible base; tier ownership is applied at candidate use."""
        return self.tiers.pool

    def __getattr__(self, name):
        return getattr(self.tiers.pool, name)

    def _blocked(self, sid, offset, length, *, candidate_only=False):
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
            index = self.tiers._intervals.get(self.tiers.pool.physical_key(part.source_id))
            if index is None:
                continue
            start, end = part.source_offset, part.source_offset + part.length
            for lo, hi in index.overlaps(start, end, self.owner, merge=not candidate_only):
                if hi > lo:
                    yield part.logical_offset + lo - start, hi - lo

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
