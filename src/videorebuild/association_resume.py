"""Atomic, scope-qualified checkpoints for certified deep associations.

Only completed association pages are durable. Assembly pruning is an ephemeral
overlay; it never removes a physical witness from this checkpoint.
"""
from contextlib import contextmanager
import hashlib
import logging
import math
import os
from pathlib import Path
import sqlite3
import time

from .sample_certificates import (CERTIFICATE_REVISION, SampleCertificates,
                                  require_search_space, search_disk_full)
from .storage import ReaderPool, SafetyError, json_text
from .structural_index import _scope_identity
from .validate import VALIDATION_REVISION

LOG = logging.getLogger(__name__)
CHECKPOINT_REVISION = 1
DEFAULT_CHECKPOINT_SECONDS = 60.0
CHECKPOINT_SECONDS_ENV = 'VIDEOREBUILD_ASSOCIATION_CHECKPOINT_SECONDS'
MAX_PENDING_HITS = 250_000
SPACE_CHECK_ROWS = 4096
SPACE_CHECK_SECONDS = 1.0


def _checkpoint_seconds():
    try:
        value = float(os.environ.get(CHECKPOINT_SECONDS_ENV, str(DEFAULT_CHECKPOINT_SECONDS)))
    except ValueError as error:
        raise SafetyError(f'{CHECKPOINT_SECONDS_ENV} must be a finite positive number of seconds') from error
    if not math.isfinite(value) or value <= 0:
        raise SafetyError(f'{CHECKPOINT_SECONDS_ENV} must be a finite positive number of seconds')
    return value


def _private(path):
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise SafetyError('Deep association checkpoint must be a private regular file')


def _identity(pool, run_key):
    base = getattr(pool, 'discovery_pool', pool)
    tiers = getattr(pool, 'tiers', None)
    ownership = (sorted((list(owner), sorted(parts)) for owner, parts in tiers._owner_intervals.items())
                 if tiers is not None else None)
    return hashlib.sha256(json_text([
        CHECKPOINT_REVISION, CERTIFICATE_REVISION, VALIDATION_REVISION, run_key,
        _scope_identity(base), base.matching_source_stamps(),
        getattr(pool, 'owner', None), ownership,
    ]).encode()).hexdigest()


def association_geometry(group, profile, relative):
    """The certified predicate and ordered reads, independent of batch membership."""
    return (profile[0], profile[1].hex(), profile[2], relative, group.span,
            tuple((chunk.offset-group.start, chunk.size) for chunk in group.chunks))


def domain_key(group, profile, relative):
    return hashlib.sha256(json_text(association_geometry(group, profile, relative)).encode()).hexdigest()


