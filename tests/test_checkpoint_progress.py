"""Recovery commits must remain checkpointable while work queues are visited."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_deep_witnesses import structural_movie
from videorebuild import engine
from videorebuild.storage import Case, ReaderPool, scan_logical


class CheckpointProgressTests(unittest.TestCase):
    def checkpoint_commit(self, case, label):
        case.event('checkpoint_fixture', {'label': label, 'data': 'x' * 8192})
        busy, frames, copied = case.db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        self.assertEqual(busy, 0)
        self.assertEqual(copied, frames, 'Recovery cursor pins committed work in the WAL')

    def test_catalogue_cursor_does_not_pin_recovery_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            movies = [structural_movie([100 + i], [8]) for i in range(3)]
            source = root / 'metadata.bin'
            source.write_bytes(b''.join(m.data for m in movies))
            with Case(root / 'case') as case:
                scan_logical(case, case.register_source(source))
                visited = []
                def whole(case, movie, *args):
                    visited.append(movie.digest)
                    self.checkpoint_commit(case, movie.digest)
                    return 'unresolved', [], False
                with patch.object(engine, 'whole_payload', side_effect=whole):
                    engine.recover(case, engine.Options(reconstruct_only=True), limit=2)
                self.assertEqual(visited, sorted(m.digest for m in movies)[:2])

    def test_mdat_cursor_does_not_pin_candidate_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            movie = structural_movie([0], [8])
            source = root / 'candidates.bin'
            source.write_bytes(bytes(96))
            with Case(root / 'case') as case:
                sid = case.register_source(source)
                scan_logical(case, sid)
                case.db.executemany('''INSERT INTO atoms
                    (source_id,kind,offset,size,header,available,payload_size)
                    VALUES(?,'mdat',?,16,8,16,8)''', ((sid, i * 16) for i in range(6)))
                case.db.commit()
                visited = []
                def attempt(case, movie, mapping, *args):
                    visited.append(mapping[(0, 0)][1])
                    self.checkpoint_commit(case, str(visited[-1]))
                    return {'status': 'rejected'}
                with ReaderPool(case.sources()) as pool, patch.object(engine, 'attempt', side_effect=attempt):
                    engine.whole_payload(case, movie, pool, {}, engine.Options(max_candidates=3), 'fixture')
                self.assertEqual(visited, [8, 24, 40])
                self.assertEqual(case.job('ordinary', movie.digest)['status'], 'search_limited')


if __name__ == '__main__':
    unittest.main()
