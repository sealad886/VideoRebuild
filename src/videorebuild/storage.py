"""Case database, source safety, range reads, and streaming signature scanning."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
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

from .bmff import MAX_METADATA, FormatError, boxes, parse_movie

LOG = logging.getLogger(__name__)
SCHEMA = 1
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
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(value, f, indent=2, sort_keys=True, ensure_ascii=True)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        while data := f.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def regular(path: Path) -> os.stat_result:
    s = path.stat()
    if not stat.S_ISREG(s.st_mode):
        raise SafetyError(f'Not a regular file: {path}. Use a disk image, not a live block device.')
    return s


def within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class Case:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.db: sqlite3.Connection | None = None
        self.lock = None

    def __enter__(self) -> 'Case':
        import fcntl
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Advisory lock survives crashes without a stale-lock deletion operation.
        lockpath = self.root / '.lock'
        if lockpath.is_symlink():
            raise SafetyError('case lock must not be a symlink')
        self.lock = lockpath.open('a+b')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            self.lock.close()
            raise SafetyError('Another process is using this case') from e
        try:
            dbpath = self.root / 'case.sqlite3'
            if dbpath.is_symlink():
                raise SafetyError('case database must not be a symlink')
            self.db = sqlite3.connect(dbpath)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA journal_mode=WAL')
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA):
                raise SafetyError(f'Unsupported database schema {version}')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, dev INTEGER NOT NULL,
                    ino INTEGER NOT NULL, sha256 TEXT, complete INTEGER NOT NULL DEFAULT 0);
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
                CREATE TABLE IF NOT EXISTS results (
                    movie_digest TEXT PRIMARY KEY REFERENCES movies(digest),
                    status TEXT NOT NULL, detail TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY, movie_digest TEXT NOT NULL,
                    mapping_digest TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
                    detail TEXT NOT NULL, UNIQUE(movie_digest,mapping_digest));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL,
                    detail TEXT NOT NULL);
            ''')
            self.db.execute(f'PRAGMA user_version={SCHEMA}')
            self.db.commit()
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

    def result(self, digest: str, status: str, detail: dict) -> None:
        self.db.execute('''INSERT INTO results VALUES(?,?,?,?) ON CONFLICT(movie_digest)
                           DO UPDATE SET status=excluded.status,detail=excluded.detail,updated=excluded.updated''',
                        (digest, status, json_text(detail), time.time()))
        self.db.commit()

    def sources(self) -> dict[int, dict]:
        return {r['id']: dict(r) for r in self.db.execute('SELECT * FROM sources WHERE complete=1 ORDER BY id')}

    def invalidate_results(self) -> None:
        # A larger evidence set can reveal a second valid pairing. Do not leave
        # a prior uniqueness claim published after even a scan-only operation.
        rows = self.db.execute("SELECT * FROM results WHERE status != 'stale'").fetchall()
        for row in rows:
            detail = json.loads(row['detail'])
            detail['previous_status'] = row['status']
            detail['reason'] = 'source_catalog_changed; run recover again'
            detail.pop('run_key', None)
            detail['outputs'] = []
            for suffix in ('.mp4', '.json'):
                p = self.root / 'reconstructed' / (row['movie_digest'][:24] + suffix)
                if p.is_symlink():
                    raise SafetyError('Published artifact is a symlink')
                if p.exists():
                    p.unlink()  # Only the publication name; candidate bytes remain.
            self.db.execute('UPDATE results SET status=?,detail=?,updated=? WHERE movie_digest=?',
                            ('stale', json_text(detail), time.time(), row['movie_digest']))
        self.db.commit()


class ReaderPool:
    """Bounded file descriptor cache; sources are opened read-only and checked."""
    def __init__(self, sources: dict[int, dict], limit: int = 32):
        self.sources = sources
        self.limit = limit
        self.handles = OrderedDict()

    def close(self) -> None:
        for f in self.handles.values():
            f.close()
        self.handles.clear()

    def __enter__(self) -> 'ReaderPool':
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def check(self, sid: int) -> None:
        row = self.sources[sid]
        s = regular(Path(row['path']))
        if (s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino) != (row['size'], row['mtime_ns'], row['dev'], row['ino']):
            raise SafetyError(f'Source changed since indexing: {row["path"]}. Start a new case for changed evidence.')

    def handle(self, sid: int):
        self.check(sid)
        if sid in self.handles:
            self.handles.move_to_end(sid)
            return self.handles[sid]
        f = Path(self.sources[sid]['path']).open('rb')
        # Verify the opened inode too, not only the path checked before open().
        s = os.fstat(f.fileno())
        row = self.sources[sid]
        if (s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino) != (row['size'], row['mtime_ns'], row['dev'], row['ino']):
            f.close()
            raise SafetyError('Source changed during open')
        self.handles[sid] = f
        if len(self.handles) > self.limit:
            self.handles.popitem(last=False)[1].close()
        return f

    def read(self, sid: int, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.sources[sid]['size']:
            raise FormatError('source range exceeds file bounds')
        f = self.handle(sid)
        f.seek(offset)
        data = f.read(size)
        if len(data) != size:
            raise SafetyError(f'Short read from {self.sources[sid]["path"]}')
        return data


def iter_blocks(path: Path, overlap: int = 16) -> Iterator[tuple[int, bytes, int]]:
    """Yield a block with lookahead, and exclusive search-start boundary.

