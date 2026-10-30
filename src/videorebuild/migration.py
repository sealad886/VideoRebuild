"""Explicit, backed-up conversion of legacy recovery cases to the shared schema."""
from __future__ import annotations

from contextlib import ExitStack, closing
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid

from .storage import SCHEMA, SafetyError, file_sha256, initialize_database, json_text, regular


def _artifact(root: Path, name: str) -> Path:
    relative = Path(name)
    path = root / relative
    if relative.is_absolute() or '..' in relative.parts or path.is_symlink():
        raise SafetyError('Unsafe legacy artifact path')
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise SafetyError(f'Missing or unsafe legacy artifact: {name}')
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise SafetyError('Legacy artifact parent is a symlink')
    return path


def _verify_output(root: Path, name: str, digest: str, size: int | None = None) -> dict:
    if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
        raise SafetyError('Legacy output lacks a valid SHA-256')
    path = _artifact(root,name)
    if (size is not None and path.stat().st_size != size) or file_sha256(path) != digest:
        raise SafetyError(f'Legacy publication changed: {name}')
    return dict(path=name,size=path.stat().st_size,sha256=digest)


def _ordinary(db: sqlite3.Connection, root: Path) -> int:
    version = db.execute('PRAGMA user_version').fetchone()['user_version']
    if version != 1:
        raise SafetyError(f'Only legacy ordinary schema 1 can migrate; found {version}')
    db.execute('ALTER TABLE sources ADD COLUMN extents TEXT')
    db.execute("ALTER TABLE sources ADD COLUMN binding TEXT NOT NULL DEFAULT '{}'")
    db.execute('ALTER TABLE sources ADD COLUMN hash_scope TEXT')
    db.execute("UPDATE sources SET binding=?,hash_scope='full'",(json_text({'acquisition':'unknown'}),))
    db.execute('ALTER TABLE results RENAME TO legacy_results')
    initialize_database(db)
    count = 0
    for row in db.execute('SELECT * FROM legacy_results'):
        detail = json.loads(row['detail'])
        if not isinstance(detail,dict):
            raise SafetyError('Invalid legacy result detail')
        candidates = detail.get('candidates',[])
        for candidate in candidates:
            _verify_output(root,candidate['path'],candidate['sha256'],candidate.get('bytes'))
        for output in detail.get('outputs',[]):
            if len(candidates) != 1:
                raise SafetyError('Legacy publication has no unique validated candidate')
            _verify_output(root,output,candidates[0]['sha256'],candidates[0].get('bytes'))
        detail['historical_validation'] = True
        detail['legacy_validation_contract'] = 'ordinary-schema-1'
        detail.pop('run_key',None)
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',
                   ('ordinary',row['movie_digest'],row['status'],json_text(detail),row['updated']))
        count += 1
    for row in db.execute('SELECT id,detail FROM attempts'):
        detail = json.loads(row['detail'])
        detail['historical_validation'] = True
        db.execute('UPDATE attempts SET detail=? WHERE id=?',(json_text(detail),row['id']))
    db.execute('DROP TABLE legacy_results')
    return count


def _fragmented(source: sqlite3.Connection, target: sqlite3.Connection, root: Path,
                binding: dict) -> int:
    initialize_database(target)
    image = binding.get('image',{})
    required = ('path','size','mtime_ns','device','inode')
    if not isinstance(image,dict) or not all(k in image for k in required):
        raise SafetyError('Legacy fragmented case lacks source identity')
    cur = target.execute('''INSERT INTO sources(path,kind,size,mtime_ns,dev,ino,binding)
                            VALUES(?,?,?,?,?,?,?)''',
                         (image['path'],'image',image['size'],image['mtime_ns'],image['device'],
                          image['inode'],json_text({'acquisition':'unknown'})))
    sid = cur.lastrowid
    target.execute('INSERT INTO settings VALUES(?,?)',('legacy_fragmented_binding',json_text(binding)))
    count = 0
    for row in source.execute('SELECT * FROM results ORDER BY offset'):
        evidence = _artifact(root,row['detail_path'])
        if evidence.stat().st_size > 64*1024*1024:
            raise SafetyError('Legacy validation evidence exceeds migration size limit')
        detail = json.loads(evidence.read_text(encoding='utf-8'))
        if not isinstance(detail,dict):
            raise SafetyError('Invalid legacy fragmented evidence')
        outputs = detail.get('outputs',[])
        verified = []
        if row['status'] == 'decoded_prefix' and not outputs:
            raise SafetyError('Legacy decoded prefix has no publication')
        if outputs and (type(detail.get('output_bytes')) is not int or detail['output_bytes'] <= 0):
            raise SafetyError('Legacy publication lacks a valid byte length')
        for name in outputs:
            verified.append(_verify_output(root,name,detail.get('candidate_sha256'),detail.get('output_bytes')))
        detail.update(historical_validation=True,legacy_validation_contract='fragmented-0.2',
                      detail_path=row['detail_path'],evidence_sha256=file_sha256(evidence),
                      output_evidence=verified)
        detail.setdefault('reason',row['reason'])
        target.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',
                       ('fragmented',f'legacy:{sid}:{row["offset"]}',row['status'],json_text(detail),row['updated']))
        count += 1
    return count


