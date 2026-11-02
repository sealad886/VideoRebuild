"""Case database, source safety, range reads, and streaming signature scanning."""
from __future__ import annotations

from collections import OrderedDict
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import tempfile
import time
from typing import Iterator, TextIO

from .progress import phase
from .paths import is_ignored_path, is_ignored_source
from .bmff import MAX_METADATA, FormatError, boxes, parse_movie
from .ranges import Extent, RescueMap, UnavailableRange, resolve_extents, validate_extents

LOG = logging.getLogger(__name__)
SCHEMA = 2
BLOCK = 4 * 1024 * 1024
SIGNATURES = re.compile(b'ftyp|moov|mdat|moof')
EXTENSIONS = {'.mp4', '.mov', '.m4v', '.m4s', '.bin', '.dat', '.h264', '.h265', '.264', '.265', '.hevc'}


class SafetyError(RuntimeError):
    pass


def json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


@contextmanager
def atomic_text(path: Path) -> Iterator[TextIO]:
    if path.is_symlink():
        raise SafetyError('Refusing to write through a case-file symlink')
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def write_json(path: Path, value: object) -> None:
    with atomic_text(path) as f:
        json.dump(value, f, indent=2, sort_keys=True, ensure_ascii=True)
        f.write('\n')


def create_private_json(path: Path, value: object) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    created = os.fstat(fd)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, indent=2, sort_keys=True, ensure_ascii=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            current = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                path.unlink(missing_ok=True)
        raise


def file_sha256(path: Path) -> str:
    if is_ignored_path(path):
        raise SafetyError('Finder metadata is not recovery evidence')
    digest = hashlib.sha256()
    with phase('Hashing '+path.name, total=path.stat().st_size, unit='bytes') as task, path.open('rb') as f:
        completed = 0
        while data := f.read(1024 * 1024):
            digest.update(data)
            completed += len(data)
            task.update(completed)
    return digest.hexdigest()


def regular(path: Path | str) -> os.stat_result:
    s = os.stat(path)
    if not stat.S_ISREG(s.st_mode):
        raise SafetyError(f'Not a regular file: {path}. Use a disk image, not a live block device.')
    return s


def within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def initialize_database(db: sqlite3.Connection) -> None:
    """Initialize the canonical schema for new cases and explicit migration."""
    db.executescript('''
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, dev INTEGER NOT NULL,
                    ino INTEGER NOT NULL, sha256 TEXT, complete INTEGER NOT NULL DEFAULT 0,
                    extents TEXT, binding TEXT NOT NULL DEFAULT '{}', hash_scope TEXT);
                CREATE INDEX IF NOT EXISTS sources_physical ON sources(id) WHERE extents IS NULL;
                CREATE TABLE IF NOT EXISTS movies (
                    digest TEXT PRIMARY KEY, data BLOB NOT NULL, summary TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS atoms (
                    id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
                    kind TEXT NOT NULL, offset INTEGER NOT NULL, size INTEGER NOT NULL,
                    header INTEGER NOT NULL, available INTEGER NOT NULL,
                    payload_size INTEGER NOT NULL, movie_digest TEXT REFERENCES movies(digest),
                    UNIQUE(source_id,kind,offset));
                CREATE INDEX IF NOT EXISTS atoms_kind_size ON atoms(kind,payload_size);
                CREATE INDEX IF NOT EXISTS atoms_movie ON atoms(movie_digest);
                CREATE TABLE IF NOT EXISTS jobs (
                    kind TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL,
                    detail TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(kind,key));
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(kind,status);
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY, movie_digest TEXT NOT NULL,
                    mapping_digest TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
                    detail TEXT NOT NULL, UNIQUE(movie_digest,mapping_digest));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL,
                    detail TEXT NOT NULL);
            ''')
    db.execute(f'PRAGMA user_version={SCHEMA}')
    db.commit()


def source_identity(path: Path) -> dict:
    s = regular(path)
    return dict(path=str(path),size=s.st_size,mtime_ns=s.st_mtime_ns,dev=s.st_dev,ino=s.st_ino)


def _source_stamp(s: os.stat_result) -> tuple[int, int, int]:
    """Persisted continuity checks; mount-assigned device numbers are transient."""
    return s.st_size, s.st_mtime_ns, s.st_ino