Lookahead avoids lost signatures at buffer boundaries. Only the first `owned`
bytes own signature starts, so no duplicate hits are emitted.
"""
    with path.open('rb') as f:
        pos = 0
        data = f.read(BLOCK + overlap)
        while data:
            owned = min(BLOCK, len(data))
            yield pos, data, owned
            pos += owned
            if len(data) <= BLOCK:
                break
            data = data[BLOCK:] + f.read(BLOCK)


def input_files(roots: list[Path], case_root: Path, all_files: bool) -> Iterator[Path]:
    seen = set()
    for root in roots:
        root = root.expanduser().resolve()
        if not root.exists():
            raise SafetyError(f'Input does not exist: {root}')
        if root.is_dir():
            if within(case_root, root) or within(root, case_root):
                raise SafetyError('Case and input directories must not contain one another')
            def onerror(error: OSError) -> None:
                raise SafetyError(f'Cannot enumerate input directory: {error}') from error
            def walk_paths():
                for folder, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
                    dirs.sort()
                    for name in sorted(names):
                        yield Path(folder)/name
            paths = walk_paths()
        else:
            paths = iter([root])
        for p in paths:
            if p.is_symlink():
                LOG.warning('Skipping symlink: %s', p)
                continue
            if not all_files and root.is_dir() and p.suffix.lower() not in EXTENSIONS:
                continue
            p = p.resolve()
            if within(p, case_root):
                raise SafetyError('An input is inside the case directory')
            regular(p)
            if str(p) not in seen:
                seen.add(str(p))
                yield p


def _candidate_header(f, offset: int, size: int) -> tuple[int, bytes, int] | None:
    if offset < 0 or offset + 8 > size:
        return None
    f.seek(offset)
    raw = f.read(16)
    n, kind = struct.unpack_from('>I4s', raw)
    header = 8
    if n == 1:
        if len(raw) < 16:
            return None
        n = struct.unpack_from('>Q', raw, 8)[0]
        header = 16
    elif n == 0:
        n = size - offset
    if n < header or n > 16 * 1024**4:
        return None
    return n, kind, header


def scan_source(case: Case, path: Path, kind: str) -> None:
    s = regular(path)
    prev = case.db.execute('SELECT * FROM sources WHERE path=?', (str(path),)).fetchone()
    identity = (s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino)
    if prev:
        if identity != (prev['size'], prev['mtime_ns'], prev['dev'], prev['ino']):
            raise SafetyError(f'Previously indexed source changed: {path}; use a new case')
        if prev['kind'] != kind:
            raise SafetyError(f'The same input was supplied as both image and recovered file: {path}')
        if prev['complete']:
            LOG.info('Already indexed: %s', path)
            return
        sid = prev['id']
        case.db.execute('DELETE FROM atoms WHERE source_id=?', (sid,))
    else:
        case.invalidate_results()
        cur = case.db.execute('INSERT INTO sources(path,kind,size,mtime_ns,dev,ino) VALUES(?,?,?,?,?,?)',
                              (str(path), kind, *identity))
        sid = cur.lastrowid
    case.db.commit()
    digest = hashlib.sha256()
    count = 0
    last_log = time.monotonic()
    with path.open('rb') as random:
        for base, data, owned in iter_blocks(path):
            digest.update(data[:owned])
            for match in SIGNATURES.finditer(data):
                # Type field may be in lookahead; ownership belongs to the size field.
                start = base + match.start() - 4
                if not base <= start < base + owned:
                    continue
                info = _candidate_header(random, start, s.st_size)
                if info is None:
                    continue
                n, tag, header = info
                available = min(n, s.st_size - start)
                mdigest = None
                if tag in {b'moov', b'moof', b'ftyp'}:
                    cap = 4096 if tag == b'ftyp' else MAX_METADATA
                    if available != n or n > cap:
                        continue
                    random.seek(start)
                    raw = random.read(n)
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
                        if tag == b'moov':
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
            case.db.commit()
            if time.monotonic() - last_log > 5:
                LOG.info('Scanning %s: %.1f%%; %d atom candidates', path.name,
                         100 * min(base+owned, s.st_size) / max(1, s.st_size), count)
                last_log = time.monotonic()
    now = path.stat()
    if (now.st_size, now.st_mtime_ns, now.st_dev, now.st_ino) != identity:
        raise SafetyError(f'Source changed during scan: {path}')
    case.db.execute('UPDATE sources SET sha256=?,complete=1 WHERE id=?', (digest.hexdigest(), sid))
    case.db.commit()
    LOG.info('Indexed %s: %d candidate atoms', path, count)


def scan(case: Case, roots: list[Path], images: list[Path], all_files: bool = False) -> None:
    count = 0
    for p in input_files(roots, case.root, all_files):
        scan_source(case, p, 'file')
        count += 1
    for p in images:
        p = p.expanduser().resolve()
        if within(p, case.root):
            raise SafetyError('Source image must be outside the case')
        scan_source(case, p, 'image')
        count += 1
    if not count:
        raise SafetyError('No matching input files; use --all-files for unknown extensions')
    # Adding evidence invalidates prior uniqueness claims. Preserved attempt files
    # are retained, but results must be recomputed against the expanded source set.
    case.event('scan_complete', {'inputs': count})
