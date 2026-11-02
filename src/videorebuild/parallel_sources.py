"""Bounded source discovery with private readers and ordered coordinator output."""
from concurrent.futures import ThreadPoolExecutor
from queue import Full, Queue
from threading import Event


class _Cancelled(Exception):
    pass


def stream_source_scans(pool, tasks, scan, opt, task, workers,
                        *, batch_size=4096, queue_depth=2):
    """Yield ``(sid, batch, final_stats)`` in input source order.

    Workers use independent reader handles and never receive the case database.
    Only the coordinator consumes postings and publishes completion receipts.
    A ``None`` batch marks successful completion; errors propagate instead. At
    most ``workers`` tasks are submitted. Each worker retains at most
    ``queue_depth`` queued batches plus its current producer batch; the
    coordinator can hold one additional yielded batch.
    Closing the iterator cancels production, joins workers and closes readers.
    """
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError('search workers must be an integer from 1 to 8')
    if batch_size < 1 or queue_depth < 1:
        raise ValueError('discovery queue bounds must be positive')
    stopped = Event()
    profiles = {}

    def send(queue, message):
        while not stopped.is_set():
            try:
                queue.put(message, timeout=0.05)
                return
            except Full:
                pass
        raise _Cancelled()

    class WorkerTask:
        def __init__(self, queue):
            self.queue = queue
            self.completed = 0

        def advance(self, count):
            self.completed += count
            send(self.queue, ('progress', count))

        def update(self, completed=None, **_):
            if completed is not None:
                delta = completed - self.completed
                self.completed = completed
                if delta:
                    send(self.queue, ('progress', delta))
            self.heartbeat()

        def heartbeat(self, **_):
            if stopped.is_set():
                raise _Cancelled()

    def produce(sid, profile, queue):
        state = dict(nal_checks=0, anchor_checks=0, limited=False,
                     stop_reason=None, discovery_header_reads=0,
                     discovery_header_cache_hits=0)
        try:
            maximum, bits = profile

            def wanted(length):
                return bool(bits[length >> 3] & (1 << (length & 7)))

            with pool.fork((sid,)) as reader:
                batch = []
                for location in scan(reader, maximum + 4, wanted, opt, state,
                                     WorkerTask(queue), source_ids=(sid,)):
                    if stopped.is_set():
                        raise _Cancelled()
                    batch.append(location)
                    if len(batch) == batch_size:
                        send(queue, ('batch', tuple(batch)))
                        batch.clear()
                if batch:
                    send(queue, ('batch', tuple(batch)))
                reader.check(sid)
            send(queue, ('done', state))
        except _Cancelled:
            pass
        except BaseException as error:
            if not stopped.is_set():
                try:
                    send(queue, ('error', error))
                except _Cancelled:
                    pass

    pending = []
    source_tasks = iter(tasks)
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='videorebuild-discovery')

    def submit():
        item = next(source_tasks, None)
        if item is None:
            return False
        sid, lengths = item
        if not lengths:
            raise ValueError('discovery task needs at least one length')
        # Coverage planning shares one canonical tuple per missing target set.
        # Identity lookup avoids rehashing/iterating a huge tuple for every
        # source. Keep its reference so an object id cannot be recycled.
        identity = id(lengths)
        saved = profiles.get(identity)
        if saved is None:
            maximum = max(lengths)
            bits = bytearray((maximum >> 3) + 1)
            for length in lengths:
                bits[length >> 3] |= 1 << (length & 7)
            saved = (lengths, (maximum, bytes(bits)))
            profiles[identity] = saved
        queue = Queue(maxsize=queue_depth)
        future = executor.submit(produce, sid, saved[1], queue)
        pending.append((sid, queue, future))
        return True

    try:
        for _ in range(workers):
            if not submit():
                break
        while pending:
            sid, queue, future = pending.pop(0)
            while True:
                kind, value = queue.get()
                if kind == 'progress':
                    task.advance(value)
                elif kind == 'batch':
                    yield sid, value, None
                elif kind == 'error':
                    raise value
                else:
                    future.result()
                    yield sid, None, value
                    break
            submit()
    finally:
        stopped.set()
        executor.shutdown(wait=True, cancel_futures=True)
