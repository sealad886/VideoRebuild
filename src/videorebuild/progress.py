"""Shared, throttled progress for command-line operations; never writes stdout."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import sys
import time
from typing import Callable, Iterator, TextIO

_ACTIVE: ContextVar[Reporter | None] = ContextVar('videorebuild_progress', default=None)
_UNSET = object()


def _text(value: object) -> str:
    return ''.join(character if character.isprintable() else ' ' for character in str(value))


class Reporter:
    def __init__(self, *, enabled: bool = True, stream: TextIO | None = None,
                 interval: float = 5.0, clock: Callable[[], float] | None = None):
        self.enabled = enabled
        self.stream = sys.stderr if stream is None else stream
        self.interval = interval
        self.clock = time.monotonic if clock is None else clock
        self.log: Callable[[str], None] | None = None

    def emit(self, message: str) -> None:
        if not self.enabled:
            return
        # Feedback is best effort: a closed progress pipe must not abort recovery.
        try:
            self.stream.write(message + '\n')
            self.stream.flush()
        except (OSError, ValueError):
            pass
        if self.log is not None:
            try:
                self.log(message)
            except (OSError, ValueError):
                pass


@contextmanager
def reporting(*, enabled: bool = True, stream: TextIO | None = None,
              interval: float = 5.0, clock: Callable[[], float] | None = None) -> Iterator[Reporter]:
    reporter = Reporter(enabled=enabled, stream=stream, interval=interval, clock=clock)
    token = _ACTIVE.set(reporter)
    try:
        yield reporter
    finally:
        _ACTIVE.reset(token)


class Phase:
    def __init__(self, label: str, *, total: int | None = None, unit: str = 'items'):
        self.reporter = _ACTIVE.get()
        self.label, self.total, self.unit = label, total, unit
        self.completed = 0
        self.detail = ''
        self.final_status = 'finished'
        self.counts: dict[str, object] = {}
        self.started = self.last_emitted = 0.0

    def __enter__(self) -> Phase:
        if self.reporter is not None and self.reporter.enabled:
            self.started = self.last_emitted = self.reporter.clock()
            self._emit('started', self.started)
        return self

    def _emit(self, status: str, now: float) -> None:
        elapsed = max(0.0, now - self.started)
        count = f'{self.completed:,}'
        if self.total is not None:
            count += f'/{self.total:,}'
        count += f' {self.unit}'
        if self.total is not None and self.total > 0:
            count += f' ({100 * self.completed / self.total:.1f}%)'
        message = f'Progress: {_text(self.label)} | {status} | {count} | elapsed {elapsed:.1f}s'
        # Byte throughput supports a useful estimate; candidate/record costs vary.
        if self.unit == 'bytes' and self.completed > 0 and elapsed > 0:
            rate = self.completed / elapsed
            message += f' | {rate / 1024**2:.2f} MiB/s'
            if status == 'running' and self.total is not None and self.completed < self.total:
                message += f' | ETA ~{(self.total - self.completed) / rate:.1f}s'
        if self.counts:
            message += ' | ' + ', '.join(f'{_text(key)}={_text(value)}' for key, value in self.counts.items())
        if self.detail:
            message += ' | ' + _text(self.detail)
        self.reporter.emit(message)
        self.last_emitted = now

    def update(self, completed: int | None = None, *, total: int | None | object = _UNSET,
               detail: str | None = None, **counts: object) -> None:
        if self.reporter is None or not self.reporter.enabled:
            return
        if completed is not None:
            self.completed = completed
        if total is not _UNSET:
            self.total = total
        if detail is not None:
            self.detail = detail
        self.counts.update(counts)
        now = self.reporter.clock()
        if now - self.last_emitted >= self.reporter.interval:
            self._emit('running', now)

    def advance(self, amount: int = 1, *, detail: str | None = None, **counts: object) -> None:
        self.update(self.completed + amount, detail=detail, **counts)

    def heartbeat(self, *, detail: str | None = None, **counts: object) -> None:
        self.update(detail=detail, **counts)

    def outcome(self, status: str, *, detail: str | None = None) -> None:
        """Record a non-exception outcome such as a timeout or search limit."""
        self.final_status = status
        if detail is not None:
            self.detail = detail

    def __exit__(self, kind, value, traceback) -> bool:
        if self.reporter is not None and self.reporter.enabled:
            status = 'interrupted' if kind is not None and issubclass(kind, KeyboardInterrupt) else (
                'failed' if kind is not None else self.final_status)
            self._emit(status, self.reporter.clock())
        return False


def phase(label: str, *, total: int | None = None, unit: str = 'items') -> Phase:
    """Report actual work; finishing never manufactures a completed denominator."""
    return Phase(label, total=total, unit=unit)
