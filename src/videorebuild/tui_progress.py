"""Interpret bounded progress observations; never infer recovery completion."""
from __future__ import annotations

from datetime import datetime
import math
import re


_PROGRESS = re.compile(
    r'^(?:(?P<timestamp>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) INFO )?'
    r'Progress: (?P<label>[^|\r\n]+) \| (?P<status>[a-z_]+) \| '
    r'(?P<completed>[0-9,]{1,30})(?:/(?P<total>[0-9,]{1,30}))? '
    r'(?P<unit>[a-zA-Z_]+)(?: \([0-9.]+%\))? \| '
    r'elapsed (?P<elapsed>[0-9.]{1,30})s(?P<rest>(?: \| .*)?)$')
_ACTIVE = {'started', 'running'}
_SCOPE_NOTE = 'Phase work only; overall recovery total and ETA are unknown.'


def _count(value: str | None) -> int | None:
    if value is None:
        return None
    # Reject malformed separators rather than silently normalizing them.
    if ',' in value and re.fullmatch(r'\d{1,3}(?:,\d{3})+', value) is None:
        raise ValueError('Invalid progress count')
    return int(value.replace(',', ''))


def _record(line: str) -> dict | None:
    match = _PROGRESS.fullmatch(line)
    if match is None or len(match['label']) > 1024:
        return None
    try:
        elapsed = float(match['elapsed'])
        if not math.isfinite(elapsed):
            return None
        stamp = (datetime.strptime(match['timestamp'], '%Y-%m-%d %H:%M:%S,%f').timestamp()
                 if match['timestamp'] else None)
        return {'label': match['label'], 'status': match['status'],
                'completed': _count(match['completed']), 'total': _count(match['total']),
                'unit': match['unit'], 'elapsed_seconds': elapsed, 'timestamp': stamp,
                # Scanner placeholder counters in older runtimes are not verified
                # discovery totals. Preserve those in the raw log, not in bars.
                'detail': ''}
    except (ValueError, OverflowError, OSError):
        return None


def progress_observations(lines: list[str], *, busy: bool | None, now: float,
                          run_started: float | None = None) -> tuple[list[dict], str]:
    """Return at most three recent phase observations from already bounded text.

    Counts come from complete log records, never job-history percentages. Nested
    starts/ends delimit phases when present in the tail; missing starts do not
    invent a parent or denominator. A writer lock is only evidence of a writer,
    not verification that a particular process or phase is still alive.
    """
    active: list[dict] = []
    latest = None
    error_context = False
    for line in lines:
        if ' ERROR ' in line or line.startswith(('ERROR:', 'Traceback (')):
            active.clear()
            error_context = True
            continue
        record = _record(line)
        if record is None:
            continue
        stamp = record['timestamp']
        # recovery_started is committed just after its enclosing phase starts;
        # logging timestamps have millisecond precision, events use floats.
        if run_started is not None and (stamp is None or stamp < run_started - 1):
            continue
        error_context = False
        label = record['label']
        position = next((i for i, item in enumerate(active) if item['label'] == label), None)
        if record['status'] in _ACTIVE:
            if position is None:
                active.append(record)
            elif record['status'] == 'started':
                # A repeated start is a new invocation, not cumulative progress.
                active[position:] = [record]
            else:
                active[position] = record
        elif position is not None:
            del active[position:]
        latest = record
    # Small terminals must show the measured inner work before opaque parents.
    selected = list(reversed(active[-3:]))
    if latest is not None and latest not in selected:
        selected = [latest, *selected[:2]]
    observations = []
    for record in selected:
        item = dict(record)
        stamp = item.pop('timestamp')
        # A newer unrelated log write cannot make an old phase fresh.
        age = max(0.0, now - stamp) if stamp is not None else None
        stale = age is None or age > 30
        historical = busy is not True or error_context
        completed, total = item['completed'], item['total']
        if total is not None and completed > total:
            item['detail'] = 'Reported count exceeds its total; percentage unavailable.'
        fraction = completed / total if total is not None and total > 0 and completed <= total else None
        if item['status'] not in _ACTIVE | {'finished'} and fraction == 1:
            fraction = None
        eta = None
        if (not stale and not historical and item['status'] == 'running' and
                item['unit'] == 'bytes' and fraction is not None and 0 < completed < total and
                item['elapsed_seconds'] > 0):
            eta = (total - completed) * item['elapsed_seconds'] / completed
            if not math.isfinite(eta):
                eta = None
        item.update(fraction=fraction, eta_seconds=eta,
                    eta_basis='phase_average' if eta is not None else None,
                    age_seconds=age, stale=stale, historical=historical)
        observations.append(item)
    note = _SCOPE_NOTE
    if busy is not True:
        note += ' No verified active phase; showing last logged observations.'
    elif error_context:
        note += ' An error followed the last progress record.'
    elif not observations:
        note += ' Waiting for a complete progress record from this run.'
    return observations, note
