import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from videorebuild.cli import main
from videorebuild.storage import (Case, ReaderPool, SafetyError, atomic_text, create_private_json,
                                 file_sha256, scan_logical, write_json)
from videorebuild.ranges import Extent, UnavailableRange


class StorageTests(unittest.TestCase):
    def test_report_formats_preserve_source_hardlinks(self):
        for report_name in ('summary.json', 'results.csv', 'index.html'):
            with self.subTest(report=report_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'original.bin'
                original = b'synthetic immutable source'
                source.write_bytes(original)
                case = root / 'case'
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['scan', '--case', str(case), '--input', str(source), '--all-files']), 0)
                    alias = case / 'reports' / 'summary.json.tmp'
                    os.link(source, alias)
                    target = case / 'reports' / report_name
                    target.unlink()
                    os.link(source, target)
                    self.assertEqual(main(['report', '--case', str(case)]), 0)
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(alias.read_bytes(), original)
                self.assertEqual(json.loads((case / 'reports' / 'summary.json').read_text())['sources'], 1)
                self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)

    def test_atomic_text_failure_preserves_old_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.txt'
            target.write_bytes(b'old report')
            with self.assertRaises(ValueError):
                with atomic_text(target) as stream:
                    stream.write('partial replacement')
                    raise ValueError('report generation failed')
            self.assertEqual(target.read_bytes(), b'old report')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_json_rejects_symlink_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.write_bytes(b'original')
            target = root / 'report.json'
            target.symlink_to(source)
            with self.assertRaises(SafetyError):
                write_json(target, {'new': True})
            with self.assertRaises(FileExistsError):
                create_private_json(target, {'new': True})
            self.assertTrue(target.is_symlink())
            self.assertEqual(source.read_bytes(), b'original')

    def test_atomic_json_failure_preserves_old_target_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.json'
            target.write_bytes(b'old report')
            with patch('videorebuild.storage.os.fsync', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    write_json(target, {'new': True})
            self.assertEqual(target.read_bytes(), b'old report')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_json_outputs_private_and_fresh_output_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous_umask = os.umask(0)
            try:
                for writer in (write_json, create_private_json):
                    with self.subTest(writer=writer.__name__):
                        target = root / (writer.__name__ + '.json')
                        value = {'unicode': 'café', 'items': [1, None]}
                        writer(target, value)
                        self.assertEqual(json.loads(target.read_text()), value)
                        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                fresh = root / 'create_private_json.json'
                previous = fresh.read_bytes()
                with self.assertRaises(FileExistsError):
                    create_private_json(fresh, {'replacement': True})
                self.assertEqual(fresh.read_bytes(), previous)
            finally:
                os.umask(previous_umask)

    def test_hash_empty_and_chunk_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'data'
            for size in (0, 1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 1, 2 * 1024 * 1024 + 7):
                with self.subTest(size=size):
                    data = (bytes(range(256)) * (size // 256 + 1))[:size]
                    target.write_bytes(data)
                    self.assertEqual(file_sha256(target), hashlib.sha256(data).hexdigest())

    def test_failed_private_json_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.json'
            with self.assertRaises(TypeError):
                create_private_json(target, {'partial': 1, 'unsupported': object()})
            self.assertFalse(target.exists())
            with patch('videorebuild.storage.os.fsync', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    create_private_json(target, {'partial': 1})
            self.assertFalse(target.exists())
            create_private_json(target, {'complete': True})
            self.assertEqual(json.loads(target.read_text()), {'complete': True})

    def test_private_json_failure_preserves_replacement_path(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.json'

            def replace_then_fail(fd):
                target.unlink()
                target.write_bytes(b'unrelated replacement')
                raise OSError('disk failure')

            with patch('videorebuild.storage.os.fsync', side_effect=replace_then_fail):
                with self.assertRaises(OSError):
                    create_private_json(target, {'partial': 1})
            self.assertEqual(target.read_bytes(), b'unrelated replacement')

    def test_private_json_close_failure_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'report.json'
            real_fdopen = os.fdopen

            @contextlib.contextmanager
            def fail_on_close(fd, *args, **kwargs):
                with real_fdopen(fd, *args, **kwargs) as stream:
                    yield stream
                raise OSError('close failure')

            with patch('videorebuild.storage.os.fdopen', side_effect=fail_on_close):
                with self.assertRaises(OSError):
                    create_private_json(target, {'partial': 1})
            self.assertFalse(target.exists())
            create_private_json(target, {'complete': True})
            self.assertEqual(json.loads(target.read_text()), {'complete': True})

    def test_new_case_private_existing_case_permissions_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / 'existing'
            existing.mkdir(mode=0o750)
            existing.chmod(0o750)
            previous_umask = os.umask(0)
            try:
                for path, expected_mode in ((root / 'new', 0o700), (existing, 0o750)):
                    with self.subTest(path=path.name), Case(path):
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected_mode)
            finally:
                os.umask(previous_umask)

    def test_logical_reads_and_scanning_cross_physical_boundaries(self):
        from videorebuild.bmff import make_box
        data = make_box(b'ftyp',b'isom\x00\x00\x00\x00isom')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a,b = root/'a.bin',root/'b.bin'
            a.write_bytes(b'XX'+data[:6])
            b.write_bytes(b'junk'+data[6:])
            with Case(root/'case') as case:
                first,second = case.register_source(a),case.register_source(b)
                sid = case.add_stream('video.mp4',[Extent(0,first,2,6),Extent(6,second,4,len(data)-6)],size=len(data))
                with patch('videorebuild.storage.BLOCK',7):
                    scan_logical(case,sid)
                self.assertEqual(case.db.execute('SELECT kind FROM atoms').fetchall()[0][0],'ftyp')
                self.assertEqual(case.sources()[sid]['sha256'],hashlib.sha256(data).hexdigest())
                with ReaderPool(case.sources(),limit=1,physical_sources=case.physical_sources()) as pool:
                    self.assertEqual(pool.read(sid,3,9),data[3:12])
                    self.assertEqual(len(pool.provenance(sid,3,9)),2)
                    self.assertLessEqual(len(pool.handles),1)
                self.assertEqual(a.read_bytes(),b'XX'+data[:6])
                self.assertEqual(b.read_bytes(),b'junk'+data[6:])

    def test_logical_labels_do_not_conflate_distinct_recordings_and_staging_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'image'; source.write_bytes(b'abcdefgh')
            with Case(root/'case') as case:
                physical = case.register_source(source,'image')
                a = case.add_stream('same.mp4',[Extent(0,physical,0,4)],size=4)
                b = case.add_stream('same.mp4',[Extent(0,physical,4,4)],size=4)
                self.assertNotEqual(a,b)
                self.assertEqual(a,case.add_stream('same.mp4',[Extent(0,physical,0,4)],size=4))
                case.db.execute('SAVEPOINT intake')
                case.add_stream('later.mp4',[Extent(0,physical,0,4)],size=4)
                case.db.execute('ROLLBACK TO intake')
                case.db.execute('RELEASE intake')
                self.assertEqual(case.db.execute("SELECT COUNT(*) FROM sources WHERE kind='logical'").fetchone()[0],2)

    def test_map_holes_are_not_read_or_hashed_as_recovered_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'image'; source.write_bytes(b'AAAAXXXXBBBB')
            rescue = root/'map'; rescue.write_text('0 + 1\n0 4 +\n4 4 -\n8 4 +\n')
            with Case(root/'case') as case:
                sid = case.register_source(source,'image',mapfile=rescue)
                scan_logical(case,sid)
                with ReaderPool(case.sources()) as pool:
                    self.assertEqual(list(pool.available_ranges(sid)),[(0,4),(8,4)])
                    self.assertEqual(pool.read(sid,8,4),b'BBBB')
                    with self.assertRaises(UnavailableRange):
                        pool.read(sid,2,8)
                    rescue.write_text('0 + 1\n0 12 +\n')
                    with self.assertRaises(SafetyError):
                        pool.read(sid,0,4)
                self.assertEqual(case.sources()[sid]['hash_scope'],'available-ranges')
                self.assertEqual(case.sources()[sid]['sha256'],hashlib.sha256(b'AAAABBBB').hexdigest())

    def test_logical_source_change_and_settings_change_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'input'; source.write_bytes(b'abcdef')
            with Case(root/'case') as case:
                sid = case.register_source(source)
                logical = case.add_stream('video',[Extent(0,sid,0,6)],size=6)
                scan_logical(case,logical)
                case.bind('settings',{'version':1})
                with self.assertRaises(SafetyError):
                    case.bind('settings',{'version':2})
                with ReaderPool(case.sources(),physical_sources=case.physical_sources()) as pool:
                    source.write_bytes(b'longer source')
                    with self.assertRaises(SafetyError):
                        pool.read(logical,0,1)

    def test_scanning_one_stream_does_not_parse_unrelated_catalogue_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'image'; source.write_bytes(b'abcdefgh')
            with Case(root/'case') as case:
                physical = case.register_source(source,'image')
                sid = case.add_stream('chosen',[Extent(0,physical,0,8)],size=8)
                unrelated = case.add_stream('unrelated',[Extent(0,physical,0,8)],size=8)
                case.db.execute('UPDATE sources SET extents=? WHERE id=?',('{unreadable',unrelated))
                case.db.commit()
                scan_logical(case,sid)
                self.assertEqual(case.sources()[sid]['sha256'],hashlib.sha256(b'abcdefgh').hexdigest())

    def test_narrow_range_and_identity_checks_do_not_walk_every_extent(self):
        class Counted:
            def __init__(self,values): self.values,self.reads=values,0
            def __len__(self): return len(self.values)
            def __getitem__(self,index):
                self.reads+=1
                return self.values[index]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'image'; source.write_bytes(b'x'*10000)
            with Case(root/'case') as case:
                physical=case.register_source(source,'image')
                sid=case.add_stream('many',[Extent(i,physical,i,1) for i in range(10000)],size=10000)
                rows={r['id']:dict(r) for r in case.db.execute('SELECT * FROM sources')}
                with ReaderPool(rows,physical_sources=case.physical_sources()) as pool:
                    counted=Counted(pool.extents[sid]); pool.extents[sid]=counted
                    for _ in range(100): pool.check(sid)
                    self.assertEqual(list(pool.available_ranges(sid,9999,10000)),[(9999,1)])
                    self.assertLess(counted.reads,40)


class MigrationTests(unittest.TestCase):
    def legacy_ordinary(self,root):
        case_root = root/'case'
        with Case(case_root):
            pass
        output = case_root/'candidates'/'old.mp4'
        output.write_bytes(b'original encoded media')
        digest = file_sha256(output)
        db = sqlite3.connect(case_root/'case.sqlite3')
        db.execute('DROP TABLE jobs')
        db.execute('DROP TABLE settings')
        db.execute('DROP INDEX sources_physical')
        for column in ('extents','binding','hash_scope'):
            db.execute(f'ALTER TABLE sources DROP COLUMN {column}')
        db.execute('CREATE TABLE results(movie_digest TEXT PRIMARY KEY,status TEXT,detail TEXT,updated REAL)')
        detail = {'run_key':'old','candidates':[{'path':'candidates/old.mp4','sha256':digest}], 'outputs':[]}
        db.execute('INSERT INTO results VALUES(?,?,?,?)',('metadata','matched',json.dumps(detail),1.0))
        db.execute('PRAGMA user_version=1')
        db.commit(); db.close()
        return case_root,output

    def test_ordinary_migration_preserves_history_and_creates_verified_backup(self):
        from videorebuild.migration import migrate_case
        with tempfile.TemporaryDirectory() as directory:
            case_root,output = self.legacy_ordinary(Path(directory))
            original = output.read_bytes()
            receipt = migrate_case(case_root)
            self.assertEqual(receipt['migrated_jobs'],1)
            backup = case_root/receipt['backup']
            with contextlib.closing(sqlite3.connect(backup)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT status FROM results').fetchone()[0],'matched')
            with Case(case_root) as case:
                job = case.job('ordinary','metadata')
                self.assertEqual(job['status'],'matched')
                self.assertTrue(job['detail']['historical_validation'])
                self.assertNotIn('run_key',job['detail'])
            self.assertEqual(output.read_bytes(),original)
            self.assertEqual(migrate_case(case_root)['status'],'already_current')

    def test_corrupt_publication_refuses_migration_without_losing_original_or_backup(self):
        from videorebuild.migration import migrate_case
        with tempfile.TemporaryDirectory() as directory:
            case_root,output = self.legacy_ordinary(Path(directory))
            output.write_bytes(b'changed')
            with self.assertRaises(SafetyError):
                migrate_case(case_root)
            with contextlib.closing(sqlite3.connect(case_root/'case.sqlite3')) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertEqual(output.read_bytes(),b'changed')
            self.assertEqual(len(list((case_root/'migration-backups').glob('*/case.sqlite3'))),1)

    def test_failed_installation_restores_original_database(self):
        from videorebuild.migration import migrate_case
        with tempfile.TemporaryDirectory() as directory:
            case_root,output = self.legacy_ordinary(Path(directory))
            replace = os.replace
            def fail_install(source,destination):
                if Path(source).name.startswith('.migration-') and Path(destination).name == 'case.sqlite3':
                    raise OSError('installation failure')
                return replace(source,destination)
            with patch('videorebuild.migration.os.replace',side_effect=fail_install):
                with self.assertRaises(OSError):
                    migrate_case(case_root)
            with contextlib.closing(sqlite3.connect(case_root/'case.sqlite3')) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM results').fetchone()[0],1)
            self.assertEqual(output.read_bytes(),b'original encoded media')

    def test_backup_includes_committed_wal_on_failed_migration(self):
        from videorebuild.migration import migrate_case
        with tempfile.TemporaryDirectory() as directory:
            case_root,output = self.legacy_ordinary(Path(directory))
            output.write_bytes(b'changed')
            with contextlib.closing(sqlite3.connect(case_root/'case.sqlite3')) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('PRAGMA wal_autocheckpoint=0')
                writer.execute('INSERT INTO events(created,kind,detail) VALUES(1,?,?)',('wal-only','{}'))
                writer.commit()
                self.assertTrue(Path(str(case_root/'case.sqlite3')+'-wal').exists())
                with self.assertRaises(SafetyError):
                    migrate_case(case_root)
                backup = next((case_root/'migration-backups').glob('*/case.sqlite3'))
                with contextlib.closing(sqlite3.connect(backup)) as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='wal-only'").fetchone()[0],1)

    def test_malformed_legacy_detail_preserves_original_database_and_backup(self):
        from videorebuild.migration import migrate_case
        with tempfile.TemporaryDirectory() as directory:
            case_root,_ = self.legacy_ordinary(Path(directory))
            with contextlib.closing(sqlite3.connect(case_root/'case.sqlite3')) as db:
                db.execute("UPDATE results SET detail='{broken'")
                db.commit()
            with self.assertRaises(ValueError):
                migrate_case(case_root)
            with contextlib.closing(sqlite3.connect(case_root/'case.sqlite3')) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT detail FROM results').fetchone()[0],'{broken')
            self.assertEqual(len(list((case_root/'migration-backups').glob('*/case.sqlite3'))),1)

    def test_late_invalid_migration_row_cannot_install_partial_conversion(self):
        from videorebuild.migration import migrate_case
        with tempfile.TemporaryDirectory() as directory:
            case_root,output = self.legacy_ordinary(Path(directory))
            with contextlib.closing(sqlite3.connect(case_root/'case.sqlite3')) as db:
                db.execute('INSERT INTO results VALUES(?,?,?,?)',('later','blocked','{broken',2.0))
                db.commit()
            with self.assertRaises(ValueError):
                migrate_case(case_root)
            with contextlib.closing(sqlite3.connect(case_root/'case.sqlite3')) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM results').fetchone()[0],2)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='jobs'").fetchone())
            self.assertEqual(output.read_bytes(),b'original encoded media')

    def test_fragmented_migration_keeps_old_evidence_and_validation_contract(self):
        from videorebuild.migration import migrate_case
        from videorebuild.image_recovery import verify_completed
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); case_root=root/'case'; case_root.mkdir()
            (case_root/'attempts').mkdir(); (case_root/'recovered').mkdir()
            output = case_root/'recovered'/'prefix.mp4'; output.write_bytes(b'old prefix')
            evidence = {'outputs':['recovered/prefix.mp4'],'candidate_sha256':file_sha256(output),'output_bytes':10}
            (case_root/'attempts'/'result.json').write_text(json.dumps(evidence))
            binding={'image':{'path':str(root/'image'),'size':100,'mtime_ns':1,'device':2,'inode':3}}
            (case_root/'fragmented-case.json').write_text(json.dumps(binding))
            with contextlib.closing(sqlite3.connect(case_root/'fragmented.sqlite3')) as db:
                db.execute('CREATE TABLE results(offset INTEGER PRIMARY KEY,status TEXT,reason TEXT,detail_path TEXT,updated REAL)')
                db.execute('INSERT INTO results VALUES(?,?,?,?,?)',(0,'decoded_prefix','done','attempts/result.json',1))
                db.commit()
            receipt=migrate_case(case_root)
            self.assertEqual(receipt['migrated_jobs'],1)
            self.assertFalse((case_root/'fragmented.sqlite3').exists())
            with Case(case_root) as case:
                job=list(case.get_jobs('fragmented'))[0]
                self.assertEqual(job['status'],'decoded_prefix')
                self.assertTrue(job['detail']['historical_validation'])
                self.assertEqual(job['detail']['output_evidence'][0]['sha256'],file_sha256(output))
                self.assertEqual(case.sources(),{})
                verify_completed(case)
                output.write_bytes(b'changed historical publication')
                with self.assertRaises(SafetyError):
                    verify_completed(case)
                output.write_bytes(b'old prefix')
            self.assertEqual(output.read_bytes(),b'old prefix')


if __name__ == '__main__':
    unittest.main()
