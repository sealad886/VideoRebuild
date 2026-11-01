"""Small, side-effect-free model for the terminal recovery front end."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import shutil
import sqlite3
import time

from .storage import SCHEMA


@dataclass
class Settings:
    case: str = ''
    input: str = ''
    image: str = ''
    allocation_map: str = ''
    deep: bool = True
    reconstruct_only: bool = True
    include_recovered: bool = False
    retry: bool = False


def _path(value: str, label: str) -> Path:
    if not value.strip():
        raise ValueError(f'{label} is required')
    if any(ord(character) < 32 for character in value):
        raise ValueError(f'{label} contains a control character')
    return Path(value).expanduser()


def build_arguments(settings: Settings, action: str) -> list[str]:
    """Build argv only; the canonical CLI owns all source and case mutations."""
    if action not in {'scan', 'run', 'recover'}:
        raise ValueError(f'Unknown recovery action: {action}')
    case = _path(settings.case, 'Case folder')
    if case.is_symlink():
        raise ValueError('Case folder must not be a symlink')
    if case.exists() and not case.is_dir():
        raise ValueError('Case folder must be a directory')
    arguments = [action, '--case', str(case)]
    if action == 'recover':
        if not (case / 'case.sqlite3').is_file():
            raise ValueError('Recover requires an existing indexed case; use Scan or Run for a new case')
        if settings.allocation_map:
            allocation = _path(settings.allocation_map, 'Allocation map')
            if not allocation.is_file():
                raise ValueError('Allocation map must be an existing file')
            arguments.extend(['--allocation-map', str(allocation)])
    else:
        if settings.allocation_map:
            raise ValueError('Allocation maps apply to Recover only; scan first, then Recover with the map')
        if not settings.input and not settings.image:
            raise ValueError('Choose a recovered input folder/file or a raw image first')
        if settings.input:
            source = _path(settings.input, 'Recovered input')
            if not (source.is_dir() or source.is_file()):
                raise ValueError('Recovered input must be an existing file or directory')
            arguments.extend(['--input', str(source)])
        if settings.image:
            image = _path(settings.image, 'Raw image')
            if not image.is_file():
                raise ValueError('Raw image must be an existing regular file, not a device')
            arguments.extend(['--image', str(image)])
    if action in {'run', 'recover'}:
        for name in ('deep', 'reconstruct_only', 'include_recovered', 'retry'):
            if getattr(settings, name):
                arguments.append('--' + name.replace('_', '-'))
    return arguments


def _display(value: str) -> str:
    return ''.join(character if character.isprintable() else ' ' for character in value)


def _tail(path: Path, lines: int = 12) -> tuple[list[str], float | None]:
    if path.is_symlink():
        raise ValueError('Refusing a symlinked case log')
    try:
        with path.open('rb') as stream:
            info = os.fstat(stream.fileno())
            start = max(0, info.st_size - 65536)
            stream.seek(start)
            data = stream.read(65536)
        values = data.decode('utf-8', errors='replace').splitlines()
        if start and values:
            values.pop(0)  # The first line may start inside a UTF-8 character.
        return [_display(value) for value in values[-lines:]], info.st_mtime
    except FileNotFoundError:
        return [], None


def snapshot(case: str | Path) -> dict:
    """Read a bounded case snapshot without initializing or acquiring its writer lock.

    No attempt rows or media bytes are read. A busy case indicates an advisory
    lock holder, not a verified process identity; an idle case does not prove
    recovery completed. Every SQLite connection closes before returning.
    """
    root = Path(case).expanduser().absolute()
    result = {'case': str(root), 'error': None, 'busy': None, 'jobs': {},
              'sources': None, 'free_bytes': None, 'log_lines': [],
              'log_mtime': None, 'report_path': str(root / 'reports' / 'index.html')}
    errors = []
    ancestor = root
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    try:
        result['free_bytes'] = shutil.disk_usage(ancestor).free
    except OSError:
        pass
    if not str(case).strip():
        result['error'] = 'Choose a case folder'
        return result
    if root.is_symlink() or not root.is_dir():
        result['error'] = 'Case folder is missing, is not a directory, or is a symlink'
        return result
    try:
        result['log_lines'], result['log_mtime'] = _tail(root / 'run.log')
    except (OSError, ValueError) as error:
        errors.append(str(error))
    lock = None
    locked = False
    try:
        import fcntl
        lockpath = root / '.lock'
        if lockpath.is_symlink():
            raise ValueError('Refusing a symlinked case lock')
        try:
            lock = lockpath.open('rb')
        except FileNotFoundError:
            raise ValueError('Case lock is missing; no database snapshot available') from None
        try:
            # Retain this short shared lock while reading an idle database so a
            # canonical writer cannot start during an immutable snapshot.
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            result['busy'] = False
            locked = True
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            result['busy'] = True
        database = root / 'case.sqlite3'
        if database.is_symlink() or not database.is_file():
            raise ValueError('Case database is missing or is a symlink')
        wal, shared = Path(str(database) + '-wal'), Path(str(database) + '-shm')
        if wal.is_symlink() or shared.is_symlink():
            raise ValueError('Refusing symlinked SQLite journal files')
        if result['busy']:
            # Opening a WAL database without existing shared memory could create
            # journal files even in mode=ro. Let its writer initialize them first.
            if not wal.is_file() or not shared.is_file():
                raise ValueError('Case is busy; waiting for readable WAL metadata')
            query = '?mode=ro'
        elif wal.exists() and wal.stat().st_size:
            if not shared.is_file():
                raise ValueError('WAL shared memory is missing; database snapshot unavailable')
            query = '?mode=ro'
        else:
            # No WAL to merge and the canonical writer lock is held shared.
            # immutable avoids creating empty -wal/-shm files in the case.
            query = '?mode=ro&immutable=1'
        deadline = time.monotonic() + 0.25
        with closing(sqlite3.connect(database.as_uri() + query, uri=True, timeout=0.05)) as connection:
            connection.execute('PRAGMA query_only=ON')
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version != SCHEMA:
                raise ValueError(f'Unsupported case schema {version}; expected {SCHEMA}')
            result['jobs'] = {f'{kind}/{status}': count for kind, status, count in
                              connection.execute('SELECT kind,status,COUNT(*) FROM jobs GROUP BY kind,status')}
            result['sources'] = connection.execute('SELECT COUNT(*) FROM sources WHERE complete=1').fetchone()[0]
    except (OSError, ValueError, sqlite3.Error) as error:
        errors.append(str(error))
    finally:
        if lock is not None:
            if locked:
                fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
    result['error'] = '; '.join(errors) if errors else None
    return result