class AssociationCheckpoint:
    def __init__(self, case, pool, run_key):
        self.interval = _checkpoint_seconds()
        self.pool = pool
        work = Path(case.root) / 'work'
        if work.is_symlink() or (work.exists() and not work.is_dir()):
            raise SafetyError('Deep association work path must be a real directory')
        work.mkdir(mode=0o700, exist_ok=True)
        self.path = work / 'association-checkpoint.sqlite3'
        for suffix in ('', '-journal', '-wal', '-shm'):
            _private(self.path.with_name(self.path.name+suffix))
        identity = _identity(pool, run_key)
        require_search_space(work)
        self._connect()
        self.states = {}
        self.changed = set()
        self.pending_hits = self.space_check_rows = 0
        self.last_commit = self.last_space_check = time.monotonic()
        try:
            self._initialize(identity)
        except BaseException:
            self.db.close()
            raise
        LOG.info('Deep association checkpoint policy: interval=%.3gs, pending_hit_limit=%d; '
                 'single in-place database, no historical snapshots', self.interval, MAX_PENDING_HITS)

    def _connect(self):
        self.db = sqlite3.connect(self.path)
        try:
            self.db.execute('PRAGMA temp_store=MEMORY')
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA cache_size=-8192')
            state = self.path.stat()
            self.file_identity = state.st_dev, state.st_ino
        except BaseException:
            self.db.close()
            raise

    def _remove(self):
        for suffix in ('', '-journal', '-wal', '-shm'):
            _private(self.path.with_name(self.path.name+suffix))
        state = self.path.stat()
        if (state.st_dev, state.st_ino) != self.file_identity:
            raise SafetyError('Deep association checkpoint changed before cleanup')
        self.path.unlink()
        for suffix in ('-journal', '-wal', '-shm'):
            self.path.with_name(self.path.name+suffix).unlink(missing_ok=True)

    def _initialize(self, identity):
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if not tables:
            self.db.execute('BEGIN')
            with self.db:
                self.db.execute('CREATE TABLE identity(value TEXT NOT NULL)')
                self.db.execute('''CREATE TABLE progress(domain TEXT PRIMARY KEY,
                    sid INTEGER NOT NULL,start INTEGER NOT NULL,processed INTEGER NOT NULL,
                    checks INTEGER NOT NULL,hits INTEGER NOT NULL,complete INTEGER NOT NULL
                    CHECK(complete IN (0,1))) WITHOUT ROWID''')
                self.db.execute('''CREATE TABLE hit_locations(domain TEXT,fingerprint TEXT,sid INTEGER,start INTEGER,
                    PRIMARY KEY(domain,sid,start)) WITHOUT ROWID''')
                self.db.execute('CREATE INDEX hit_order ON hit_locations(domain,fingerprint,sid,start)')
                self.db.execute('INSERT INTO identity VALUES(?)', (identity,))
                self.db.execute(f'PRAGMA user_version={CHECKPOINT_REVISION}')
            return
        if (tables != {'identity', 'progress', 'hit_locations'} or
                self.db.execute('PRAGMA user_version').fetchone()[0] != CHECKPOINT_REVISION):
            raise SafetyError('Deep association checkpoint schema is invalid')
        saved = self.db.execute('SELECT value FROM identity LIMIT 2').fetchall()
        if len(saved) != 1:
            raise SafetyError('Deep association checkpoint identity is invalid')
        if saved != [(identity,)]:
            # Stale derived work cannot be reused. Closing and removing its
            # private file avoids a second, source-sized rollback journal.
            self.db.close()
            self._remove()
            self._connect()
            self._initialize(identity)
            LOG.info('Deep association checkpoint invalidated: source, options or eligibility scope changed')

    def register(self, domain):
        if domain not in self.states:
            row = self.db.execute('SELECT sid,start,processed,checks,hits,complete FROM progress WHERE domain=?',
                                  (domain,)).fetchone()
            state = list(row) if row is not None else [0, -1, 0, 0, 0, 0]
            if (any(type(value) is not int for value in state) or state[0] < 0 or
                    state[1] < -1 or not 0 <= state[4] <= state[3] <= state[2] or
                    state[5] not in (0, 1) or bool(state[2]) != (state[1] >= 0)):
                raise SafetyError('Deep association checkpoint cursor is invalid')
            if row is not None and self.db.execute(
                    'SELECT count(*) FROM hit_locations WHERE domain=?', (domain,)).fetchone()[0] != state[4]:
                raise SafetyError('Deep association checkpoint hits do not match its cursor')
            self.states[domain] = state
        return self.states[domain]

    def add_hit(self, domain, fingerprint, sid, start):
        self.db.execute('INSERT INTO hit_locations VALUES(?,?,?,?)', (domain, fingerprint, sid, start))
        self.pending_hits += 1

    def advance(self, domain, sid, start, checks, hits):
        # Called only after the yielded association body returned normally.
        # A signal anywhere in this page rolls the transaction back.
        state = self.states[domain]
        if state[2] and (sid, start) <= tuple(state[:2]):
            raise SafetyError('Deep association checkpoint cursor did not advance')
        state[:] = [sid, start, state[2]+1, state[3]+checks, state[4]+hits, 0]
        self.changed.add(domain)
        self.space_check_rows += 1
        now = time.monotonic()
        if (self.space_check_rows >= SPACE_CHECK_ROWS or
                now-self.last_space_check >= SPACE_CHECK_SECONDS):
            require_search_space(self.path.parent)
            self.space_check_rows = 0
            self.last_space_check = now
        # Bound actual transaction metadata, not source bytes hashed or rejected
        # locations. Large reads and zero-hit scans must not force rapid saves.
        if self.pending_hits >= MAX_PENDING_HITS or now-self.last_commit >= self.interval:
            self.save()

    def complete(self, domains):
        base = getattr(self.pool, 'discovery_pool', self.pool)
        for sid in base.sources:
            base.check(sid)
        for domain in domains:
            if not self.states[domain][5]:
                self.states[domain][5] = 1
                self.changed.add(domain)
        self.save()

    def save(self):
        if not self.changed:
            return
        require_search_space(self.path.parent)
        with self.db:
            self.db.executemany('''INSERT INTO progress VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(domain) DO UPDATE SET sid=excluded.sid,start=excluded.start,
                processed=excluded.processed,checks=excluded.checks,hits=excluded.hits,complete=excluded.complete''',
                ((domain, *self.states[domain]) for domain in self.changed))
        LOG.info('Deep association checkpoint saved: %d associations, %d hits across %d geometries',
                 sum(state[2] for state in self.states.values()), sum(state[4] for state in self.states.values()),
                 len(self.states))
        self.changed.clear()
        self.pending_hits = 0
        self.last_commit = time.monotonic()

    def close(self, successful=False):
        try:
            self.db.rollback()
        finally:
            self.db.close()
        if successful:
            # All batch results are durable and the assembly connection has
            # closed. Unlinking reclaims work without journaling every hit.
            self._remove()


@contextmanager
def association_checkpoint(case, pool, run_key, certificates):
    base = getattr(pool, 'discovery_pool', pool)
    # Synthetic unsupported readers/certificates retain the uncached oracle.
    if not isinstance(certificates, SampleCertificates) or not isinstance(base, ReaderPool):
        yield None
        return
    checkpoint = None
    try:
        checkpoint = AssociationCheckpoint(case, pool, run_key)
        yield checkpoint
        finished, checkpoint = checkpoint, None
        finished.close(successful=True)
    except sqlite3.DatabaseError as error:
        if search_disk_full(error):
            raise SafetyError('Deep association paused because the case volume is full; resume the same command') from error
        raise SafetyError('Deep association checkpoint database is invalid') from error
    finally:
        if checkpoint is not None:
            checkpoint.close(successful=False)
