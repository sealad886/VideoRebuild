"""Missing-only restoration of generated case files from exact saved evidence."""
from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Callable

from .storage import SafetyError, file_sha256

LOG = logging.getLogger(__name__)


def safe_case_path(case, name: str | Path) -> Path:
    """Confine a generated file to the case, refusing existing symlink components."""
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise SafetyError(f'Unsafe generated case path: {name}')
    root = Path(case.root)
    current = root
    for component in (None, *relative.parts):
        if component is not None:
            current /= component
        try:
            state = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(state.st_mode):
            raise SafetyError(f'Generated case path is a symlink: {current}')
        if current != root / relative and not stat.S_ISDIR(state.st_mode):
            raise SafetyError(f'Generated case parent is not a directory: {current}')
    return root / relative


def _stamp(state):
    return state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns


def _verify(path, digest, size):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise SafetyError(f'Generated case file is not regular: {path}')
    if size is not None and before.st_size != size:
        raise SafetyError(f'Generated case file changed: {path}')
    if file_sha256(path) != digest or _stamp(path.lstat()) != _stamp(before):
        raise SafetyError(f'Generated case file changed: {path}')
    return before


def _remove_owned(path, owned):
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == owned:
        path.unlink()


def _install(source, destination):
    """Install without overwriting, with bounded copying on filesystems lacking links."""
    try:
        os.link(source, destination)
        return
    except FileExistsError as error:
        raise SafetyError(f'Generated case file appeared during restoration: {destination}') from error
    except OSError as error:
        if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOSYS,
                              errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
    owned = None
    try:
        with destination.open('xb') as output:
            state = os.fstat(output.fileno())
            owned = state.st_dev, state.st_ino
            with source.open('rb') as stream:
                shutil.copyfileobj(stream, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
    except BaseException as error:
        if owned is not None:
            _remove_owned(destination, owned)
        if isinstance(error, FileExistsError):
            raise SafetyError(f'Generated case file appeared during restoration: {destination}') from error
        raise


def ensure_generated_file(case, relative_path: str | Path, expected_sha256: str, *,
                          peers=(), rebuild: Callable[[Path], object] | None = None,
                          expected_size: int | None = None) -> Path:
    """Verify a retained file or recreate an absent one without altering saved decisions.

    Rebuilders write to an absent private staging path and own source/provenance
    checks. Every restored byte must match the previously committed hash.
    """
    if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64 or
            any(character not in '0123456789abcdefABCDEF' for character in expected_sha256)):
        raise SafetyError('Missing or invalid generated file SHA-256 evidence')
    if expected_size is not None and (type(expected_size) is not int or expected_size < 0):
        raise SafetyError('Invalid generated file size evidence')
    expected_sha256 = expected_sha256.lower()
    destination = safe_case_path(case, relative_path)
    if destination.exists():
        _verify(destination, expected_sha256, expected_size)
        return destination
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe_case_path(case, relative_path)
    for name in peers:
        source = safe_case_path(case, name)
        if not source.exists():
            continue
        before = _verify(source, expected_sha256, expected_size)
        _install(source, destination)
        owned = destination.lstat()
        try:
            # Creating a hard link changes ctime without changing content.
            if _stamp(source.lstat())[:4] != _stamp(before)[:4]:
                raise SafetyError(f'Generated case peer changed during restoration: {source}')
            _verify(destination, expected_sha256, expected_size)
        except BaseException:
            _remove_owned(destination, (owned.st_dev, owned.st_ino))
            raise
        LOG.info('Restored missing generated file: %s', destination)
        return destination
    if rebuild is None:
        raise SafetyError(f'Missing generated case file has no reconstruction evidence: {destination}')
    with tempfile.TemporaryDirectory(prefix='.videorebuild-restore-', dir=destination.parent) as directory:
        staged = Path(directory) / 'artifact'
        rebuild(staged)
        _verify(staged, expected_sha256, expected_size)
        with staged.open('rb') as stream:
            os.fsync(stream.fileno())
        safe_case_path(case, relative_path)
        _install(staged, destination)
    LOG.info('Restored missing generated file: %s', destination)
    return destination
