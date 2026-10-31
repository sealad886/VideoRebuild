"""Reboot continuity and live physical alias/race protection."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from videorebuild.bmff import Chunk, FormatError, Movie
from videorebuild.candidate_tiers import CandidateTiers
from videorebuild.engine import validate_ranges
from videorebuild.storage import Case, ReaderPool, SafetyError, json_text


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.bin'
        self.source.write_bytes(b'encoded evidence')
        self.case = Case(self.root / 'case').__enter__()
        self.addCleanup(self.case.__exit__, None, None, None)
        self.sid = self.case.register_source(self.source, 'image')
        self.case.db.execute('UPDATE sources SET complete=1,sha256=? WHERE id=?',
                             (hashlib.sha256(self.source.read_bytes()).hexdigest(), self.sid))
        self.case.db.commit()

    def remounted_sources(self):
        self.case.db.execute('UPDATE sources SET dev=dev+1000 WHERE id=?', (self.sid,))
        self.case.db.commit()
        return self.case.sources()

    def fake_stat(self, state, **changes):
        values = {key: getattr(state, key) for key in ('st_size', 'st_mtime_ns', 'st_dev', 'st_ino')}
        return SimpleNamespace(**(values | changes))

    def test_remount_resumes_without_rewriting_index_or_invalidating_job(self):
        sources = self.remounted_sources()
        self.case.result('completed', 'reconstructed', {'outputs': []})
        before = dict(self.case.db.execute('SELECT * FROM sources WHERE id=?', (self.sid,)).fetchone())
        with ReaderPool(sources) as pool:
            self.assertEqual(pool.read(self.sid, 0, 7), b'encoded')
            self.assertEqual(pool.physical_key(self.sid), (self.source.stat().st_dev, self.source.stat().st_ino))
        self.assertEqual(self.case.register_source(self.source, 'image'), self.sid)
        self.assertEqual(dict(self.case.db.execute('SELECT * FROM sources WHERE id=?', (self.sid,)).fetchone()), before)
        self.assertEqual(self.case.job('ordinary', 'completed')['status'], 'reconstructed')
        self.assertEqual(sources[self.sid], before)

    def test_size_mtime_and_inode_changes_remain_rejected(self):
        row = self.remounted_sources()[self.sid]
        for field in ('size', 'mtime_ns', 'ino'):
            with self.subTest(field=field), self.assertRaises(SafetyError):
                ReaderPool._check_identity(row | {field: row[field] + 1})

    def test_device_change_after_pool_is_pinned_is_rejected(self):
        sources = self.remounted_sources()
        state = self.source.stat()
        with ReaderPool(sources) as pool:
            pool.check(self.sid)
            with patch('videorebuild.storage.regular', return_value=self.fake_stat(state, st_dev=state.st_dev+1)):
                with self.assertRaises(SafetyError):
                    pool.check(self.sid)

    def test_opened_descriptor_must_match_current_path_including_device(self):
        state = self.source.stat()
        with ReaderPool(self.remounted_sources()) as pool:
            with patch('videorebuild.storage.os.fstat', return_value=self.fake_stat(state, st_dev=state.st_dev+1)):
                with self.assertRaises(SafetyError):
                    pool.handle(self.sid)
            self.assertFalse(pool.handles)

    def test_cached_descriptor_is_checked_against_current_path(self):
        state = self.source.stat()
        with ReaderPool(self.remounted_sources()) as pool:
            pool.handle(self.sid)
            with patch('videorebuild.storage.os.fstat', return_value=self.fake_stat(state, st_dev=state.st_dev+1)):
                with self.assertRaises(SafetyError):
                    pool.read(self.sid, 0, 1)

    def test_map_remount_keeps_historical_binding_but_hash_changes_fail(self):
        image = self.root / 'mapped-image.bin'
        image.write_bytes(b'12345678')
        mapfile = self.root / 'image.map'
        mapfile.write_text('0 + 1\n0 8 +\n')
        sid = self.case.register_source(image, 'image', mapfile=mapfile)
        row = dict(self.case.db.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone())
        binding = json.loads(row['binding'])
        binding['mapfile']['dev'] += 1000
        self.case.db.execute('UPDATE sources SET dev=dev+1000,binding=?,complete=1 WHERE id=?',
                             (json_text(binding), sid))
        self.case.db.commit()
        before = dict(self.case.db.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone())
        self.assertEqual(self.case.register_source(image, 'image', mapfile=mapfile), sid)
        with ReaderPool(self.case.sources()) as pool:
            self.assertEqual(pool.read(sid, 0, 8), b'12345678')
        self.assertEqual(dict(self.case.db.execute('SELECT * FROM sources WHERE id=?', (sid,)).fetchone()), before)
        modified = mapfile.stat()
        mapfile.write_text('0 + 1\n0 8 ?\n')
        os.utime(mapfile, ns=(modified.st_atime_ns, modified.st_mtime_ns))
        with self.assertRaises(SafetyError):
            ReaderPool(self.case.sources())

    def test_live_aliases_overlap_and_demote_across_different_historical_devices(self):
        alias = self.root / 'alias.bin'
        os.link(self.source, alias)
        alias_id = self.case.register_source(alias, 'image')
        self.case.db.execute('UPDATE sources SET dev=dev+id*1000,complete=1')
        self.case.db.commit()
        sources = self.case.sources()
        before = {sid: dict(row) for sid, row in sources.items()}
        with ReaderPool(sources) as pool:
            movie = Movie(b'', (), (Chunk(0, 0, 0, (4,), 0), Chunk(0, 1, 4, (4,), 1)), 'fixture')
            with self.assertRaises(FormatError):
                validate_ranges(movie, {(0, 0): (self.sid, 0), (0, 1): (alias_id, 0)}, pool)
            candidate = self.case.root / 'candidates' / 'accepted.mp4'
            output = self.case.root / 'reconstructed' / 'accepted.mp4'
            candidate.write_bytes(pool.read(self.sid, 0, 4))
            output.write_bytes(candidate.read_bytes())
            part = pool.provenance(self.sid, 0, 4)[0]
            part['source_identity'] = {key: sources[self.sid][key] for key in ('size', 'mtime_ns', 'dev', 'ino')}
            candidate.with_suffix('.json').write_text(json.dumps({'provenance': {'chunks': [{'physical_extents': [part]}]}}))
            self.case.result('accepted', 'reconstructed', {'outputs': [str(output.relative_to(self.case.root))],
                'candidates': [{'path': str(candidate.relative_to(self.case.root)), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}]})
            tiers = CandidateTiers(self.case, pool)
            self.assertFalse(tiers.view().permits(alias_id, 0, 4))
            self.assertTrue(tiers.view().permits(alias_id, 4, 4))
            self.assertEqual(tiers.records[0]['source_identity']['dev'], sources[self.sid]['dev'])
        self.assertEqual(self.case.sources(), before)

    def test_filtered_metadata_does_not_mutate_original_sources(self):
        sources = self.remounted_sources()
        metadata = self.root / '.DS_Store'
        metadata.write_bytes(b'Finder metadata')
        state = metadata.stat()
        sources[99] = dict(path=str(metadata), size=state.st_size, mtime_ns=0,
                           dev=-1, ino=state.st_ino, binding='{}')
        before = {sid: dict(row) for sid, row in sources.items()}
        with ReaderPool(sources, physical_sources=sources) as pool:
            self.assertNotIn(99, pool.sources)
            self.assertNotIn(99, pool.physical)
            with self.assertRaises((SafetyError, FormatError)):
                pool.read(99, 0, 1)
            self.assertEqual(pool.read(self.sid, 0, 7), b'encoded')
        self.assertEqual(sources, before)


if __name__ == '__main__':
    unittest.main()
