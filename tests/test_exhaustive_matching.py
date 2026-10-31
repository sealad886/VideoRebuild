import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from helpers import generate
from videorebuild import engine
from videorebuild.bmff import Chunk, FormatError
from videorebuild.storage import Case, ReaderPool, scan_logical


class ExhaustiveMatchingTests(unittest.TestCase):
    def test_all_whole_candidates_and_large_padding_are_searched_by_default(self):
        for options, expected_attempts, expected_limited in (
                (engine.Options(), 530, False),
                (engine.Options(max_candidates=512), 512, True),
                (engine.Options(max_padding=1024*1024), 0, True)):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'payloads.bin'
                padding = 2*1024*1024
                source.write_bytes(bytes(530*16+padding))
                movie = SimpleNamespace(digest='synthetic', chunks=[Chunk(0, 0, 0, (5,), 0)], span=5,
                                        tracks=[SimpleNamespace(handler=b'vide')])
                with Case(root / 'case') as case:
                    sid = case.register_source(source)
                    scan_logical(case, sid)
                    case.db.executemany('''INSERT INTO atoms
                        (source_id,kind,offset,size,header,available,payload_size,movie_digest)
                        VALUES(?,'mdat',?,?,8,?,?,NULL)''',
                        ((sid, i*16, 13+padding, 13+padding, 5+padding) for i in range(530)))
                    case.db.commit()

                    def check_candidate(case, movie, mapping, *args):
                        return {'status': 'valid' if mapping[(0, 0)][1] == 529*16+8 else 'rejected', 'media_sha256': 'last'}

                    with ReaderPool(case.sources()) as pool, \
                            patch.object(engine, 'attempt', side_effect=check_candidate) as attempted, \
                            patch.object(engine, 'finish', return_value='synthetic'):
                        _, successes, limited = engine.whole_payload(case, movie, pool, {}, options, 'test')
                    self.assertEqual(attempted.call_count, expected_attempts)
                    self.assertEqual(limited, expected_limited)
                    self.assertEqual(bool(successes), expected_attempts == 530)

    def test_duplicate_valid_mappings_retain_only_distinct_media_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'payloads.bin'
            source.write_bytes(bytes(100*16))
            movie = SimpleNamespace(digest='synthetic', chunks=[Chunk(0, 0, 0, (5,), 0)], span=5,
                                        tracks=[SimpleNamespace(handler=b'vide')])
            with Case(root / 'case') as case:
                sid = case.register_source(source)
                scan_logical(case, sid)
                case.db.executemany("""INSERT INTO atoms
                    (source_id,kind,offset,size,header,available,payload_size,movie_digest)
                    VALUES(?,'mdat',?,13,8,13,5,NULL)""", ((sid, i*16) for i in range(100)))
                case.db.commit()
                def valid(case, movie, mapping, *args):
                    offset = mapping[(0, 0)][1]
                    return {'status': 'valid', 'media_sha256': 'different' if offset == 99*16+8 else 'same'}
                with ReaderPool(case.sources()) as pool, \
                        patch.object(engine, 'attempt', side_effect=valid) as attempted, \
                        patch.object(engine, 'finish') as finished:
                    _, candidates, limited = engine.whole_payload(case, movie, pool, {}, engine.Options(), 'test')
                self.assertEqual(attempted.call_count, 100)
                self.assertEqual({c['media_sha256'] for c in candidates}, {'same', 'different'})
                self.assertEqual(len(finished.call_args.args[2]), 2)
                self.assertFalse(limited)

    def test_unlimited_deep_hits_and_assemblies_preserve_explicit_limits(self):
        for options, expected, limited in (
                (engine.Options(), 80, False),
                (engine.Options(max_hits=32), 32, True),
                (engine.Options(max_assemblies=64), 64, True)):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'nal-sequences.bin'
                source.write_bytes(bytes.fromhex('000000026588')*80)
                info = source.stat()
                sources = {1: dict(path=str(source), size=info.st_size, mtime_ns=info.st_mtime_ns,
                                   dev=info.st_dev, ino=info.st_ino)}
                movie = SimpleNamespace(digest='synthetic')
                group = SimpleNamespace(key=('synthetic', 0), span=6, chunks=[], movie=movie,
                                        anchors=lambda: [(2, 0)],
                                        mapping=lambda sid, start: {(0, 0): (sid, start)})
                with ReaderPool(sources) as pool, \
                        patch.object(engine, 'groups_for', return_value=[group]), \
                        patch.object(engine, 'check_samples', return_value=True), \
                        patch.object(engine, '_group_hash', side_effect=lambda g, m, p: str(m[(0, 0)][1])), \
                        patch.object(engine, 'attempt', return_value={'status': 'rejected', 'content_evaluated': True}) as attempted, \
                        patch.object(engine, 'finish') as finished:
                    engine.deep_search(SimpleNamespace(), [movie], pool, {}, options, 'test')
                self.assertEqual(attempted.call_count, expected)
                self.assertEqual(finished.call_args.args[3], limited)
                self.assertEqual(finished.call_args.args[6]['tested_assemblies'], expected)

    def test_incomplete_whole_search_reaches_deep_and_retains_incompleteness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'original.mp4'
            generate(source, audio=False, seconds=0.25)
            with Case(root / 'case') as case:
                scan_logical(case, case.register_source(source))
                deferred = []
                with patch.object(engine, 'doctor', return_value={}), \
                        patch.object(engine, 'whole_payload', return_value=('search_limited', [], True)), \
                        patch.object(engine, 'deep_search', side_effect=lambda case, movies, *args, **kwargs: deferred.extend(movies)) as deep:
                    engine.recover(case, engine.Options(deep=True))
                deep.assert_called_once()
                movie = deferred[0]
                self.assertIn(movie.digest, deep.call_args.kwargs['incomplete'])

                # A later successful deep attempt cannot erase the incomplete
                # whole-payload search's unresolved uniqueness question.
                data = bytes.fromhex('000000026588')
                separate = root / 'section.bin'
                separate.write_bytes(data)
                info = separate.stat()
                sources = {1: dict(path=str(separate), size=len(data), mtime_ns=info.st_mtime_ns,
                                   dev=info.st_dev, ino=info.st_ino)}
                group = SimpleNamespace(key=(movie.digest, 0), span=6, chunks=[], movie=movie,
                                        anchors=lambda: [(2, 0)],
                                        mapping=lambda sid, start: {(0, 0): (sid, start)})
                with ReaderPool(sources) as pool, patch.object(engine, 'groups_for', return_value=[group]), \
                        patch.object(engine, 'check_samples', return_value=True), \
                        patch.object(engine, '_group_hash', return_value='one'), \
                        patch.object(engine, 'attempt', return_value={'status': 'valid', 'media_sha256': 'same'}), \
                        patch.object(engine, 'finish') as finished:
                    engine.deep_search(case, [movie], pool, {}, engine.Options(), 'test', incomplete={movie.digest})
                self.assertTrue(finished.call_args.args[3])

    def test_attempt_cache_reused_across_search_policy_but_not_scope_exclusion(self):
        for status in ('valid', 'rejected'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'samples.bin'
                source.write_bytes(bytes.fromhex('0000000165'))
                movie = SimpleNamespace(digest='synthetic', chunks=[Chunk(0, 0, 0, (5,), 0)])
                with Case(root / 'case') as case:
                    sid = case.register_source(source)
                    scan_logical(case, sid)
                    mapping = {(0, 0): (sid, 0)}
                    candidate = case.root / 'candidates' / 'cached.mp4'
                    candidate.write_bytes(source.read_bytes())
                    cached = {'status': status, 'tools': {}, 'timeout': 300.0,
                              'validation_revision': engine.VALIDATION_REVISION,
                              'path': 'candidates/cached.mp4', 'sha256': hashlib.sha256(candidate.read_bytes()).hexdigest()}
                    digest = engine.mapping_digest(movie, mapping)
                    case.db.execute('INSERT INTO attempts(movie_digest,mapping_digest,stage,status,detail) VALUES(?,?,?,?,?)',
                                    (movie.digest, digest, 'whole_mdat', status, json.dumps(cached)))
                    case.db.commit()
                    with ReaderPool(case.sources()) as pool, patch.object(engine, 'check_samples') as checked:
                        reused = engine.attempt(case, movie, mapping, pool, {}, engine.Options(), 'whole_mdat')
                        self.assertEqual(reused['status'], cached['status'])
                        if status == 'valid':
                            checked.assert_not_called()
                        else:
                            self.assertEqual(checked.call_count, 1)
                        with patch.object(pool, 'provenance', side_effect=FormatError('source range excluded by allocation scope')):
                            blocked = engine.attempt(case, movie, mapping, pool, {}, engine.Options(), 'whole_mdat')
                        self.assertEqual(blocked['status'], 'rejected')
                        self.assertIn('allocation', blocked['reason'])
                        self.assertEqual(json.loads(case.db.execute('SELECT detail FROM attempts').fetchone()[0]), cached)


if __name__ == '__main__':
    unittest.main()
