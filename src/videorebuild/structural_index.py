"""Durable, coverage-qualified structural postings on the case output volume.

Postings describe available encoded bytes, never recovery ownership. Tier/owner
policy is applied by consumers. Interrupted sources have no completion receipt;
their partial postings may be retained, but never establish a negative match.
"""
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
import sqlite3

from .storage import SafetyError, json_text

PARSER_REVISION = 4  # Boundary-safe headers, normalized coverage and posting counts.


def _plain(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _identity(pool):
    sources = []
    for sid, row in sorted(pool.sources.items()):
        pool.check(sid)
        sources.append([sid, {k: row.get(k) for k in
                        ('path', 'kind', 'size', 'mtime_ns', 'ino', 'sha256',
                         'hash_scope', 'extents', 'binding')},
                        list(pool.available_ranges(sid))])
    physical = [[sid, {k: row.get(k) for k in
                      ('path', 'size', 'mtime_ns', 'ino', 'sha256', 'binding')}]
                for sid, row in sorted(getattr(pool, 'physical', {}).items())]
    mappings = [[sid, _plain(extents)] for sid, extents in sorted(getattr(pool, 'extents', {}).items())]
    acquisition = [[sid, rescue.image_offset, _plain(bound),
                    [_plain(span) for span in rescue.spans(0, pool.physical[sid]['size'])]]
                   for sid, (rescue, bound) in sorted(getattr(pool, 'maps', {}).items())]
    return json_text(_plain({'parser_revision': PARSER_REVISION, 'sources': sources,
                      'physical': physical,
                      'mappings': mappings, 'acquisition': acquisition,
                      'allocation': getattr(getattr(pool, 'allocation_scope', None), 'digest', None)}))


def _scope_identity(pool):
    """One immutable source/mapping scope per pinned reader lifetime."""
    value = getattr(pool, '_matching_scope_identity', None)
    if value is None:
        value = pool._matching_scope_identity = _identity(pool)
    return value


def _directory(case):
    current = Path(case.root)
    for component in ('work', 'structural-index'):
        current = current / component
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise SafetyError(f'Structural index path must be a real directory: {current}')
        current.mkdir(mode=0o700, exist_ok=True)
    return current



def _common_coverage(profiles, receipts, source_ids):
    signatures = {tuple(sorted(receipts.get(sid, ()))) for sid in source_ids}
    common = None
    for signature in signatures:
        covered = frozenset().union(*(profiles[key] for key in signature))
        common = covered if common is None else common & covered
    return common or frozenset()


def _session(case, pool, *, existing_only=False):
    sessions = getattr(pool, '_structural_sessions', None)
    if sessions is None:
        sessions = pool._structural_sessions = {}
    root = str(case.root)
    if root in sessions:
        return sessions[root]
    identity = _scope_identity(pool)
    key = hashlib.sha256(identity.encode()).hexdigest()
    if existing_only:
        directory = Path(case.root)/'work'/'structural-index'
        if directory.is_symlink():
            raise SafetyError('Structural index directory must not be a symlink')
        path = directory/(key+'.sqlite3')
        if not path.exists():
            return None
    else:
        path = _directory(case)/(key+'.sqlite3')
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise SafetyError('Structural index must be a private regular file')
    db = sqlite3.connect(path)
    try:
        db.execute('PRAGMA temp_store=MEMORY')
        db.executescript("""
            CREATE TABLE IF NOT EXISTS identity(value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS targets(id TEXT PRIMARY KEY,lengths TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS coverage(sid INTEGER,target TEXT,PRIMARY KEY(sid,target)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS locations(length INTEGER,sid INTEGER,start INTEGER,
                PRIMARY KEY(length,sid,start)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS frequencies(length INTEGER PRIMARY KEY,total INTEGER NOT NULL) WITHOUT ROWID;
        """)
        saved = db.execute('SELECT value FROM identity').fetchone()
        if saved is None:
            db.execute('INSERT INTO identity VALUES(?)', (identity,))
            db.commit()
        elif saved[0] != identity:
            raise SafetyError('Structural index identity does not match its source scope')
        profiles = {key: frozenset(json.loads(value)) for key, value in db.execute('SELECT id,lengths FROM targets')}
        receipts = {}
        for sid, target in db.execute('SELECT sid,target FROM coverage'):
            receipts.setdefault(sid, set()).add(target)
        session = dict(db=db, path=path, profiles=profiles, receipts=receipts,
                       requests={}, complete_requests=set(),
                       common=_common_coverage(profiles, receipts, pool.sources))
        sessions[root] = session
        pool._matching_contexts.callback(db.close)
        return session
    except BaseException:
        db.close()
        raise


@contextmanager
def postings(case, pool, lengths, scan, opt, task):
    """Completed source/profile receipts, shared once per pinned reader pass."""
    session = _session(case, pool)
    db, path = session['db'], session['path']
    request = session['requests'].get(id(lengths))
    if request is None or request[0] is not lengths:
        wanted_lengths = frozenset(lengths)
        encoded = json_text(sorted(wanted_lengths))
        target = hashlib.sha256(encoded.encode()).hexdigest()
        session['requests'][id(lengths)] = (lengths, wanted_lengths, target)
    else:
        _, wanted_lengths, target = request
    profiles, receipts = session['profiles'], session['receipts']
    if target not in profiles:
        db.execute('INSERT OR IGNORE INTO targets VALUES(?,?)', (target, json_text(sorted(wanted_lengths))))
        db.commit()
        profiles[target] = wanted_lengths
    tasks = []
    stats = dict(nal_checks=0, anchor_checks=0, limited=False, stop_reason=None,
                 locations=0, discovery_sources_scanned=0, discovery_sources_reused=0,
                 discovery_header_reads=0, discovery_header_cache_hits=0,
                 discovery_locations_checked=0)
    if target in session['complete_requests'] or wanted_lengths.issubset(session['common']):
        stats['discovery_sources_reused'] = len(pool.sources)
    else:
        missing_profiles = {}
        for sid in pool.sources:
            signature = tuple(sorted(receipts.get(sid, ())))
            if signature not in missing_profiles:
                covered = frozenset().union(*(profiles[key] for key in signature))
                missing_profiles[signature] = tuple(sorted(wanted_lengths-covered))
            missing = missing_profiles[signature]
            if not missing:
                stats['discovery_sources_reused'] += 1
            else:
                tasks.append((sid, missing))
    workers = getattr(opt, 'search_workers', 1)
    stats['search_workers'] = min(workers, len(tasks))
    needed_bytes = sum(count for sid, _ in tasks for _, count in pool.available_ranges(sid))
    task.update(total=needed_bytes, reused_sources=stats['discovery_sources_reused'])
    try:
        def serial_events():
            class SourceTask:
                def __init__(self):
                    self.completed = 0
                def update(self, completed=None, **counts):
                    if completed is not None:
                        task.advance(completed-self.completed, **counts)
                        self.completed = completed
                    else:
                        task.heartbeat(**counts)
                def heartbeat(self, **counts):
                    task.heartbeat(**counts)
            predicates = {}
            for sid, missing in tasks:
                if id(missing) not in predicates:
                    maximum = missing[-1]
                    bits = bytearray((maximum >> 3) + 1)
                    for length in missing:
                        bits[length >> 3] |= 1 << (length & 7)
                    predicates[id(missing)] = (missing, bits)
                bits = predicates[id(missing)][1]
                wanted = lambda length: bool(bits[length >> 3] & (1 << (length & 7)))
                local = dict(nal_checks=0, anchor_checks=0, limited=False, stop_reason=None)
                batch = []
                for location in scan(pool, missing[-1]+4, wanted, opt, local, SourceTask(), source_ids=(sid,)):
                    batch.append(location)
                    if len(batch) == 4096:
                        yield sid, tuple(batch), None
                        batch.clear()
                if batch:
                    yield sid, tuple(batch), None
                yield sid, None, local

        if workers > 1 and tasks:
            from .parallel_sources import stream_source_scans
            events = stream_source_scans(pool, tasks, scan, opt, task, workers)
        else:
            events = serial_events()
        try:
            for sid, batch, final in events:
                if batch is not None:
                    frequencies = {}
                    for source, start, length in batch:
                        stats['discovery_locations_checked'] += 1
                        inserted = db.execute('INSERT OR IGNORE INTO locations VALUES(?,?,?)',
                                              (length, source, start)).rowcount
                        stats['locations'] += inserted
                        frequencies[length] = frequencies.get(length, 0)+inserted
                    db.executemany('INSERT INTO frequencies VALUES(?,?) ON CONFLICT(length) DO UPDATE SET total=total+excluded.total',
                                   ((length, count) for length, count in frequencies.items() if count))
                    db.commit()
                    continue
                if final['limited']:
                    raise SafetyError('Incomplete capped discovery cannot establish structural index coverage')
                pool.check(sid)
                for counter in ('nal_checks', 'discovery_header_reads', 'discovery_header_cache_hits'):
                    stats[counter] += final.get(counter, 0)
                stats['discovery_sources_scanned'] += 1
                # Receipt refers to one shared target set; no S x L catalogue.
                db.execute('INSERT OR IGNORE INTO coverage VALUES(?,?)', (sid, target))
                db.commit()
                receipts.setdefault(sid, set()).add(target)
        finally:
            events.close()
        if tasks:
            session['common'] = _common_coverage(profiles, receipts, pool.sources)
        session['complete_requests'].add(target)
        if not getattr(pool, '_recovery_active', False):
            for sid in pool.sources:
                pool.check(sid)
        stats['discovery_index'] = str(path.relative_to(case.root))
        task.heartbeat(scanned_sources=stats['discovery_sources_scanned'], reused_sources=stats['discovery_sources_reused'])
        yield db, stats
    except sqlite3.DatabaseError as error:
        db.rollback()
        raise SafetyError(f'Structural index database is invalid: {error}') from error
    except BaseException:
        db.rollback()
        raise
