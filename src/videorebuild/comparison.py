"""Read-only video-content comparison, with fresh proof for every positive match.

The archive cache contains metadata, never an assertion that a recovered video
is present. Only complete current file equality or complete ordered encoded
video-sample equality can remove a recovered file from the attention list.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
import csv
from datetime import datetime, timezone
import fcntl
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile
import zlib

from .bmff import FormatError, MAX_METADATA, MAX_SAMPLES, parse_movie
from .fragmented import MAX_INIT, parse_fragment, parse_initialization
from .paths import is_ignored_path
from .progress import phase
from .storage import SafetyError, within

VIDEO_EXTENSIONS = frozenset({'.mp4', '.mov', '.m4v', '.mkv', '.avi', '.webm',
                              '.mts', '.m2ts', '.ts', '.mpeg', '.mpg', '.3gp', '.wmv'})
REPRESENTED = frozenset({'represented_exact', 'represented_encoded'})
MAX_FRAGMENT_PAIR = 64 * 1024 * 1024
_CACHE_VERSION = 1


def _stamp(path: Path) -> tuple[int, ...]:
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(value.st_mode):
        raise SafetyError(f'Not a regular, non-symlink video file: {path}')
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_mode)


def _check(path: Path, expected) -> None:
    if tuple(expected) != _stamp(path):
        raise SafetyError(f'Video changed during comparison: {path}')


def _overlapping_directories(left: Path, right: Path) -> bool:
    if within(left, right) or within(right, left):
        return True
    # Resolved paths can still use different spellings on case-insensitive volumes.
    for child, parent in ((left, right), (right, left)):
        try:
            parent_info = parent.stat()
        except FileNotFoundError:
            continue
        for ancestor in (child, *child.parents):
            try:
                if os.path.samestat(ancestor.stat(), parent_info):
                    return True
            except FileNotFoundError:
                continue
    return False


def _inventory(root: Path) -> tuple[list[Path], list[dict], int]:
    files, errors = [], []
    skipped = 0

    def onerror(error):
        errors.append({'path': str(error.filename or root), 'error': str(error)})

    for folder, directories, names in os.walk(root, followlinks=False, onerror=onerror):
        retained = []
        for name in sorted(directories):
            path = Path(folder) / name
            if is_ignored_path(name) or path.is_symlink():
                skipped += 1
            else:
                retained.append(name)
        directories[:] = retained
        for name in sorted(names):
            path = Path(folder) / name
            if is_ignored_path(name) or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if path.is_symlink():
                skipped += 1
                continue
            files.append(path)
    return files, errors, skipped


def _top_boxes(handle, size):
    position = 0
    count = 0
    while position < size:
        count += 1
        if count > MAX_SAMPLES:
            raise FormatError('MP4 top-level box count exceeds comparison limit')
        if size - position < 8:
            raise FormatError('trailing bytes outside MP4 boxes')
        handle.seek(position)
        header = handle.read(min(16, size-position))
        if len(header) < 8:
            raise FormatError('short MP4 box header')
        length, kind = struct.unpack_from('>I4s', header)
        width = 8
        if length == 1:
            if len(header) < 16:
                raise FormatError('short extended MP4 box header')
            length = struct.unpack_from('>Q', header, 8)[0]
            width = 16
        elif length == 0:
            length = size - position
        if length < width or length > size - position:
            raise FormatError('MP4 box exceeds current file boundary')
        yield position, length, kind, width
        position += length


def _read(handle, offset, size):
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise FormatError('short read while comparing video metadata')
    return data


def _media(path: Path) -> dict:
    """Read bounded MP4 metadata; ordinary mdat payloads are sought over."""
    identity = _stamp(path)
    with path.open('rb') as handle:
        roots = list(_top_boxes(handle, identity[2]))
        moovs = [box for box in roots if box[2] == b'moov']
        if len(moovs) != 1:
            raise FormatError('unsupported media: expected one complete MP4 moov')
        start, length, _, _ = moovs[0]
        if length > MAX_METADATA:
            raise FormatError('MP4 metadata exceeds comparison limit')
        raw = _read(handle, start, length)
        fragmented = any(box[2] == b'moof' for box in roots)
        tracks = []
        if not fragmented:
            movie = parse_movie(raw)
            mdats = [(a+h, a+n) for a, n, kind, h in roots if kind == b'mdat']
            for track in movie.tracks:
                if track.handler != b'vide':
                    continue
                samples = []
                for chunk in track.chunks:
                    position = chunk.offset
                    for size in chunk.sizes:
                        if not any(a <= position and position+size <= b for a, b in mdats):
                            raise FormatError('video sample is outside a complete local mdat')
                        samples.append([position, size])
                        position += size
                tracks.append({'codec': track.codec.decode('ascii'),
                               'config': track.config_hash, 'samples': samples})
        else:
            ftyps = [box for box in roots if box[2] == b'ftyp']
            if len(ftyps) != 1 or ftyps[0][1]+length > MAX_INIT:
                raise FormatError('fragmented initialization exceeds comparison limit')
            init = parse_initialization(_read(handle, ftyps[0][0], ftyps[0][1])+raw)
            by_id = {tid: {'codec': track.codec, 'config': track.config_sha256,
                          'samples': []} for tid, track in init.tracks.items()
                     if track.handler == 'vide'}
            sample_count = 0
            for index, box in enumerate(roots):
                position, length, kind, _ = box
                if kind != b'moof':
                    continue
                if index+1 >= len(roots) or roots[index+1][2] != b'mdat':
                    raise FormatError('fragmented moof has no adjacent complete mdat')
                pair_length = length+roots[index+1][1]
                if pair_length > MAX_FRAGMENT_PAIR:
                    raise FormatError('fragment pair exceeds comparison buffer limit')
                # The recovery parser checks sample addressing and framing. Read
                # one bounded pair at a time, without decoding its video frames.
                fragment = parse_fragment(_read(handle, position, pair_length), 0, init)
                sample_count += len(fragment.samples)
                if sample_count > MAX_SAMPLES:
                    raise FormatError('fragmented sample count exceeds comparison limit')
                for sample in fragment.samples:
                    if sample.track_id in by_id:
                        by_id[sample.track_id]['samples'].append(
                            [position+sample.offset, sample.size])
            tracks = list(by_id.values())
        if not tracks or any(not track['samples'] for track in tracks):
            raise FormatError('no complete supported video samples')
    _check(path, identity)
    return {'stamp': identity, 'tracks': tracks}


def _keys(media: dict) -> list[list[tuple[tuple, int]]]:
    result = []
    for track in media['tracks']:
        sizes = [sample[1] for sample in track['samples']]
        width = min(4, len(sizes))
        positions = sorted({0, (len(sizes)-width)//2, len(sizes)-width})
        result.append([((track['codec'], track['config'], width,
                         tuple(sizes[position:position+width])), position)
                       for position in positions])
    return result


def _safe_output(path: Path, *, directory: bool = False) -> None:
    for candidate in [path, *path.parents]:
        if candidate.is_symlink():
            raise SafetyError(f'Comparison output must not contain symlinks: {candidate}')
    if not directory and path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SafetyError(f'Comparison output must be a regular file with one hard link: {path}')


def _atomic_write(path: Path, content: str) -> None:
    _safe_output(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', newline='',
                                         dir=path.parent, prefix='.comparison-',
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _safe_output(path)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _same_range(left, left_offset, right, right_offset, size):
    left.seek(left_offset)
    right.seek(right_offset)
    while size:
        count = min(size, 1024*1024)
        one, two = left.read(count), right.read(count)
        if len(one) != count or len(two) != count:
            raise SafetyError('Short read during current video-content proof')
        if one != two:
            return False
        size -= count
    return True


def _same_file(left: Path, right: Path, left_stamp, right_stamp) -> bool:
    _check(left, left_stamp)
    _check(right, right_stamp)
    if left_stamp[2] != right_stamp[2]:
        return False
    with left.open('rb') as first, right.open('rb') as second:
        equal = _same_range(first, 0, second, 0, left_stamp[2])
    _check(left, left_stamp)
    _check(right, right_stamp)
    return equal


def _track_matches(left, right, start, source, target):
    if left['codec'] != right['codec'] or left['config'] != right['config']:
        return False
    samples, other = left['samples'], right['samples']
    if start < 0 or start+len(samples) > len(other):
        return False
    if any(a[1] != b[1] for a, b in zip(samples, other[start:start+len(samples)])):
        return False
    # Cheap current-byte anchors reject size collisions before reading the span.
    for index in sorted({0, len(samples)//2, len(samples)-1}):
        a, b = samples[index], other[start+index]
        if not _same_range(source, a[0], target, b[0], a[1]):
            return False
    return all(_same_range(source, a[0], target, b[0], a[1])
               for a, b in zip(samples, other[start:start+len(samples)]))


def _represented(left_path, left, right_path, right, primary_track, primary_start):
    """Every A video track needs its own complete, ordered B sample span."""
    _check(left_path, left['stamp'])
    _check(right_path, right['stamp'])
    matches = []
    with left_path.open('rb') as source, right_path.open('rb') as target:
        for index, track in enumerate(left['tracks']):
            choices = []
            for other_index, other in enumerate(right['tracks']):
                if index == 0 and other_index != primary_track:
                    continue
                starts = [primary_start] if index == 0 else range(
                    len(other['samples'])-len(track['samples'])+1)
                for start in starts:
                    if _track_matches(track, other, start, source, target):
                        choices.append((other_index, start))
            if not choices:
                return None
            matches.append(choices)

    def assign(index, used, spans):
        if index == len(matches):
            return spans
        for other_index, start in matches[index]:
            if other_index not in used:
                found = assign(index+1, used | {other_index}, spans+[
                    {'recovered_track': index, 'archive_track': other_index,
                     'archive_sample_start': start,
                     'samples': len(left['tracks'][index]['samples'])}])
                if found is not None:
                    return found
        return None

    result = assign(0, set(), [])
    _check(left_path, left['stamp'])
    _check(right_path, right['stamp'])
    return result


def _csv(rows: list[dict]) -> str:
    output = io.StringIO(newline='')
    fields = ['path', 'status', 'archive_path', 'method', 'detail', 'visual_candidates']
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        values = dict(row)
        values['visual_candidates'] = json.dumps(row.get('visual_candidates', []))
        writer.writerow(values)
    return output.getvalue()


def compare_directories(recovered: Path, archive: Path, output: Path, *,
                        visual: bool = False, timeout: float = 60.0) -> dict:
    """Report which A videos lack complete current encoded-content proof in B.

    Visual similarities always remain review candidates. A no-match result is
    relative to the indexed supported files, and does not establish absence of
    transcoded, cropped, differently packetized or inaccessible archive content.
    """
    started = datetime.now(timezone.utc).isoformat()
    if not math.isfinite(timeout) or timeout <= 0:
        raise SafetyError('Comparison timeout must be finite and positive')
    roots = []
    for label, value in [('Recovered', recovered), ('Archive', archive)]:
        value = Path(value).expanduser()
        if value.is_symlink() or not value.is_dir():
            raise SafetyError(f'{label} input must be an existing non-symlink directory: {value}')
        roots.append(value.resolve())
    recovered, archive = roots
    if _overlapping_directories(recovered, archive):
        raise SafetyError('Recovered and archive folders must be separate, non-overlapping directories')
    output = Path(output).expanduser().absolute()
    _safe_output(output, directory=True)
    output = output.resolve()
    for root in roots:
        if _overlapping_directories(output, root):
            raise SafetyError('Comparison output and input folders must not contain one another')
    output.mkdir(parents=True, exist_ok=True)
    lock = output / '.comparison.lock'
    _safe_output(lock)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        state = os.fstat(descriptor)
        if not stat.S_ISREG(state.st_mode) or state.st_nlink != 1:
            raise SafetyError(f'Comparison lock must be a regular file with one hard link: {lock}')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SafetyError('Another comparison is using this output directory') from error
        return _compare(recovered, archive, output, visual=visual, timeout=timeout, started=started)
    finally:
        os.close(descriptor)


def _compare(recovered: Path, archive: Path, output: Path, *,
             visual: bool, timeout: float, started: str) -> dict:
    cache_path = output / 'comparison-cache.sqlite3'
    names = ['comparison.json', 'comparison.csv', 'missing.csv', 'summary.md']
    for path in [cache_path, output / 'comparison-cache.sqlite3-journal',
                 output / 'comparison-cache.sqlite3-wal',
                 output / 'comparison-cache.sqlite3-shm',
                 output / 'visual-cache.sqlite3', output / 'visual-cache.sqlite3-journal',
                 output / 'visual-cache.sqlite3-wal', output / 'visual-cache.sqlite3-shm',
                 *[output / name for name in names]]:
        _safe_output(path)

    with phase('Discovering comparison videos', unit='folders') as task:
        recovered_paths, recovered_inventory_errors, recovered_skipped = _inventory(recovered)
        task.advance()
        archive_paths, archive_inventory_errors, archive_skipped = _inventory(archive)
        task.advance()
    errors = [{'side': 'recovered', **item} for item in recovered_inventory_errors]
    errors.extend({'side': 'archive', **item} for item in archive_inventory_errors)
    recovered_media, requested, recovered_stamps = {}, {}, {}
    with phase('Reading recovered video metadata', total=len(recovered_paths), unit='files') as task:
        for path in recovered_paths:
            try:
                recovered_stamps[str(path)] = _stamp(path)
                media = _media(path)
                recovered_media[str(path)] = media
                for track_keys in _keys(media):
                    for key, _ in track_keys:
                        if key not in requested:
                            requested[key] = len(requested)
            except (OSError, ValueError, SafetyError) as error:
                recovered_media[str(path)] = {'error': str(error)}
            task.advance(detail=path.name)

    rows = []
    archive_stamps = {}
    cache_hits = 0
    connection = sqlite3.connect(cache_path)
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, _CACHE_VERSION):
            raise SafetyError('Unsupported comparison cache version; select a new output folder')
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS media(
                id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
                stamp TEXT NOT NULL, size INTEGER NOT NULL,
                evidence BLOB, error TEXT, active INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS media_size ON media(size);
            CREATE TEMP TABLE postings(
                key INTEGER NOT NULL, file_id INTEGER NOT NULL,
                track INTEGER NOT NULL, position INTEGER NOT NULL);
            CREATE INDEX temp.posting_key ON postings(key);
        ''')
        connection.execute(f'PRAGMA user_version={_CACHE_VERSION}')
        connection.execute('UPDATE media SET active=0')
        connection.commit()
        key_widths = {}
        for codec, config, width, _ in requested:
            key_widths.setdefault((codec, config), set()).add(width)
        with phase('Indexing archive video metadata', total=len(archive_paths), unit='files') as task:
            for path in archive_paths:
                stamp, media, error = (), None, None
                try:
                    stamp = _stamp(path)
                    archive_stamps[str(path)] = stamp
                    saved = connection.execute('SELECT id,stamp,evidence,error FROM media WHERE path=?',
                                               (str(path),)).fetchone()
                    if saved and tuple(json.loads(saved[1])) == stamp and saved[2] and not saved[3]:
                        media = json.loads(zlib.decompress(saved[2]))
                        cache_hits += 1
                    else:
                        media = _media(path)
                    _check(path, stamp)
                except (OSError, ValueError, SafetyError, zlib.error) as problem:
                    error = str(problem)
                    errors.append({'side': 'archive', 'path': str(path), 'error': error})
                encoded = zlib.compress(json.dumps(media, separators=(',', ':')).encode()) if media else None
                connection.execute('''INSERT INTO media(path,stamp,size,evidence,error,active)
                    VALUES(?,?,?,?,?,1) ON CONFLICT(path) DO UPDATE SET stamp=excluded.stamp,
                    size=excluded.size,evidence=excluded.evidence,error=excluded.error,active=1''',
                    (str(path), json.dumps(stamp), stamp[2] if stamp else -1, encoded, error))
                file_id = connection.execute('SELECT id FROM media WHERE path=?', (str(path),)).fetchone()[0]
                postings = []
                if media and not error:
                    for index, track in enumerate(media['tracks']):
                        widths = key_widths.get((track['codec'], track['config']), ())
                        sizes = [sample[1] for sample in track['samples']]
                        for width in widths:
                            for position in range(len(sizes)-width+1):
                                key = (track['codec'], track['config'], width,
                                       tuple(sizes[position:position+width]))
                                if key in requested:
                                    postings.append((requested[key], file_id, index, position))
                    connection.executemany('INSERT INTO postings VALUES(?,?,?,?)', postings)
                connection.commit()
                task.advance(detail=path.name, cache_hits=cache_hits, errors=len(errors))

        fresh = OrderedDict()

        def current(file_id):
            if file_id in fresh:
                fresh.move_to_end(file_id)
                return fresh[file_id]
            path, cached_stamp = connection.execute('SELECT path,stamp FROM media WHERE id=?',
                                                   (file_id,)).fetchone()
            path = Path(path)
            _check(path, json.loads(cached_stamp))
            evidence = _media(path)
            fresh[file_id] = (path, evidence)
            if len(fresh) > 16:
                fresh.popitem(last=False)
            return path, evidence

        represented = 0
        with phase('Comparing recovered video content', total=len(recovered_paths), unit='files') as task:
            for path in recovered_paths:
                row = {'path': str(path), 'status': 'no_match', 'archive_path': '',
                       'method': '', 'detail': 'No complete encoded-video match found; absence is unproven.',
                       'visual_candidates': []}
                left = recovered_media[str(path)]
                try:
                    identity = _stamp(path)
                    if 'stamp' in left:
                        _check(path, left['stamp'])
                    # Equal-size candidates are the only whole files read for
                    # exact comparison; the complete archive is never hashed.
                    for file_id, right_path, stamp_text in connection.execute(
                            'SELECT id,path,stamp FROM media WHERE active=1 AND size=?', (identity[2],)):
                        target, right_stamp = Path(right_path), json.loads(stamp_text)
                        try:
                            if _same_file(path, target, identity, right_stamp):
                                row.update(status='represented_exact', archive_path=str(target),
                                           method='complete_file_bytes', detail='Complete current file bytes are equal.')
                                break
                        except (OSError, SafetyError) as error:
                            errors.append({'side': 'archive', 'path': str(target), 'error': str(error)})
                    if row['status'] not in REPRESENTED and 'error' not in left:
                        primary = _keys(left)[0]
                        selected, anchor = min(primary, key=lambda item: connection.execute(
                            'SELECT COUNT(*) FROM postings WHERE key=?', (requested[item[0]],)).fetchone()[0])
                        tried = set()
                        for file_id, track, position, target_path in connection.execute(
                                'SELECT p.file_id,p.track,p.position,m.path FROM postings p '
                                'JOIN media m ON m.id=p.file_id WHERE p.key=?',
                                (requested[selected],)):
                            start = position-anchor
                            candidate = (file_id, track, start)
                            if start < 0 or candidate in tried:
                                continue
                            tried.add(candidate)
                            target = Path(target_path)
                            try:
                                target, right = current(file_id)
                                spans = _represented(path, left, target, right, track, start)
                            except (OSError, ValueError, SafetyError) as error:
                                errors.append({'side': 'archive', 'path': str(target),
                                               'error': str(error)})
                                continue
                            if spans is not None:
                                row.update(status='represented_encoded', archive_path=str(target),
                                           method='complete_ordered_video_samples',
                                           detail='Every recovered video sample equals a current archive span; audio and timing were not compared.',
                                           spans=spans)
                                break
                    if row['status'] not in REPRESENTED and 'error' in left:
                        row.update(status='unreadable', detail=left['error'])
                        errors.append({'side': 'recovered', 'path': str(path), 'error': left['error']})
                except (OSError, ValueError, SafetyError) as error:
                    row.update(status='unreadable', detail=str(error))
                    errors.append({'side': 'recovered', 'path': str(path), 'error': str(error)})
                rows.append(row)
                represented += row['status'] in REPRESENTED
                task.advance(detail=path.name, represented=represented)
    finally:
        connection.close()

    limitations = [
        'Encoded matching proves ordered video sample bytes and codec configuration only; audio, timing, chronology and recovery completeness are not established.',
        'No-match does not establish absence of transcoded, cropped, differently packetized, unsupported or inaccessible video.',
        'Symlink files/directories and Finder .DS_Store metadata are skipped; only documented video extensions are inventoried.',
        'The metadata cache uses current file identity and timestamps; every positive encoded match reparses metadata and rereads all matched current video bytes.',
    ]
    visual_complete = True
    if visual:
        from .visual_comparison import find_visual_candidates
        pending = [Path(row['path']) for row in rows if row['status'] not in REPRESENTED]
        try:
            candidates = find_visual_candidates(pending, archive_paths, cache_dir=output, timeout=timeout)
            visual_complete = getattr(candidates, 'complete', True)
            errors.extend({'side': 'visual', **item} for item in getattr(candidates, 'errors', []))
            limitations.extend(getattr(candidates, 'limitations', []))
            for row in rows:
                found = candidates.get(row['path'], [])
                if row['status'] not in REPRESENTED and found:
                    row.update(status='visual_candidate', visual_candidates=found,
                               detail='Visual similarity requires review; retained in missing.csv.')
        except (OSError, ValueError, SafetyError) as error:
            visual_complete = False
            errors.append({'side': 'visual', 'path': '', 'error': str(error)})
    # Recovery may keep publishing new files during a read-only comparison.
    # Report the processed snapshot and expose drift instead of claiming that
    # the growing recovered folder has been exhaustively compared.
    final_recovered, final_errors, _ = _inventory(recovered)
    initial_names, final_names = set(map(str, recovered_paths)), set(map(str, final_recovered))
    added, removed = sorted(final_names-initial_names), sorted(initial_names-final_names)
    changed = []
    for path, identity in recovered_stamps.items():
        try:
            _check(Path(path), identity)
        except (OSError, SafetyError):
            changed.append(path)
    errors.extend({'side': 'recovered', **item} for item in final_errors)
    if added or removed or changed:
        errors.append({'side': 'recovered', 'path': str(recovered),
                       'error': 'Recovered folder changed during comparison; rerun for the newer snapshot.'})
        invalidated = set(removed) | set(changed)
        for row in rows:
            if row['path'] in invalidated:
                row.update(status='unreadable', archive_path='', method='',
                           detail='Recovered file changed or disappeared after its snapshot; rerun before relying on a match.')
    final_archive, final_archive_errors, _ = _inventory(archive)
    initial_archive_names = set(map(str, archive_paths))
    final_archive_names = set(map(str, final_archive))
    archive_added = sorted(final_archive_names-initial_archive_names)
    archive_removed = sorted(initial_archive_names-final_archive_names)
    archive_changed = []
    for path, identity in archive_stamps.items():
        try:
            _check(Path(path), identity)
        except (OSError, SafetyError):
            archive_changed.append(path)
    errors.extend({'side': 'archive', **item} for item in final_archive_errors)
    if archive_added or archive_removed or archive_changed:
        errors.append({'side': 'archive', 'path': str(archive),
                       'error': 'Archive folder changed during comparison; rerun for the newer snapshot.'})
        invalidated_archive = set(archive_removed) | set(archive_changed)
        for row in rows:
            if row['status'] in REPRESENTED and row['archive_path'] in invalidated_archive:
                row.update(status='unreadable', archive_path='', method='',
                           detail='Matching archive file changed or disappeared after its proof; rerun before relying on the match.')
    counts = dict(Counter(row['status'] for row in rows))
    summary = {'complete': not errors and visual_complete,
               'started_at': started, 'finished_at': datetime.now(timezone.utc).isoformat(),
               'recovered_snapshot': {'added': added, 'removed': removed, 'changed': changed},
               'archive_snapshot': {'added': archive_added, 'removed': archive_removed,
                                    'changed': archive_changed},
               'visual_complete': visual_complete,
               'recovered': str(recovered), 'archive': str(archive),
               'output': str(output), 'recovered_files': len(recovered_paths),
               'archive_files': len(archive_paths), 'counts': counts,
               'attention_files': sum(row['status'] not in REPRESENTED for row in rows),
               'archive_cache_hits': cache_hits, 'errors': len(errors),
               'skipped_symlinks_or_metadata': recovered_skipped+archive_skipped,
               'report_paths': {name: str(output/name) for name in names}}
    document = {'summary': summary, 'results': rows, 'errors': errors, 'limitations': limitations}
    _atomic_write(output/'comparison.json', json.dumps(document, indent=2)+'\n')
    _atomic_write(output/'comparison.csv', _csv(rows))
    _atomic_write(output/'missing.csv', _csv([row for row in rows if row['status'] not in REPRESENTED]))
    text = ('# Video folder comparison\n\n'
            f'Recovered (A): {recovered}\n\nArchive (B): {archive}\n\n'
            f'Compared {len(recovered_paths)} recovered and {len(archive_paths)} archive video files. '
            f'{summary["attention_files"]} recovered files need attention; {len(errors)} errors.\n\n'
            'Start with `missing.csv`. It includes visual candidates and unreadable files; '
            'no-match is a candidate for review, not proof of historical absence.\n\n'
            + '\n'.join(f'- {name}: {count}' for name, count in sorted(counts.items()))
            + '\n\n'+'\n'.join(f'- {item}' for item in limitations)+'\n')
    _atomic_write(output/'summary.md', text)
    return summary
