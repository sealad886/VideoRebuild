"""Parallel source reads retain ordered evidence and bounded cancellation."""
import threading
import unittest

from videorebuild.parallel_sources import stream_source_scans


class Pool:
    def __init__(self):
        self.readers = []
        self.lock = threading.Lock()

    def fork(self, ids):
        parent = self

        class Reader:
            closed = False

            def __enter__(self):
                self.ids = tuple(ids)
                self.thread = threading.get_ident()
                with parent.lock:
                    parent.readers.append(self)
                return self

            def check(self, sid):
                assert sid in self.ids
                assert self.thread == threading.get_ident()

            def __exit__(self, *_):
                self.closed = True

        return Reader()


class Task:
    def __init__(self):
        self.completed = 0
        self.threads = []

    def advance(self, count):
        self.completed += count
        self.threads.append(threading.get_ident())


class ParallelSourceTests(unittest.TestCase):
    def test_source_order_private_readers_and_coordinator_progress(self):
        pool, task = Pool(), Task()
        second_started = threading.Event()

        def scan(reader, maximum, wanted, opt, stats, progress, *, source_ids):
            sid, = source_ids
            reader.check(sid)
            if sid == 1:
                self.assertTrue(second_started.wait(2))
            else:
                second_started.set()
            self.assertTrue(wanted(6))
            self.assertEqual(maximum, 10)
            for offset in range(5):
                progress.update(offset + 1)
                stats['anchor_checks'] += 1
                yield sid, offset, 6

        events = list(stream_source_scans(pool, [(1, [6]), (2, [6])], scan,
                                         None, task, 2, batch_size=2))
        self.assertEqual([sid for sid, batch, _ in events if batch is None], [1, 2])
        self.assertEqual([location for _, batch, _ in events if batch for location in batch],
                         [(sid, offset, 6) for sid in (1, 2) for offset in range(5)])
        self.assertEqual(task.completed, 10)
        self.assertEqual(set(task.threads), {threading.get_ident()})
        self.assertEqual(len(pool.readers), 2)
        self.assertTrue(all(reader.closed for reader in pool.readers))

    def test_error_does_not_issue_source_completion_receipt(self):
        pool, task = Pool(), Task()

        def scan(reader, maximum, wanted, opt, stats, progress, *, source_ids):
            yield source_ids[0], 0, 6
            raise RuntimeError('synthetic read failure')

        events = []
        with self.assertRaisesRegex(RuntimeError, 'synthetic read failure'):
            for event in stream_source_scans(pool, [(1, [6]), (2, [6])], scan,
                                            None, task, 2, batch_size=1):
                events.append(event)
        self.assertFalse(any(batch is None for _, batch, _ in events))
        self.assertTrue(all(reader.closed for reader in pool.readers))

    def test_close_cancels_backpressured_workers_and_closes_readers(self):
        pool, task = Pool(), Task()

        def scan(reader, maximum, wanted, opt, stats, progress, *, source_ids):
            for offset in range(100_000):
                yield source_ids[0], offset, 6

        events = stream_source_scans(pool, [(sid, [6]) for sid in range(4)], scan,
                                    None, task, 2, batch_size=1, queue_depth=1)
        next(events)
        events.close()
        self.assertLessEqual(len(pool.readers), 2)
        self.assertTrue(all(reader.closed for reader in pool.readers))

    def test_worker_bounds_rejected(self):
        for workers in (0, 9, 1.5, True):
            with self.assertRaises(ValueError):
                list(stream_source_scans(Pool(), [], None, None, Task(), workers))

    def test_shared_target_profile_is_prepared_once_for_many_sources(self):
        class Profile(list):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        lengths = Profile([6, 11, 16])

        def scan(reader, maximum, wanted, opt, stats, progress, *, source_ids):
            self.assertTrue(all(wanted(length) for length in (6, 11, 16)))
            yield source_ids[0], 0, 6

        events = list(stream_source_scans(Pool(), [(sid, lengths) for sid in range(20)],
                                         scan, None, Task(), 2))
        self.assertEqual(sum(batch is None for _, batch, _ in events), 20)
        self.assertEqual(lengths.iterations, 2)  # maximum and bitset build, not per source


if __name__ == '__main__':
    unittest.main()
