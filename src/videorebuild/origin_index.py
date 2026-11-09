"""Point-origin structural joins: O(P x D) starts, rather than M x P pairs."""
from collections import OrderedDict, defaultdict
import hashlib
import sqlite3

from . import nal_search
from .storage import SafetyError, json_text
from .progress import phase
from .structural_index import _directory, _scope_identity

ORIGIN_REVISION = 2
_TABLES = {'identity', 'origins', 'profiles', 'receipts', 'completed', 'matches'}


def _private(path):
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise SafetyError('Origin index must be a private regular file')


def first_anchor(movie):
    """First mandatory eligible video sample, relative to whole payload start."""
    for chunk in movie.chunks:
        track = movie.tracks[chunk.track]
        if track.handler != b'vide' or getattr(track, 'nal_length', None) != 4:
            continue
        position = chunk.offset-movie.chunks[0].offset
        for size in chunk.sizes:
            if 8 <= size < 16*1024*1024 and position >= 0:
                return position, size-4
            position += size
    return None


class OriginIndex:
    def __init__(self, case, pool):
        self.case, self.pool = case, pool
        self.db = None
        self.profiles = {}
        self.heads = OrderedDict()
        self.stats = dict(origin_catalogue_rows=0, origin_starts_checked=0, origin_nal_visits=0,
                          origin_header_reads=0, origin_header_cache_hits=0,
                          origin_profiles_reused=0, origin_postings=0)

    def _origins(self):
        for row in self.case.db.execute('''SELECT a.source_id,a.offset,a.header,a.payload_size,a.size,a.available
                FROM atoms a JOIN sources s ON s.id=a.source_id
                WHERE a.kind='mdat' AND s.complete=1 ORDER BY a.source_id,a.offset'''):
            if row[0] in self.pool.sources:
                yield tuple(row)

    def __enter__(self):
        inventory = hashlib.sha256()
        for row in self._origins():
            inventory.update(json_text(row).encode()+b'\n')
            self.stats['origin_catalogue_rows'] += 1
        # Keep the canonical path stable so changed live source stamps cannot
        # silently open another cache beneath the same registered evidence.
        legacy_identity = json_text([_scope_identity(self.pool), inventory.hexdigest(), 1])
        identity = json_text([legacy_identity, ORIGIN_REVISION, self.pool.matching_source_stamps()])
        self.path = _directory(self.case)/('origins-'+hashlib.sha256(legacy_identity.encode()).hexdigest()+'.sqlite3')
        for suffix in ('', '-journal', '-wal', '-shm'):
            _private(self.path.with_name(self.path.name+suffix))
        self.db = sqlite3.connect(self.path)
        try:
            self.db.execute('PRAGMA temp_store=MEMORY')
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if tables == _TABLES and self.db.execute('PRAGMA user_version').fetchone()[0] == 0:
                self._backup_legacy(legacy_identity)
                tables = set()
            if not tables:
                self.db.execute('BEGIN')
                with self.db:
                    self.db.execute('CREATE TABLE identity(value TEXT NOT NULL)')
                    self.db.execute('''CREATE TABLE origins(sid INTEGER,offset INTEGER,header INTEGER,payload INTEGER,
                        PRIMARY KEY(sid,offset)) WITHOUT ROWID''')
                    self.db.execute('CREATE TABLE profiles(id TEXT PRIMARY KEY,relative INTEGER,lengths TEXT) WITHOUT ROWID')
                    self.db.execute('''CREATE TABLE receipts(sid INTEGER,offset INTEGER,profile TEXT,
                        PRIMARY KEY(profile,sid,offset)) WITHOUT ROWID''')
                    self.db.execute('CREATE TABLE completed(profile TEXT PRIMARY KEY) WITHOUT ROWID')
                    self.db.execute('''CREATE TABLE matches(relative INTEGER,length INTEGER,payload INTEGER,
                        sid INTEGER,offset INTEGER,header INTEGER,
                        PRIMARY KEY(relative,length,payload,sid,offset)) WITHOUT ROWID''')
                    self.db.execute('INSERT INTO identity VALUES(?)', (identity,))
                    self.db.executemany('INSERT INTO origins VALUES(?,?,?,?)', (row[:4] for row in self._origins()))
                    self.db.execute(f'PRAGMA user_version={ORIGIN_REVISION}')
            elif (tables != _TABLES or self.db.execute('PRAGMA user_version').fetchone()[0] != ORIGIN_REVISION or
                  self.db.execute('SELECT value FROM identity LIMIT 2').fetchall() != [(identity,)]):
                raise SafetyError('Origin index identity does not match source/origin coverage or live source stamps')
        except BaseException:
            if self.db is not None:
                self.db.close()
                self.db = None
            raise
        return self

    def _backup_legacy(self, identity):
        if (self.db.execute('SELECT value FROM identity LIMIT 2').fetchall() != [(identity,)] or
                [row[1] for row in self.db.execute('PRAGMA table_info(matches)')] !=
                ['relative', 'length', 'sid', 'offset']):
            raise SafetyError('Legacy origin index does not match its source scope')
        backup = self.path.with_name(self.path.name+'.v1.bak')
        _private(backup)
        if backup.exists():
            raise SafetyError('Legacy origin index backup already exists; preserve it before rebuilding')
        state = self.path.stat()
        self.db.close()
        self.db = None
        for suffix in ('', '-journal', '-wal', '-shm'):
            _private(self.path.with_name(self.path.name+suffix))
        if any(self.path.with_name(self.path.name+suffix).exists() for suffix in ('-journal', '-wal', '-shm')):
            raise SafetyError('Legacy origin index still has a journal after closing')
        current = self.path.stat()
        if (state.st_dev, state.st_ino) != (current.st_dev, current.st_ino):
            raise SafetyError('Legacy origin index changed before backup')
        self.path.rename(backup)
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA temp_store=MEMORY')

    def __exit__(self, kind, *_):
        try:
            if kind is None:
                for sid in self.pool.sources:
                    self.pool.check(sid)
        finally:
            if self.db is not None:
                self.db.close()
                self.db = None
            self.heads.clear()

    def _header(self, sid, position):
        key = sid, position
        if key in self.heads:
            self.stats['origin_header_cache_hits'] += 1
            self.heads.move_to_end(key)
            return self.heads[key]
        self.stats['origin_header_reads'] += 1
        value = nal_search.header(lambda offset, count: self.pool.read(sid, offset, count), position)
        self.heads[key] = value
        if len(self.heads) > 8192:
            self.heads.popitem(last=False)
        return value

    def prepare(self, profiles):
        grouped = defaultdict(set)
        for relative, length in profiles:
            if type(relative) is not int or relative < 0 or type(length) is not int or not 4 <= length < 16*1024*1024-4:
                raise ValueError('invalid mandatory origin anchor')
            grouped[relative].add(length)
        grouped = {relative: lengths for relative, lengths in grouped.items()
                   if not lengths.issubset(self.profiles.get(relative, ()))}
        if not grouped:
            return
        with phase('Index whole payload origins', unit='origins') as task:
            for relative, lengths in sorted(grouped.items()):
                if lengths.issubset(self.profiles.get(relative, ())):
                    continue
                lengths.update(self.profiles.get(relative, ()))
                encoded = json_text(sorted(lengths))
                profile = hashlib.sha256(json_text([relative, encoded]).encode()).hexdigest()
                self.db.execute('INSERT OR IGNORE INTO profiles VALUES(?,?,?)', (profile, relative, encoded))
                if self.db.execute('SELECT 1 FROM completed WHERE profile=?', (profile,)).fetchone():
                    self.stats['origin_profiles_reused'] += 1
                    self.profiles[relative] = frozenset(lengths)
                    continue
                missing = self.db.execute('''SELECT o.sid,o.offset,o.header,o.payload FROM origins o LEFT JOIN receipts r
                        ON r.sid=o.sid AND r.offset=o.offset AND r.profile=? WHERE r.sid IS NULL ORDER BY o.sid,o.offset''', (profile,))
                maximum = max(lengths)+4
                checked = 0
                for sid, offset, header, payload in missing:
                    self.pool.check(sid)
                    self.stats['origin_starts_checked'] += 1
                    if self.pool.permits(sid, offset, header):
                        def visit():
                            self.stats['origin_nal_visits'] += 1
                            return True
                        for length in nal_search.cumulative_lengths(
                                lambda position: self._header(sid, position), offset+header+relative,
                                self.pool.sources[sid]['size'], maximum, visit):
                            if length in lengths:
                                self.stats['origin_postings'] += self.db.execute(
                                    'INSERT OR IGNORE INTO matches VALUES(?,?,?,?,?,?)',
                                    (relative, length, payload, sid, offset, header)).rowcount
                    self.pool.check(sid)
                    self.db.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?)', (sid, offset, profile))
                    checked += 1
                    task.advance(origin_starts_checked=self.stats['origin_starts_checked'])
                    if checked % 128 == 0:
                        self.db.commit()
                self.db.execute('INSERT OR IGNORE INTO completed VALUES(?)', (profile,))
                self.db.commit()
                self.profiles[relative] = frozenset(lengths)

            task.heartbeat(**self.stats)

    def candidates(self, relative, length):
        if length not in self.profiles.get(relative, ()):
            raise SafetyError('Origin profile coverage is incomplete')
        yield from self.db.execute('''SELECT sid,offset,header,payload FROM matches
                WHERE relative=? AND length=? ORDER BY payload,sid,offset''', (relative, length))