def _live_stamp(s: os.stat_result) -> tuple[int, int, int, int]:
    """Same-run path/descriptor identity still includes the current device."""
    return s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino


def _binding_identity(binding: dict) -> dict:
    value = dict(binding)
    if 'mapfile' in value:
        value['mapfile'] = {key: item for key, item in value['mapfile'].items() if key != 'dev'}
    return value


class Case:
    def __init__(self, root: Path):
        if root.expanduser().is_symlink():
            raise SafetyError('Case root must not be a symlink')
        self.root = root.expanduser().resolve()
        self.db: sqlite3.Connection | None = None
        self.lock = None

    def __enter__(self) -> 'Case':
        import fcntl
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Advisory lock survives crashes without a stale-lock deletion operation.
        lockpath = self.root / '.lock'
        if lockpath.is_symlink() or (lockpath.exists() and lockpath.stat().st_nlink != 1):
            raise SafetyError('case lock must not be a symlink')
        self.lock = lockpath.open('a+b')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            self.lock.close()
            raise SafetyError('Another process is using this case') from e
        try:
            dbpath = self.root / 'case.sqlite3'
            if dbpath.is_symlink() or (dbpath.exists() and dbpath.stat().st_nlink != 1):
                raise SafetyError('case database must not be a symlink')
            if (self.root / 'fragmented.sqlite3').exists() and not dbpath.exists():
                raise SafetyError('Legacy fragmented case: run migrate-case before recovery')
            if not dbpath.exists():
                unknown = [p.name for p in self.root.iterdir()
                           if p.name != '.lock' and not is_ignored_path(p)]
                if unknown:
                    raise SafetyError(f'New case must be empty; found {unknown[:5]}')
            self.db = sqlite3.connect(dbpath)
            self.db.row_factory = sqlite3.Row
            # Candidate-query sorts must not spill onto the system temp disk.
            # Persistent case tables remain in the database on the output volume.
            self.db.execute('PRAGMA temp_store=MEMORY')
            self.db.execute('PRAGMA foreign_keys=ON')
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA):
                raise SafetyError(f'Unsupported database schema {version}; run migrate-case')
            if version == 0 and self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                raise SafetyError('Unversioned existing database requires explicit migration')
            self.db.execute('PRAGMA journal_mode=WAL')
            initialize_database(self.db)
            for name in ('candidates', 'reconstructed', 'reports', 'work'):
                p = self.root / name
                if p.is_symlink():
                    raise SafetyError(f'Case subdirectory must not be a symlink: {p}')
                p.mkdir(exist_ok=True)
            return self
        except BaseException:
            if self.db:
                self.db.close()
            self.lock.close()
            raise

    def __exit__(self, *_: object) -> None:
        if self.db:
            self.db.close()
        if self.lock:
            self.lock.close()

    def event(self, kind: str, detail: object) -> None:
        self.db.execute('INSERT INTO events(created,kind,detail) VALUES(?,?,?)',
                        (time.time(), kind, json_text(detail)))
        self.db.commit()

    def result(self, key: str, status: str, detail: dict, *, kind: str = 'ordinary') -> None:
        self.db.execute('''INSERT INTO jobs VALUES(?,?,?,?,?) ON CONFLICT(kind,key)
                           DO UPDATE SET status=excluded.status,detail=excluded.detail,updated=excluded.updated''',
                        (kind, str(key), status, json_text(detail), time.time()))
        self.db.commit()

    def job(self, kind: str, key: str) -> dict | None:
        row = self.db.execute('SELECT * FROM jobs WHERE kind=? AND key=?', (kind,str(key))).fetchone()
        if row is None:
            return None
        value = dict(row)
        value['detail'] = json.loads(value['detail'])
        return value

    def get_jobs(self, kind: str | None = None) -> Iterator[dict]:
        query, params = ('SELECT * FROM jobs ORDER BY kind,key', ()) if kind is None else (
            'SELECT * FROM jobs WHERE kind=? ORDER BY key', (kind,))
        for row in self.db.execute(query, params):
            value = dict(row)
            value['detail'] = json.loads(value['detail'])
            yield value

    def bind(self, name: str, settings: object) -> None:
        value = json_text(settings)
        old = self.db.execute('SELECT value FROM settings WHERE name=?', (name,)).fetchone()
        if old and old[0] != value:
            raise SafetyError(f'Case settings changed for {name}; use a new case')
        self.db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (name,value))
        self.db.commit()

    def sources(self) -> dict[int, dict]:
        return {r['id']: dict(r) for r in self.db.execute('SELECT * FROM sources WHERE complete=1 ORDER BY id')
                if not is_ignored_source(r)}

    def physical_sources(self) -> dict[int, dict]:
        return {r['id']: dict(r) for r in self.db.execute('SELECT * FROM sources WHERE extents IS NULL ORDER BY id')
                if not is_ignored_source(r)}

    def register_source(self, path: Path, kind: str = 'file', *, mapfile: Path | None = None,
                        image_offset: int = 0) -> int:
        if is_ignored_path(path):
            raise SafetyError('Finder metadata is not recovery evidence')
        path = path.expanduser().resolve()
        if is_ignored_path(path):
            raise SafetyError('Finder metadata is not recovery evidence')
        if within(path,self.root):
            raise SafetyError('Source must be outside the case')
        identity = source_identity(path)
        binding = {'acquisition':'unknown'}
        if mapfile is not None:
            if kind != 'image':
                raise SafetyError('A rescue map must be bound to an image')
            mapfile = mapfile.expanduser().resolve()
            if is_ignored_path(mapfile):
                raise SafetyError('Finder metadata is not an acquisition map')
            if within(mapfile,self.root):
                raise SafetyError('Rescue map must be outside the case')
            bound = source_identity(mapfile)
            RescueMap.from_file(mapfile,image_offset=image_offset)
            bound['sha256'] = file_sha256(mapfile)
            if source_identity(mapfile) != {k:v for k,v in bound.items() if k != 'sha256'}:
                raise SafetyError('Rescue map changed during registration')
            binding = {'mapfile':bound,'image_offset':image_offset}
        elif image_offset:
            raise SafetyError('Image offset requires a rescue map')
        row = self.db.execute('SELECT * FROM sources WHERE path=?',(str(path),)).fetchone()
        if row:
            if row['extents'] is not None or row['kind'] != kind:
                raise SafetyError('The same input was supplied with different source roles')
            if any(row[k] != identity[k] for k in ('size','mtime_ns','ino')):
                raise SafetyError(f'Previously indexed source changed: {path}; use a new case')
            if _binding_identity(json.loads(row['binding'])) != _binding_identity(binding):
                raise SafetyError('Source acquisition settings changed; use a new case')
            return row['id']
        self.invalidate_results()
        cur = self.db.execute('''INSERT INTO sources(path,kind,size,mtime_ns,dev,ino,binding)
                               VALUES(?,?,?,?,?,?,?)''',
                              (str(path),kind,identity['size'],identity['mtime_ns'],identity['dev'],
                               identity['ino'],json_text(binding)))
        self.db.commit()
        return cur.lastrowid

    def add_stream(self, name: str, extents, *, size: int,
                   association: str = 'filesystem_metadata') -> int:
        if is_ignored_path(name):
            raise SafetyError('Finder metadata is not recovery evidence')
        transaction = self.db.in_transaction
        physical = self.physical_sources()
        extents = validate_extents(size,extents,{sid:r['size'] for sid,r in physical.items()})
        encoded = json_text([asdict(e) for e in extents])
        binding = json_text({'association':association,'name':name})
        identity = json_text({'size':size,'extents':json.loads(encoded),'name':name,'association':association})
        path = 'logical:' + hashlib.sha256(identity.encode()).hexdigest()
        row = self.db.execute('SELECT * FROM sources WHERE path=?',(path,)).fetchone()
        if row:
            if row['size'] != size or row['extents'] != encoded or row['binding'] != binding:
                raise SafetyError(f'Logical source changed: {name}; use a new case')
            return row['id']
        if not transaction:
            self.invalidate_results()
        cur = self.db.execute('''INSERT INTO sources(path,kind,size,mtime_ns,dev,ino,extents,binding)
                               VALUES(?,?,?,0,0,0,?,?)''',
                              (path,'logical',size,encoded,binding))
        if not transaction:
            self.db.commit()
        return cur.lastrowid

    def invalidate_results(self) -> None:
        # A larger evidence set can reveal a second valid pairing. Do not leave
        # a prior uniqueness claim published after even a scan-only operation.
        rows = self.db.execute("SELECT * FROM jobs WHERE kind='ordinary' AND status NOT IN ('stale','historical')").fetchall()
        for row in rows:
            detail = json.loads(row['detail'])
            detail['previous_status'] = row['status']
            detail['reason'] = 'source_catalog_changed; run recover again'
            detail.pop('run_key', None)
            detail['outputs'] = []
            for suffix in ('.mp4', '.json'):
                p = self.root / 'reconstructed' / (row['key'][:24] + suffix)
                if p.is_symlink():
                    raise SafetyError('Published artifact is a symlink')
                if p.exists():
                    p.unlink()  # Only the publication name; candidate bytes remain.
            self.db.execute("UPDATE jobs SET status=?,detail=?,updated=? WHERE kind='ordinary' AND key=?",
                            ('stale', json_text(detail), time.time(), row['key']))
        self.db.commit()


