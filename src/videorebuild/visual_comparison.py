"""Bounded, local visual suggestions; never evidence for excluding recovered media.

Fingerprints deliberately have no identity claim. They compare a low-resolution,
1 Hz grayscale sequence, so unrelated surveillance recordings can still resemble
one another. Only encoded-content comparison can confirm representation.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from contextlib import closing
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile

from .paths import is_ignored_path
from .progress import phase

REVISION = 2
VIDEO_DEMUXERS = 'mov,matroska,avi,m4v,mpeg,mpegts,mpegvideo,asf'
FRAME_WIDTH = 9
FRAME_HEIGHT = 8
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT
MAX_FRAMES = 300
MAX_CANDIDATES = 5
MAX_HASH_DISTANCE = 6


class VisualResults(dict):
    """Candidate mapping plus coverage information for the comparison report."""

    def __init__(self):
        super().__init__()
        self.errors: list[dict] = []
        self.limitations: list[str] = [
            'Visual matches are review suggestions, not proof that a recording exists in the archive.',
            f'Visual comparison samples grayscale frames at 1 Hz, at most {MAX_FRAMES} seconds per video; '
            'audio, other video tracks and unsampled detail are not compared.',
            'Static or very short recordings do not provide enough temporal evidence for a visual suggestion.',
        ]
        self.complete = True


@dataclass(frozen=True)
class Fingerprint:
    frames: tuple[bytes, ...]
    truncated: bool = False

    @property
    def hashes(self) -> tuple[int, ...]:
        return tuple(_dhash(frame) for frame in self.frames)


def _dhash(frame: bytes) -> int:
    value = 0
    for row in range(FRAME_HEIGHT):
        start = row * FRAME_WIDTH
        for column in range(FRAME_WIDTH - 1):
            value = (value << 1) | (frame[start + column] > frame[start + column + 1])
    return value


def _stamp(path: Path) -> tuple[int, ...]:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Visual input must be a regular file, not a symlink')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _decode(path: Path, executable: str, timeout: float) -> Fingerprint:
    # The extra frame detects the sampling limit without a second probe process.
    args = [executable, '-hide_banner', '-loglevel', 'error', '-nostdin', '-xerror',
            '-threads', '1', '-protocol_whitelist', 'file',
            '-format_whitelist', VIDEO_DEMUXERS, '-i', str(path),
            '-map', '0:v:0', '-an', '-sn', '-dn', '-t', str(MAX_FRAMES + 1),
            '-vf', f'fps=1:start_time=0:round=near,scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=area,format=gray',
            '-frames:v', str(MAX_FRAMES + 1), '-threads', '1', '-f', 'rawvideo',
            '-pix_fmt', 'gray', 'pipe:1']
    environment = os.environ.copy()
    environment.pop('FFREPORT', None)
    environment['AV_LOG_FORCE_NOCOLOR'] = '1'
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                   env=environment, start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except BaseException:
            process.kill()
            process.wait()
            raise
        out.seek(0)
        raw = out.read((MAX_FRAMES + 1) * FRAME_BYTES + 1)
        err.seek(0)
        error = err.read(4096).decode('utf-8', 'replace').strip()
    if process.returncode:
        raise ValueError(f'Visual decoding failed (exit {process.returncode}): {error}')
    if len(raw) > (MAX_FRAMES + 1) * FRAME_BYTES or len(raw) % FRAME_BYTES:
        raise ValueError('Visual decoder returned malformed or oversized frame data')
    if not raw:
        raise ValueError('No video frames could be sampled')
    frames = tuple(raw[i:i + FRAME_BYTES] for i in range(0, len(raw), FRAME_BYTES))
    return Fingerprint(frames[:MAX_FRAMES], len(frames) > MAX_FRAMES)


def _safe_cache(path: Path) -> None:
    for suffix in ('', '-journal', '-wal', '-shm'):
        candidate = path.with_name(path.name + suffix)
        if candidate.is_symlink():
            raise ValueError('Visual cache must not be a symlink')
        if candidate.exists():
            info = candidate.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Visual cache must be a regular file with one hard link')


def _fingerprint(path: Path, db: sqlite3.Connection, executable: str,
                 tool_identity: str, timeout: float) -> Fingerprint:
    before = _stamp(path)
    identity = json.dumps([REVISION, before, tool_identity], separators=(',', ':'))
    row = db.execute('SELECT identity, data, truncated FROM visual_files WHERE path=?',
                     (str(path),)).fetchone()
    if row is not None and row[0] == identity:
        data = bytes(row[1])
        if 0 < len(data) <= MAX_FRAMES * FRAME_BYTES and len(data) % FRAME_BYTES == 0:
            return Fingerprint(tuple(data[i:i + FRAME_BYTES]
                                     for i in range(0, len(data), FRAME_BYTES)), bool(row[2]))
    fingerprint = _decode(path, executable, timeout)
    if _stamp(path) != before:
        raise ValueError('Video changed while visual fingerprints were being generated')
    db.execute('INSERT OR REPLACE INTO visual_files VALUES (?, ?, ?, ?)',
               (str(path), identity, b''.join(fingerprint.frames), int(fingerprint.truncated)))
    db.commit()
    return fingerprint


def _active(hashes: tuple[int, ...]) -> bool:
    if len(hashes) < 3:
        return False
    # Identical/static scenes cannot nominate a match. The temporal threshold
    # intentionally misses subtle motion rather than imply identity from scenery.
    changes = [(left ^ right).bit_count() for left, right in zip(hashes, hashes[1:])]
    return len(set(hashes)) >= 3 and sum(change >= 3 for change in changes) >= 2 and sum(changes) >= 12


class _HashTree:
    """Small Hamming-distance index over recovered anchors, not all archive frames."""

    def __init__(self):
        self.root = None
        self.values: dict[int, list[tuple[str, int]]] = defaultdict(list)

    def add(self, value: int, path: str, index: int) -> None:
        self.values[value].append((path, index))
        if len(self.values[value]) > 1:
            return
        if self.root is None:
            self.root = (value, {})
            return
        node = self.root
        while True:
            distance = (value ^ node[0]).bit_count()
            if distance not in node[1]:
                node[1][distance] = (value, {})
                return
            node = node[1][distance]

    def find(self, value: int):
        if self.root is None:
            return
        pending = [self.root]
        while pending:
            node = pending.pop()
            distance = (value ^ node[0]).bit_count()
            if distance <= MAX_HASH_DISTANCE:
                yield from self.values[node[0]]
            low, high = distance - MAX_HASH_DISTANCE, distance + MAX_HASH_DISTANCE
            pending.extend(child for edge, child in node[1].items() if low <= edge <= high)


def _sequence_score(recovered: Fingerprint, archive: Fingerprint,
                    recovered_hashes: tuple[int, ...], archive_hashes: tuple[int, ...],
                    offset: int) -> float | None:
    count = len(recovered_hashes)
    if offset < 0 or offset + count > len(archive_hashes):
        return None
    distances = [(left ^ right).bit_count() for left, right in
                 zip(recovered_hashes, archive_hashes[offset:offset + count])]
    if max(distances) > 16 or sum(distances) / count > 6:
        return None
    # Grayscale pixels retain coarse intensity information that dHash discards.
    # Remove each frame's mean to tolerate a moderate global brightness change.
    error = 0.0
    for i, frame in enumerate(recovered.frames):
        other = archive.frames[offset + i]
        delta = (sum(frame) - sum(other)) / FRAME_BYTES
        error += sum(abs(left - right - delta) for left, right in zip(frame, other)) / FRAME_BYTES
    error /= count
    if error > 10:
        return None
    return round(max(0.0, 1 - (sum(distances) / count) / 64 - error / 255), 4)


def find_visual_candidates(recovered_paths: list[Path], archive_paths: list[Path], *,
                           cache_dir: Path, timeout: float = 60.0) -> VisualResults:
    """Return temporal grayscale similarities without classifying any A as represented.

    ``result.errors`` records per-file decoding failures. ``complete`` is false
    when a file fails, changes during extraction, or exceeds the sampling limit.
    Short/static sequences produce no suggestions, which never proves absence.
    Successful fingerprints commit one file at a time so interrupted runs resume.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Visual timeout must be finite and positive')
    results = VisualResults()
    recovered_paths = [Path(path).absolute() for path in recovered_paths if not is_ignored_path(path)]
    archive_paths = [Path(path).absolute() for path in archive_paths if not is_ignored_path(path)]
    for path in recovered_paths:
        results[str(path)] = []
    if not recovered_paths:
        return results
    executable = shutil.which('ffmpeg')
    if executable is None:
        results.errors.append({'path': None, 'error': 'ffmpeg is required for visual comparison'})
        results.complete = False
        return results
    tool_path = Path(executable).resolve()
    tool_info = tool_path.stat()
    tool_identity = f'{tool_path}:{tool_info.st_size}:{tool_info.st_mtime_ns}'
    cache_dir = Path(cache_dir).absolute()
    if cache_dir.is_symlink() or not cache_dir.is_dir():
        raise ValueError('Visual cache directory must exist and must not be a symlink')
    # Canonicalize platform aliases such as macOS /var -> /private/var. The
    # comparison coordinator validates containment before creating this folder.
    cache_dir = cache_dir.resolve()
    cache_path = cache_dir / 'visual-cache.sqlite3'
    _safe_cache(cache_path)
    recovered = {}
    tree = _HashTree()
    failures = 0
    skipped = 0
    limited = 0
    with closing(sqlite3.connect(cache_path)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS visual_files '
                   '(path TEXT PRIMARY KEY, identity TEXT NOT NULL, data BLOB NOT NULL, truncated INTEGER NOT NULL)')

        def load(path):
            nonlocal failures, limited
            try:
                value = _fingerprint(path, db, executable, tool_identity, timeout)
                if value.truncated:
                    limited += 1
                    results.complete = False
                return value
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                failures += 1
                results.errors.append({'path': str(path), 'error': str(error)})
                results.complete = False
                return None

        with phase('Fingerprint recovered video for visual review', total=len(recovered_paths), unit='videos') as task:
            for path in recovered_paths:
                fingerprint = load(path)
                if fingerprint is not None:
                    hashes = fingerprint.hashes
                    if _active(hashes):
                        recovered[str(path)] = (fingerprint, hashes)
                        # Three spread anchors improve nomination despite 1 Hz phase
                        # shifts. Every nominated sequence is checked in full below.
                        for index in sorted({0, len(hashes) // 2, len(hashes) - 1}):
                            tree.add(hashes[index], str(path), index)
                    else:
                        skipped += 1
                task.advance(errors=failures, low_information=skipped, limited=limited)
        if recovered:
            with phase('Fingerprint archive video for visual review', total=len(archive_paths), unit='videos') as task:
                for path in archive_paths:
                    fingerprint = load(path)
                    if fingerprint is not None:
                        hashes = fingerprint.hashes
                        seen = set()
                        for index, value in enumerate(hashes):
                            for recovered_path, anchor in tree.find(value):
                                offset = index - anchor
                                key = recovered_path, offset
                                if key in seen:
                                    continue
                                seen.add(key)
                                target, target_hashes = recovered[recovered_path]
                                score = _sequence_score(target, fingerprint, target_hashes, hashes, offset)
                                if score is None:
                                    continue
                                candidates = results[recovered_path]
                                previous = next((item for item in candidates if item['archive_path'] == str(path)), None)
                                if previous is not None and previous['score'] >= score:
                                    continue
                                if previous is not None:
                                    candidates.remove(previous)
                                candidates.append({'archive_path': str(path), 'method': 'temporal_grayscale_similarity',
                                    'score': score, 'offset_seconds': offset,
                                    'sampled_seconds': len(target_hashes),
                                    'limitations': 'Review required: sparse grayscale similarity does not prove identity; '
                                        'audio and unsampled content are not compared.' + (
                                        ' Sampling was limited to the first 300 seconds.'
                                        if target.truncated or fingerprint.truncated else '')})
                                candidates.sort(key=lambda item: (-item['score'], item['archive_path']))
                                del candidates[MAX_CANDIDATES:]
                    task.advance(errors=failures, low_information=skipped, limited=limited)
    if skipped:
        results.limitations.append(f'{skipped} recovered videos had too few frames or insufficient temporal variation for suggestions.')
    if limited:
        results.limitations.append(f'{limited} videos exceeded the first-{MAX_FRAMES}-seconds visual sampling limit.')
    results.limitations.append(f'At most {MAX_CANDIDATES} visual candidates per recovered video are reported.')
    return results
