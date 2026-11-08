"""Durable exact framing results for a completed structural posting set."""
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
import hashlib
from pathlib import Path
import shutil
import sqlite3

from .bmff import FormatError, valid_sample
from .storage import SafetyError, json_text
from .structural_index import _directory
from .validate import VALIDATION_REVISION


# Bump when the cache layout or exact sample-framing predicate changes.
CERTIFICATE_REVISION = 1
PAGE_ROWS = 4096
PAGE_READ_BYTES = 64 * 1024**2
# Leave room for SQLite's journal, case reports and interrupted-run cleanup.
SEARCH_RESERVE_BYTES = 1024**3
Profile = tuple[int, bytes, int]
SUPPORTED_CODECS = {b'avc1', b'avc3', b'hvc1', b'hev1'}


def require_search_space(directory: Path) -> None:
    if shutil.disk_usage(directory).free < SEARCH_RESERVE_BYTES:
        raise SafetyError('Deep search paused to preserve free space on the case volume')


def search_disk_full(error: sqlite3.Error) -> bool:
    return getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL


class SampleCertificates:
    def __init__(self, db, discovery, pool, directory):
        self.db = db
        self.discovery = discovery
        self.pool = pool
        self.directory = directory
        self.ready: set[Profile] = set()

    def prepare(self, profiles: Iterable[Profile], task) -> None:
        profiles = sorted(set(profiles))
        self.ready.difference_update(profiles)
        if any(not 8 <= length + 4 < 16*1024**2 or codec not in SUPPORTED_CODECS or width != 4
               for length, codec, width in profiles):
            raise SafetyError('Deep sample framing profile is outside the certified scope')
        try:
            self._prepare(profiles, task)
        except sqlite3.DatabaseError as error:
            if search_disk_full(error):
                raise SafetyError('Deep search paused because the case volume is full') from error
            raise SafetyError('Deep sample certificate database is invalid') from error

    def _prepare(self, profiles, task) -> None:
        totals = {}
        saved = {}
        reused = 0
        for profile in profiles:
            frequency = self.discovery.execute('SELECT total FROM frequencies WHERE length=?', (profile[0],)).fetchone()
            totals[profile] = frequency[0] if frequency is not None else 0
            state = self.db.execute('''SELECT sid,start,processed,total,complete FROM progress
                WHERE length=? AND codec=? AND width=?''', profile).fetchone()
            if state is None:
                continue
            _, _, count, total, complete = state
            if total != totals[profile] or not 0 <= count <= total or bool(complete) != (count == total):
                raise SafetyError('Deep sample checkpoint does not match structural posting coverage')
            saved[profile] = state
            reused += count

        completed = reused
        task.update(completed, total=sum(totals.values()), reused_samples=reused)
        for profile in profiles:
            previous = saved.get(profile)
            if previous is not None and previous[4]:
                self.ready.add(profile)
                continue
            sid, start, count = previous[:3] if previous is not None else (0, 0, 0)
            for added in self._certify(profile, totals[profile], sid, start, count):
                completed += added
                task.update(completed)
            self.ready.add(profile)

    def _certify(self, profile, total, sid, start, processed):
        length, codec, width = profile
        size = length + 4
        limit = min(PAGE_ROWS, max(1, PAGE_READ_BYTES // size))
        while True:
            require_search_space(self.directory)
            if processed:
                rows = self.discovery.execute('''SELECT sid,start FROM locations
                    WHERE length=? AND (sid,start)>(?,?) ORDER BY sid,start LIMIT ?''',
                    (length, sid, start, limit + 1)).fetchall()
            else:
                rows = self.discovery.execute('''SELECT sid,start FROM locations
                    WHERE length=? ORDER BY sid,start LIMIT ?''', (length, limit + 1)).fetchall()
            complete = len(rows) <= limit
            if not complete:
                rows.pop()
            count = processed + len(rows)
            if count > total or complete != (count == total):
                raise SafetyError('Structural posting count does not match its recorded coverage')
            accepted = []
            touched = set()
            for sid, start in rows:
                if sid not in touched:
                    self.pool.check(sid)
                    touched.add(sid)
                try:
                    sample = self.pool.read(sid, start, size)
                except FormatError:
                    continue  # Unavailable in this acquisition scope.
                if valid_sample(sample, codec, width):
                    accepted.append((*profile, sid, start))
            for source in touched:
                self.pool.check(source)
            require_search_space(self.directory)
            # A negative needs no row. A committed cursor proves all rows in
            # its prefix were classified, including pages with no positives.
            with self.db:
                self.db.executemany('INSERT INTO accepted VALUES(?,?,?,?,?)', accepted)
                self.db.execute('''INSERT INTO progress VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(length,codec,width) DO UPDATE SET
                    sid=excluded.sid,start=excluded.start,processed=excluded.processed,
                    complete=excluded.complete''', (*profile, sid, start, count, total, int(complete)))
            yield len(rows)
            if complete:
                return
            processed = count

    def locations(self, profile: Profile, *, after: tuple[int, int] | None = None) -> sqlite3.Cursor:
        if profile not in self.ready:
            raise SafetyError('Deep sample framing has no complete coverage receipt')
        if after is not None:
            return self.db.execute('''SELECT sid,start FROM accepted
                WHERE length=? AND codec=? AND width=? AND (sid,start)>(?,?) ORDER BY sid,start''',
                (*profile, *after))
        return self.db.execute('''SELECT sid,start FROM accepted
            WHERE length=? AND codec=? AND width=? ORDER BY sid,start''', profile)


def _initialize(db, identity: str) -> None:
    tables = {name for (name,) in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    if tables:
        if tables != {'identity', 'progress', 'accepted'}:
            raise SafetyError('Deep sample certificate database is incomplete')
        if db.execute('SELECT value FROM identity LIMIT 2').fetchall() != [(identity,)]:
            raise SafetyError('Deep sample certificate source or index identity changed')
        return
    db.executescript('''BEGIN;
        CREATE TABLE identity(value TEXT NOT NULL);
        CREATE TABLE progress(length INTEGER,codec BLOB,width INTEGER,sid INTEGER NOT NULL,
            start INTEGER NOT NULL,processed INTEGER NOT NULL,total INTEGER NOT NULL,
            complete INTEGER NOT NULL CHECK(complete IN (0,1)),
            PRIMARY KEY(length,codec,width)) WITHOUT ROWID;
        CREATE TABLE accepted(length INTEGER,codec BLOB,width INTEGER,sid INTEGER,start INTEGER,
            PRIMARY KEY(length,codec,width,sid,start)) WITHOUT ROWID;''')
    db.execute('INSERT INTO identity VALUES(?)', (identity,))
    db.commit()


@contextmanager
def sample_certificates(case, pool, discovery, index_relative_path: str) -> Iterator[SampleCertificates]:
    """Open exact certificates for postings with complete source/profile coverage."""
    directory = _directory(case)
    relative = Path(index_relative_path)
    rows = discovery.execute('SELECT value FROM identity LIMIT 2').fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        raise SafetyError('Deep sample certificates require a bound structural index')
    key = hashlib.sha256(rows[0][0].encode()).hexdigest()
    index_path = next(Path(path) for _, name, path in discovery.execute('PRAGMA database_list') if name == 'main')
    if (relative != Path('work', 'structural-index', key + '.sqlite3') or
            index_path.resolve() != (directory / relative.name).resolve()):
        raise SafetyError('Deep sample certificates require a bound structural index')
    cache = directory / f'{key}.samples-{CERTIFICATE_REVISION}-{VALIDATION_REVISION}.sqlite3'
    for suffix in ('', '-journal', '-wal', '-shm'):
        path = cache.with_name(cache.name + suffix)
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
            raise SafetyError('Deep sample certificate database must be a private regular file')
    if not cache.exists():
        require_search_space(directory)
    # Include sources with only rejected postings. The structural key describes
    # registered evidence; the stamps also pin ctime across certificate sessions.
    identity = hashlib.sha256(json_text([key, pool.matching_source_stamps()]).encode()).hexdigest()
    try:
        db = sqlite3.connect(cache)
        try:
            _initialize(db, identity)
        except BaseException:
            db.close()
            raise
    except sqlite3.DatabaseError as error:
        if search_disk_full(error):
            raise SafetyError('Deep search paused because the case volume is full') from error
        raise SafetyError('Deep sample certificate database is invalid') from error
    try:
        yield SampleCertificates(db, discovery, pool, directory)
    finally:
        db.close()