class ReaderPool:
    """Bounded read-only access to files and logical streams through one path."""
    def __init__(self, sources: dict[int, dict], limit: int = 32, *, physical_sources=None,
                 allocation_scope=None):
        if limit < 1:
            raise ValueError('Reader handle limit must be positive')
        self.sources = {sid: row for sid, row in sources.items() if not is_ignored_source(row)}
        self.allocation_scope = allocation_scope
        self.physical = {sid: row for sid, row in (physical_sources or {}).items()
                         if not is_ignored_source(row)}
        self.physical.update({sid:r for sid,r in self.sources.items() if not r.get('extents')})
        self.limit = limit
        self.handles = OrderedDict()
        self.extents = {}
        self.physical_ids = {}
        self.maps = {}
        self._identities = {}
        self._map_identities = {}
        sizes = {sid:r['size'] for sid,r in self.physical.items()}
        for sid,row in self.sources.items():
            if row.get('extents'):
                raw = row['extents']
                raw = json.loads(raw) if isinstance(raw,str) else raw
                entries = [e if isinstance(e,Extent) else Extent(**e) for e in raw]
                self.extents[sid] = validate_extents(row['size'],entries,sizes)
                self.physical_ids[sid] = tuple(sorted({e.source_id for e in self.extents[sid]}))
        for sid,row in self.physical.items():
            binding = row.get('binding') or {}
            binding = json.loads(binding) if isinstance(binding,str) else binding
            if 'mapfile' in binding:
                bound = binding['mapfile']
                path = Path(bound['path'])
                before = self._check_identity(bound)
                rescue = RescueMap.from_file(path,image_offset=binding.get('image_offset',0))
                if file_sha256(path) != bound['sha256']:
                    raise SafetyError('Rescue map changed since registration')
                after = self._check_identity(bound)
                if _live_stamp(before) != _live_stamp(after):
                    raise SafetyError('Rescue map changed while hashing')
                self.maps[sid] = (rescue,bound)
                self._map_identities[sid] = _live_stamp(after)

    def close(self) -> None:
        for f in self.handles.values():
            f.close()
        self.handles.clear()

    def __enter__(self) -> 'ReaderPool':
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _check_identity(row: dict) -> os.stat_result:
        if is_ignored_source(row):
            raise SafetyError('Finder metadata is not recovery evidence')
        s = regular(row['path'])
        if _source_stamp(s) != (row['size'], row['mtime_ns'], row['ino']):
            raise SafetyError(f'Source changed since indexing: {row["path"]}. Start a new case for changed evidence.')
        return s

    def _checked_stat(self, sid: int) -> os.stat_result:
        if sid not in self.physical:
            raise SafetyError('Source is unavailable or ignored Finder metadata')
        s = self._check_identity(self.physical[sid])
        stamp = _live_stamp(s)
        previous = self._identities.setdefault(sid, stamp)
        if stamp != previous:
            raise SafetyError('Source changed during this run')
        return s

    def _check_map(self, sid: int) -> None:
        if sid in self.maps:
            state = self._check_identity(self.maps[sid][1])
            if _live_stamp(state) != self._map_identities[sid]:
                raise SafetyError('Rescue map changed during this run')

    def physical_key(self, sid: int) -> tuple[int, int]:
        """Live physical alias key, separate from historical manifest stamps."""
        if sid not in self._identities:
            self._checked_stat(sid)
        stamp = self._identities[sid]
        return stamp[2], stamp[3]

    def check(self, sid: int) -> None:
        ids = self.physical_ids.get(sid,(sid,))
        for physical in ids:
            self._checked_stat(physical)
            self._check_map(physical)

    def handle(self, sid: int):
        current = self._checked_stat(sid)
        self._check_map(sid)
        if sid in self.handles:
            if _live_stamp(os.fstat(self.handles[sid].fileno())) != _live_stamp(current):
                raise SafetyError('Source changed while its descriptor was cached')
            self.handles.move_to_end(sid)
            return self.handles[sid]
        # A buffered reader may fetch beyond the permitted extent even when
        # read() requests only allowed bytes. Keep physical reads exact.
        f = Path(self.physical[sid]['path']).open('rb', buffering=0)
        # Verify the opened inode too, not only the path checked before open().
        s = os.fstat(f.fileno())
        if _live_stamp(s) != _live_stamp(current):
            f.close()
            raise SafetyError('Source changed during open')
        self.handles[sid] = f
        if len(self.handles) > self.limit:
            self.handles.popitem(last=False)[1].close()
        return f

    def _ranges(self, sid: int, offset: int, size: int):
        if sid not in self.sources or offset < 0 or size < 0 or offset+size > self.sources[sid]['size']:
            raise FormatError('source range exceeds file bounds')
        if sid in self.extents:
            parts = tuple(resolve_extents(self.extents[sid],offset,size))
        elif size:
            parts = (Extent(offset,sid,offset,size),)
        else:
            parts = ()
        # Check the entire request before any caller can consume its first bytes.
        for e in parts:
            if self.allocation_scope is not None and self.physical[e.source_id].get('kind') == 'image':
                self.allocation_scope.require(e.source_id,e.source_offset,e.length)
            if e.source_id in self.maps:
                self.maps[e.source_id][0].require_rescued(e.source_offset,e.length)
        yield from parts

    def permits(self, sid: int, offset: int, size: int) -> bool:
        """Check availability and allocation policy without opening media bytes."""
        try:
            tuple(self._ranges(sid,offset,size))
        except FormatError:
            return False
        self.check(sid)
        return True

    def read(self, sid: int, offset: int, size: int) -> bytes:
        parts = []
        for e in self._ranges(sid,offset,size):
            f = self.handle(e.source_id)
            f.seek(e.source_offset)
            data = f.read(e.length)
            if len(data) != e.length:
                raise SafetyError(f'Short read from {self.physical[e.source_id]["path"]}')
            self._checked_stat(e.source_id)
            self._check_map(e.source_id)
            parts.append(data)
        return b''.join(parts)

    def provenance(self, sid: int, offset: int, size: int) -> list[dict]:
        result = []
        for e in self._ranges(sid,offset,size):
            row = self.physical[e.source_id]
            self._checked_stat(e.source_id)
            self._check_map(e.source_id)
            result.append(dict(logical_offset=e.logical_offset,source_id=e.source_id,
                               source_path=row['path'],source_offset=e.source_offset,length=e.length,
                               acquisition='rescued' if e.source_id in self.maps else 'unknown'))
        return result

    def available_ranges(self, sid: int, start: int = 0, end: int | None = None):
        end = self.sources[sid]['size'] if end is None else end
        if start < 0 or end < start or end > self.sources[sid]['size']:
            raise FormatError('source range exceeds file bounds')
        self.check(sid)
        extents = self.extents.get(sid,(Extent(0,sid,0,self.sources[sid]['size']),))
        pending = None
        index = max(0,bisect_right(extents,start,key=lambda e:e.logical_offset)-1)
        while index < len(extents):
            e = extents[index]
            index += 1
            if e.logical_offset >= end:
                break
            lo,hi = max(start,e.logical_offset),min(end,e.logical_offset+e.length)
            if hi <= lo:
                continue
            physical = e.source_offset+lo-e.logical_offset
            if e.source_id in self.maps:
                spans = ((lo+s.offset-physical,s.length) for s in
                         self.maps[e.source_id][0].spans(physical,hi-lo) if s.status == '+')
            else:
                spans = ((lo,hi-lo),)
            for offset,length in spans:
                if self.allocation_scope is not None and self.physical[e.source_id].get('kind') == 'image':
                    at = e.source_offset+offset-e.logical_offset
                    allowed = ((e.logical_offset+position-e.source_offset, count)
                               for position,count in self.allocation_scope.ranges(e.source_id,at,at+length))
                else:
                    allowed = ((offset,length),)
                for position,count in allowed:
                    if pending is not None and pending[0]+pending[1] == position:
                        pending = (pending[0],pending[1]+count)
                    else:
                        if pending is not None:
                            yield pending
                        pending = (position,count)
        if pending is not None:
            yield pending

    def iter_blocks(self, sid: int, overlap: int = 16):
        for start,length in self.available_ranges(sid):
            end = start+length
            pos = start
            data = self.read(sid,pos,min(BLOCK+overlap,end-pos))
            while data:
                owned = min(BLOCK,len(data))
                yield pos,data,owned
                pos += owned
                if len(data) <= BLOCK:
                    break
                lookahead = data[owned:]
                at = pos+len(lookahead)
                data = lookahead+self.read(sid,at,min(BLOCK,end-at))
        self.check(sid)


