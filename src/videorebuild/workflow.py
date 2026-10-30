"""Canonical read-only intake and recovery workflow."""
from __future__ import annotations

import logging
import json
from pathlib import Path, PurePosixPath
import re

from .engine import Options, recover
from .ranges import Extent, iter_dfxml
from .storage import Case, ReaderPool, SafetyError, file_sha256, input_files, regular, scan_logical
from .validate import doctor

LOG = logging.getLogger(__name__)


def _metadata(path: Path | None) -> dict | None:
    if path is None:
        return None
    path = path.expanduser().resolve()
    stat = regular(path)
    return {'path': str(path), 'size': stat.st_size, 'sha256': file_sha256(path)}


def _recovered_header(report: Path, roots: list[Path], name: str) -> Path:
    parts = PurePosixPath(name).parts
    if len(parts) == 1:
        parts = (report.parent.name, parts[0])
    if (len(parts) < 2 or not re.fullmatch(r'recup_dir\.\d+', parts[-2]) or
            not re.fullmatch(r'[A-Za-z0-9_.-]+\.mp4', parts[-1], re.I)):
        raise SafetyError(f'Cannot safely map PhotoRec header: {name}')
    candidates = []
    for root in roots:
        if not root.is_dir():
            continue
        p = root.joinpath(*parts[-2:])
        if p.exists():
            if p.is_symlink() or any(parent.is_symlink() for parent in p.parents if parent != root.parent):
                raise SafetyError('PhotoRec header path contains a symlink')
            candidates.append(p.resolve())
    if len(set(candidates)) != 1:
        raise SafetyError(f'PhotoRec header must resolve in exactly one input root: {name}')
    return candidates[0]


def intake(case: Case, args) -> None:
    roots = [p.expanduser().resolve() for p in args.input]
    images = [p.expanduser().resolve() for p in args.image]
    if not roots and not images:
        raise SafetyError('Provide at least one --input or --image')
    if any((args.report, args.extents, args.mapfile)) and len(images) != 1:
        raise SafetyError('--report, --extents and --mapfile require exactly one --image')
    if args.report and args.extents:
        raise SafetyError('Use one mapping report: --report (PhotoRec) or --extents (filesystem DFXML)')
    if args.map_offset and not args.mapfile:
        raise SafetyError('--map-offset requires --mapfile')
    binding = {'inputs': [str(p) for p in roots], 'images': [str(p) for p in images],
               'report': _metadata(args.report), 'extents': _metadata(args.extents),
               'mapfile': _metadata(args.mapfile), 'map_offset': args.map_offset,
               'all_files': args.all_files}
    case.bind('intake', binding)
    image_ids = [case.register_source(p, 'image', mapfile=args.mapfile, image_offset=args.map_offset)
                 for p in images]
    case.db.execute('CREATE TABLE IF NOT EXISTS verified_anchors '
                    '(source_id INTEGER NOT NULL,offset INTEGER NOT NULL,PRIMARY KEY(source_id,offset))')
    if args.extents:
        image_id = image_ids[0]
        records = usable = blocked = 0
        # Stage every mapping before scanning. A malformed trailing XML document
        # must roll back all imported streams, rather than publish a partial import.
        previous_streams = case.db.execute("SELECT COUNT(*) FROM sources WHERE kind='logical'").fetchone()[0]
        case.db.execute('SAVEPOINT extent_intake')
        try:
            for record in iter_dfxml(args.extents, images[0].stat().st_size):
                records += 1
                if record.reason and record.reason != 'unmapped_ranges':
                    blocked += 1
                    case.db.execute('INSERT INTO events(created,kind,detail) VALUES '
                                    '(strftime(\'%s\',\'now\'),?,?)',
                                    ('blocked_mapping', json.dumps({'name': record.name, 'reason': record.reason})))
                    continue
                if not record.extents or not record.size:
                    blocked += 1
                    continue
                extents = tuple(Extent(logical, image_id, physical, length)
                                for logical, physical, length in record.extents)
                case.add_stream(record.name, extents, size=record.size, association='filesystem_metadata')
                usable += 1
                if records % 10000 == 0:
                    LOG.info('Filesystem mapping: %d objects; %d usable; %d blocked', records, usable, blocked)
            if not usable:
                raise SafetyError('No usable filesystem byte mappings found')
            case.db.execute('RELEASE extent_intake')
            case.db.commit()
        except BaseException:
            case.db.execute('ROLLBACK TO extent_intake')
            case.db.execute('RELEASE extent_intake')
            raise
        if case.db.execute("SELECT COUNT(*) FROM sources WHERE kind='logical'").fetchone()[0] != previous_streams:
            case.invalidate_results()
        case.event('filesystem_mapping_imported', {'objects': records, 'usable': usable, 'blocked': blocked})
        for row in case.db.execute("SELECT id FROM sources WHERE kind='logical' ORDER BY id"):
            scan_logical(case, row['id'])
    else:
        # PhotoRec mappings establish verified initialization locations; the
        # image is indexed once so discovery need not trust carve lengths.
        if args.report:
            report = args.report.expanduser().resolve()
            physical = case.physical_sources()
            with ReaderPool(physical, physical_sources=physical) as pool:
                selected = 0
                case.db.execute('SAVEPOINT anchor_intake')
                try:
                    for record in iter_dfxml(report, images[0].stat().st_size, evidence_kind='photorec'):
                        if not record.name.lower().endswith('.mp4') or not 16 <= record.size <= 1024*1024:
                            continue
                        if record.reason:
                            raise SafetyError(f'Invalid PhotoRec header mapping: {record.reason}')
                        header_path = _recovered_header(report, roots, record.name)
                        before = header_path.stat()
                        with header_path.open('rb') as source:
                            header = source.read(record.size + 1)
                        if len(header) != record.size:
                            raise SafetyError('Recovered header size differs from PhotoRec report')
                        mapped = b''.join(pool.read(image_ids[0], physical_offset, length)
                                          for _, physical_offset, length in record.extents)
                        after = header_path.stat()
                        identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                        if identity(before) != identity(after) or header != mapped:
                            raise SafetyError('Image does not reproduce the recovered PhotoRec header')
                        case.db.execute('INSERT OR IGNORE INTO verified_anchors VALUES(?,?)',
                                        (image_ids[0], record.extents[0][1]))
                        selected += 1
                    if not selected:
                        raise SafetyError('No small MP4 header locations in PhotoRec report')
                    case.db.execute('RELEASE anchor_intake')
                    case.db.commit()
                except BaseException:
                    case.db.execute('ROLLBACK TO anchor_intake')
                    case.db.execute('RELEASE anchor_intake')
                    raise
            case.event('photorec_anchors_verified', {'selected': selected})
        for sid in image_ids:
            scan_logical(case, sid)
    for path in input_files(roots, case.root, args.all_files):
        scan_logical(case, case.register_source(path, 'file'))
    if _metadata(args.report) != binding['report'] or _metadata(args.extents) != binding['extents'] or _metadata(args.mapfile) != binding['mapfile']:
        raise SafetyError('An intake metadata source changed during import')


def reconstruct(case: Case, args) -> None:
    from .image_recovery import recover_fragmented
    fields = {name: getattr(args, name) for name in Options.__dataclass_fields__}
    recover(case, Options(**fields), args.limit, args.retry)
    tools = doctor()
    with ReaderPool(case.sources(), physical_sources=case.physical_sources()) as pool:
        recover_fragmented(case, pool, tools, args, args.limit, args.retry)
