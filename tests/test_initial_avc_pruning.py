"""Strict AVC start proofs prune branches without narrowing structural recovery."""
from dataclasses import replace
import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from helpers import generate, movie_from
from test_deep_witnesses import structural_movie
from videorebuild import engine
from videorebuild.bmff import FormatError, iter_sample_nals, valid_sample
from videorebuild.progress import phase
from videorebuild.storage import Case, ReaderPool, SafetyError, scan_logical
from videorebuild.validate import doctor, validate


def nal(kind, body=b'ABC'):
    return (len(body)+1).to_bytes(4, 'big') + bytes([kind]) + body


def counters():
    return dict(partial_assignments_checked=0, partial_assignments_rejected=0,
                content_prefixes_checked=0, content_prefixes_rejected=0,
                content_tuples_considered=0, initial_avc_fingerprints_checked=0,
                initial_avc_fingerprints_rejected=0, initial_avc_locations_rejected=0)


class InitialAvcPruningTests(unittest.TestCase):
    def fixture(self, domains, *, codec=b'avc1', overlay=False):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = root/'tokens.bin'
        data = bytearray()
        rows = []
        movie = structural_movie([1000+1000*i for i in range(len(domains))],
                                 [len(domain[0]) for domain in domains])
        if codec != b'avc1':
            movie = replace(movie, tracks=(replace(movie.tracks[0], codec=codec),))
        case = self.enterContext(Case(root/'case'))
        for index, domain in enumerate(domains):
            for sample in domain:
                self.assertEqual(len(sample), len(domain[0]))
                rows.append((movie.digest, index, hashlib.sha256(sample).hexdigest(), 1, len(data)))
                data.extend(sample+b'Z'*16)
        source.write_bytes(data)
        sid = case.register_source(source)
        self.assertEqual(sid, 1)
        scan_logical(case, sid)
        pool = self.enterContext(ReaderPool(case.sources()))
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        if overlay:
            db.executescript('''CREATE TABLE hit_locations(movie,g,fingerprint,sid,start);
                CREATE INDEX hits_order ON hit_locations(movie,g,fingerprint,sid,start);
                CREATE TABLE extent_domains(movie,g,domain_movie,domain_g,domain);
                CREATE TABLE pruned_hits(movie,g,sid,start,PRIMARY KEY(movie,g,sid,start));
                CREATE VIEW hits AS SELECT e.movie,e.g,h.fingerprint,h.sid,h.start
                    FROM extent_domains e JOIN hit_locations h
                    ON h.movie=e.domain_movie AND h.g=e.domain_g
                    WHERE NOT EXISTS(SELECT 1 FROM pruned_hits p WHERE p.movie=e.movie
                        AND p.g=e.g AND p.sid=h.sid AND p.start=h.start);
                CREATE TRIGGER prune_hit INSTEAD OF DELETE ON hits BEGIN
                    INSERT OR IGNORE INTO pruned_hits VALUES(OLD.movie,OLD.g,OLD.sid,OLD.start);
                END;''')
            db.executemany('INSERT INTO hit_locations VALUES(?,?,?,?,?)', rows)
            db.executemany('INSERT INTO extent_domains VALUES(?,?,?,?,NULL)',
                           ((movie.digest, i, movie.digest, i) for i in range(len(domains))))
            db.execute('INSERT INTO extent_domains VALUES(?,?,?,?,NULL)',
                       ('other-movie', 0, movie.digest, 0))
            if overlay == 'saved':
                db.executescript('''ATTACH DATABASE ':memory:' AS saved_associations;
                    CREATE TABLE saved_associations.hit_locations(domain,fingerprint,sid,start);
                    CREATE INDEX saved_associations.hits_order ON hit_locations(domain,fingerprint,sid,start);
                    INSERT INTO saved_associations.hit_locations
                        SELECT 'saved-' || g,fingerprint,sid,start FROM main.hit_locations;
                    UPDATE extent_domains SET domain='saved-' || domain_g;
                    DROP VIEW hits;
                    CREATE TEMP VIEW hits AS SELECT e.movie,e.g,h.fingerprint,h.sid,h.start
                        FROM extent_domains e JOIN saved_associations.hit_locations h ON h.domain=e.domain
                        WHERE NOT EXISTS(SELECT 1 FROM pruned_hits p WHERE p.movie=e.movie
                            AND p.g=e.g AND p.sid=h.sid AND p.start=h.start);
                    CREATE TEMP TRIGGER prune_hit INSTEAD OF DELETE ON hits BEGIN
                        INSERT OR IGNORE INTO pruned_hits VALUES(OLD.movie,OLD.g,OLD.sid,OLD.start);
                    END;''')
        else:
            db.executescript('CREATE TABLE hits(movie,g,fingerprint,sid,start);'
                             'CREATE INDEX hits_order ON hits(movie,g,fingerprint,sid,start);')
            db.executemany('INSERT INTO hits VALUES(?,?,?,?,?)', rows)
        return db, engine.groups_for(movie), pool, source, rows

    def choices(self, db, groups, pool, *, strict=True):
        stats = counters()
        with phase('fixture') as task:
            contents = engine._content_assemblies(db, groups, pool, stats, task, strict=strict)
            try:
                result = [choices for witnesses in contents for choices in witnesses]
            finally:
                contents.close()
        return result, stats

    def test_bad_first_sample_eliminates_exponential_suffix_before_any_attempt(self):
        db, groups, pool, _, _ = self.fixture([[nal(4)]] + [[nal(1), nal(5)]]*18)
        choices, stats = self.choices(db, groups, pool)
        self.assertEqual(choices, [])
        self.assertEqual(stats['initial_avc_fingerprints_rejected'], 1)
        self.assertEqual(stats['content_tuples_considered'], 0)
        self.assertEqual(stats['content_prefixes_checked'], 0)

    def test_all_surviving_content_and_physical_alternatives_remain(self):
        domains = [[nal(4), nal(5), nal(5), nal(1)], [nal(1, b'DEF'), nal(5, b'GHI')]]
        db, groups, pool, _, rows = self.fixture(domains)
        baseline, _ = self.choices(db, groups, pool, strict=False)
        rejected = (rows[0][3], rows[0][4])
        expected = {choices for choices in baseline if choices[0] != rejected}
        actual, stats = self.choices(db, groups, pool)
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(actual), 6)
        self.assertEqual(stats['initial_avc_fingerprints_rejected'], 1)

    def test_only_complete_initial_known_nonpicture_packets_are_pruned(self):
        sei = b'\0\0\0\5\6\6\1\xc0\x80'
        for codec in (b'avc1', b'avc3'):
            for sample in (nal(3), nal(4), nal(3)+nal(4), sei+nal(4), sei):
                with self.subTest(codec=codec, sample=sample):
                    db, groups, pool, _, _ = self.fixture([[sample]], codec=codec)
                    self.assertTrue(valid_sample(sample, codec, 4))
                    self.assertEqual(self.choices(db, groups, pool)[0], [])
        for sample in (nal(1), nal(2), nal(5), sei+nal(5), nal(4)+nal(1), nal(19), nal(20), nal(21)):
            with self.subTest(kept=sample):
                db, groups, pool, _, _ = self.fixture([[sample]])
                self.assertEqual(len(self.choices(db, groups, pool)[0]), 1)

    def test_later_samples_hevc_and_structural_mode_remain_eligible(self):
        for domains, codec, strict in (([[nal(5)], [nal(4)]], b'avc1', True),
                                       ([[nal(4)]], b'hvc1', True),
                                       ([[nal(4)]], b'avc1', False)):
            with self.subTest(domains=domains, codec=codec, strict=strict):
                db, groups, pool, _, _ = self.fixture(domains, codec=codec)
                self.assertEqual(len(self.choices(db, groups, pool, strict=strict)[0]), 1)

    def test_large_initial_sample_declines_optional_screen_without_reading(self):
        db, groups, pool, _, _ = self.fixture([[nal(4)]])
        groups[0].chunks = (replace(groups[0].chunks[0], sizes=(16*1024*1024+1,)),)
        stats = counters()
        with phase('fixture') as task, patch.object(pool, 'read', side_effect=AssertionError('unbounded read')):
            engine._prune_initial_avc(db, groups, pool, stats, task, engine._HitQueries(db), {})
        self.assertEqual(stats['initial_avc_fingerprints_checked'], 0)
        self.assertEqual(db.execute('SELECT count(*) FROM hits').fetchone()[0], 1)

    def test_unavailable_witness_tries_equal_content_alternative(self):
        db, groups, pool, _, rows = self.fixture([[nal(4), nal(4)]])
        original = pool.read
        calls = []
        def read(sid, start, length):
            calls.append(start)
            if start == rows[0][4]:
                raise FormatError('unavailable in this scope')
            return original(sid, start, length)
        with patch.object(pool, 'read', read):
            choices, stats = self.choices(db, groups, pool)
        self.assertEqual(choices, [])
        self.assertEqual(calls, [row[4] for row in rows])
        self.assertEqual(stats['initial_avc_locations_rejected'], 2)

    def test_no_available_read_does_not_create_content_negative(self):
        db, groups, pool, _, _ = self.fixture([[nal(4), nal(4)]])
        with patch.object(pool, 'read', side_effect=FormatError('unavailable')):
            _, stats = self.choices(db, groups, pool)
        self.assertEqual(stats['initial_avc_fingerprints_rejected'], 0)
        self.assertEqual(db.execute('SELECT count(*) FROM hits').fetchone()[0], 2)

    def test_source_change_aborts_before_any_negative_is_applied(self):
        db, groups, pool, source, _ = self.fixture([[nal(4)]])
        original = pool.read
        def changed(sid, start, length):
            result = original(sid, start, length)
            source.write_bytes(source.read_bytes()+b'changed')
            return result
        with patch.object(pool, 'read', changed), self.assertRaises(SafetyError):
            self.choices(db, groups, pool)
        self.assertEqual(db.execute('SELECT count(*) FROM hits').fetchone()[0], 1)

    def test_keyset_pages_cover_all_fingerprints_without_revisiting_deleted_rows(self):
        db, groups, pool, _, _ = self.fixture([[nal(4, i.to_bytes(3, 'big')) for i in range(600)]])
        choices, stats = self.choices(db, groups, pool)
        self.assertEqual(choices, [])
        self.assertEqual(stats['initial_avc_fingerprints_checked'], 600)
        self.assertEqual(stats['initial_avc_fingerprints_rejected'], 600)

    def test_alias_overlay_keeps_shared_association_rows_and_other_movie(self):
        for overlay in (True, 'saved'):
            with self.subTest(overlay=overlay):
                db, groups, pool, _, _ = self.fixture([[nal(4), nal(5)]], overlay=overlay)
                choices, stats = self.choices(db, groups, pool)
                self.assertEqual(len(choices), 1)
                self.assertEqual(stats['initial_avc_fingerprints_rejected'], 1)
                self.assertEqual(db.execute('SELECT count(*) FROM main.hit_locations').fetchone()[0], 2)
                if overlay == 'saved':
                    self.assertEqual(db.execute('SELECT count(*) FROM saved_associations.hit_locations').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT count(*) FROM hits WHERE movie=?', ('other-movie',)).fetchone()[0], 2)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
class InitialAvcDecoderParityTests(unittest.TestCase):
    def test_actual_partition_only_first_packet_fails_baseline_and_skips_optimized_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'generated.mp4'
            generate(source, bframes=True)
            original = source.read_bytes()
            movie = movie_from(original)
            tools = doctor()
            self.assertTrue(validate(source, movie, tools, 30)['ok'])
            first = next(c for c in movie.chunks if c.track == 0 and c.sample_start == 0)
            data = bytearray(original)
            position = first.offset
            for packet in iter_sample_nals(original[position:position+first.sizes[0]], b'avc1', 4):
                position += 4
                data[position] = (data[position] & 0xe0) | 4
                position += len(packet)
            source.write_bytes(data)
            self.assertTrue(valid_sample(data[first.offset:first.offset+first.sizes[0]], b'avc1', 4))
            baseline = validate(source, movie, tools, 30)
            self.assertFalse(baseline['ok'])
            self.assertEqual(baseline['reason'], 'strict_decode_failed')
            with Case(root/'case') as case:
                sid = case.register_source(source)
                scan_logical(case, sid)
                with ReaderPool(case.sources()) as pool, \
                        patch.object(engine, 'reconstruct', side_effect=AssertionError('bad branch copied')), \
                        patch.object(engine, 'validate', side_effect=AssertionError('bad branch decoded')):
                    engine.deep_search(case, [movie], pool, tools, engine.Options(deep=True), 'first-packet')
                job = case.job('ordinary', movie.digest)
                self.assertEqual(job['detail']['candidates'], [])
                self.assertGreater(job['detail']['initial_avc_fingerprints_rejected'], 0)
                self.assertEqual(job['detail']['tested_assemblies'], 0)
            self.assertEqual(source.read_bytes(), data)