def input_files(roots: list[Path], case_root: Path, all_files: bool) -> Iterator[Path]:
    with phase('Discovering recovered files', unit='files') as task:
        seen = set()
        discovered = skipped = 0
        for root in roots:
            if is_ignored_path(root):
                skipped += 1
                continue
            root = root.expanduser().resolve()
            if is_ignored_path(root):
                skipped += 1
                continue
            if not root.exists():
                raise SafetyError(f'Input does not exist: {root}')
            if root.is_dir():
                if within(case_root, root) or within(root, case_root):
                    raise SafetyError('Case and input directories must not contain one another')
                def onerror(error: OSError) -> None:
                    raise SafetyError(f'Cannot enumerate input directory: {error}') from error
                def walk_paths():
                    for folder, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
                        dirs[:] = sorted(name for name in dirs if not is_ignored_path(name))
                        for name in sorted(names):
                            yield Path(folder)/name
                paths = walk_paths()
            else:
                paths = iter([root])
            for p in paths:
                discovered += 1
                if discovered % 256 == 0:
                    task.heartbeat(discovered=discovered, skipped=skipped)
                if is_ignored_path(p):
                    skipped += 1
                    continue
                if p.is_symlink():
                    LOG.warning('Skipping symlink: %s', p)
                    skipped += 1
                    continue
                if not all_files and root.is_dir() and p.suffix.lower() not in EXTENSIONS:
                    skipped += 1
                    continue
                p = p.resolve()
                if within(p, case_root):
                    raise SafetyError('An input is inside the case directory')
                regular(p)
                if str(p) not in seen:
                    seen.add(str(p))
                    task.advance(detail=p.name, discovered=discovered, skipped=skipped)
                    yield p
        task.heartbeat(discovered=discovered, skipped=skipped)