def migrate_case(root: Path) -> dict:
    """Backup, verify and replace one legacy case; never alter media publications."""
    if root.expanduser().is_symlink():
        raise SafetyError('Case root must not be a symlink')
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise SafetyError('Migration requires an existing case directory')
    ordinary, fragmented = root/'case.sqlite3',root/'fragmented.sqlite3'
    if ordinary.exists() and fragmented.exists():
        raise SafetyError('Both ordinary and fragmented databases exist; migration is ambiguous')
    source_path = ordinary if ordinary.exists() else fragmented
    regular(source_path)
    if source_path.is_symlink() or source_path.stat().st_nlink != 1:
        raise SafetyError('Unsafe legacy database')
    with ExitStack() as stack:
        for name in (('.lock','.fragmented.lock') if source_path == fragmented else ('.lock',)):
            path = root/name
            if path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
                raise SafetyError('Unsafe migration lock')
            handle = stack.enter_context(path.open('a+b'))
            try:
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SafetyError('Another process owns this case') from error
        source = sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True)
        stack.callback(source.close)
        source.row_factory = lambda cursor,row: dict(zip((c[0] for c in cursor.description),row))
        if source_path == ordinary:
            version = source.execute('PRAGMA user_version').fetchone()['user_version']
            if version == SCHEMA:
                return {'status':'already_current','schema':SCHEMA}
        backup_dir = root/'migration-backups'/f'{time.strftime("%Y%m%d-%H%M%S",time.gmtime())}-{uuid.uuid4().hex[:8]}'
        if (root/'migration-backups').is_symlink():
            raise SafetyError('Migration backup directory is a symlink')
        backup_dir.mkdir(mode=0o700,parents=True)
        backup = backup_dir/source_path.name
        backup.touch(mode=0o600)
        with closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise SafetyError('Legacy database failed integrity check; backup retained')
        binding = None
        if source_path == fragmented:
            metadata = _artifact(root,'fragmented-case.json')
            if metadata.stat().st_size > 4*1024*1024:
                raise SafetyError('Legacy case metadata exceeds migration limit')
            shutil.copy2(metadata,backup_dir/metadata.name)
            binding = json.loads(metadata.read_text(encoding='utf-8'))
            if not isinstance(binding,dict):
                raise SafetyError('Legacy fragmented case metadata must be an object')
        staging = root/f'.migration-{uuid.uuid4().hex}.sqlite3'
        staging.touch(mode=0o600)
        try:
            with closing(sqlite3.connect(staging)) as target:
                target.row_factory = lambda cursor,row: dict(zip((c[0] for c in cursor.description),row))
                if source_path == ordinary:
                    source.backup(target)
                    count = _ordinary(target,root)
                else:
                    count = _fragmented(source,target,root,binding)
                receipt = dict(source=source_path.name,backup=str(backup.relative_to(root)),
                               migrated_jobs=count,schema=SCHEMA,historical_validation=True)
                target.execute('INSERT INTO settings VALUES(?,?)',('migration',json_text(receipt)))
                target.commit()
                if target.execute('PRAGMA integrity_check').fetchone()['integrity_check'] != 'ok':
                    raise SafetyError('Converted database failed integrity check')
                if target.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise SafetyError('Converted database failed foreign-key check')
                target.execute('PRAGMA journal_mode=DELETE')
            source.close()
            with staging.open('rb') as stream:
                os.fsync(stream.fileno())
            moved = []
            try:
                for path in (source_path,Path(str(source_path)+'-wal'),Path(str(source_path)+'-shm')):
                    if path.exists():
                        if path.is_symlink():
                            raise SafetyError('Unsafe legacy journal')
                        retired = backup_dir/('retired-'+path.name)
                        os.replace(path,retired)
                        moved.append((path,retired))
                os.replace(staging,ordinary)
            except BaseException:
                for path,retired in reversed(moved):
                    os.replace(retired,path)
                raise
            receipt['status'] = 'migrated'
            return receipt
        finally:
            staging.unlink(missing_ok=True)