def _candidate_header(read, offset: int, size: int) -> tuple[int, bytes, int] | None:
    if offset < 0 or offset + 8 > size:
        return None
    raw = read(offset,8)
    n, kind = struct.unpack_from('>I4s', raw)
    header = 8
    if n == 1:
        if offset+16 > size:
            return None
        n = struct.unpack('>Q', read(offset+8,8))[0]
        header = 16
    elif n == 0:
        n = size - offset
    if n < header or n > 16 * 1024**4:
        return None
    return n, kind, header


def scan_logical(case: Case, sid: int) -> None:
    row = case.db.execute('SELECT * FROM sources WHERE id=?',(sid,)).fetchone()
    if row is None:
        raise SafetyError('Unknown source')
    if is_ignored_source(row):
        LOG.info('Ignoring Finder metadata source: %s', row['path'])
        return
    sources = {sid:dict(row)}
    if row['extents'] is None:
        physical = sources
    else:
        ids = {e['source_id'] for e in json.loads(row['extents'])}
        physical = {}
        for source_id in ids:
            source = case.db.execute('SELECT * FROM sources WHERE id=? AND extents IS NULL',
                                     (source_id,)).fetchone()
            if source is None:
                raise SafetyError('Logical stream references an unavailable physical source')
            physical[source_id] = dict(source)
    if row['complete']:
        with phase('Verifying indexed source', total=1, unit='files') as task, ReaderPool(sources,physical_sources=physical) as pool:
            pool.check(sid)
            task.advance(detail=Path(row['path']).name)
        LOG.info('Already indexed: %s',row['path'])
        return
    case.db.execute('DELETE FROM atoms WHERE source_id=?',(sid,))
    case.db.commit()
    digest = hashlib.sha256()
    count = scanned = 0
    label = ('Scanning allocation-unfiltered image' if row['kind'] == 'image' else
             'Scanning mapped stream' if row['kind'] == 'logical' else 'Scanning recovered file')
    with phase(label, unit='bytes') as task:
        with ReaderPool(sources,physical_sources=physical) as pool:
            task.update(total=sum(length for _, length in pool.available_ranges(sid)), detail=Path(row['path']).name)
            for base, data, owned in pool.iter_blocks(sid):
                # The scan window already passed ReaderPool's range and identity
                # checks. Reuse contained headers/metadata instead of seeking
                # and checking the same physical source for every signature.
                def read(offset, size):
                    if base <= offset and offset + size <= base + len(data):
                        return data[offset-base:offset-base+size]
                    return pool.read(sid, offset, size)

                digest.update(memoryview(data)[:owned])
                scanned += owned
                for match in SIGNATURES.finditer(data):
                    # Type field may be in lookahead; ownership belongs to the size field.
                    start = base + match.start() - 4
                    if not base <= start < base + owned:
                        continue
                    try:
                        info = _candidate_header(read,start,row['size'])
                    except UnavailableRange:
                        continue
                    if info is None:
                        continue
                    n, tag, header = info
                    available = min(n, row['size'] - start)
                    mdigest = None
                    if tag in {b'moov', b'moof', b'ftyp'}:
                        cap = 4096 if tag == b'ftyp' else MAX_METADATA
                        if available != n or n > cap:
                            continue
                        try:
                            raw = read(start,n)
                        except UnavailableRange:
                            continue
                        if tag == b'ftyp':
                            payload = raw[header:]
                            if len(payload) < 8 or len(payload) % 4 or not all(32 <= x <= 126 for x in payload[:4]):
                                continue
                        else:
                            try:
                                root = next(boxes(raw))
                                child = list(boxes(raw, root.payload, root.end))
                                kinds = {b.kind for b in child}
                                if tag == b'moov' and not {b'mvhd', b'trak'} <= kinds:
                                    continue
                                if tag == b'moof' and not {b'mfhd', b'traf'} <= kinds:
                                    continue
                            except (FormatError, StopIteration):
                                continue
                            if tag == b'moov' and b'mvex' not in kinds:
                                mdigest = hashlib.sha256(raw).hexdigest()
                                error = summary = None
                                try:
                                    summary = json_text(parse_movie(raw).summary())
                                except FormatError as e:
                                    error = str(e)
                                case.db.execute('INSERT OR IGNORE INTO movies VALUES(?,?,?,?)',
                                                (mdigest, raw, summary, error))
                    elif tag == b'mdat' and available <= header:
                        continue
                    case.db.execute('''INSERT OR IGNORE INTO atoms
                        (source_id,kind,offset,size,header,available,payload_size,movie_digest)
                        VALUES(?,?,?,?,?,?,?,?)''',
                        (sid, tag.decode(), start, n, header, available, available-header, mdigest))
                    count += 1
                pool.check(sid)
                case.db.commit()
                task.update(scanned, atoms=count)
            pool.check(sid)
        scope = 'full' if scanned == row['size'] else 'available-ranges'
        case.db.execute('UPDATE sources SET sha256=?,complete=1,hash_scope=? WHERE id=?',
                        (digest.hexdigest(),scope,sid))
        case.db.commit()
    LOG.info('Indexed %s: %d candidate atoms; %d available bytes',row['path'],count,scanned)
