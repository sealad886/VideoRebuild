"""Bounded, exact support pruning for physical candidate domains.

This removes impossible pairwise choices; it does not replace the assembler's
global occupied-range history or prove that the surviving domains are feasible.
"""
from __future__ import annotations

from collections import deque
import heapq


def prune_domains(domains, *, max_edges: int = 200_000, max_checks: int = 800_000):
    """Return supported domains and counters, preserving candidate order.

    A domain entry is ``(key, physical_extents)``; each extent contains
    ``(device, inode, start, end)``.  Different groups must not overlap the same
    physical bytes.  A sweep builds only overlapping candidate-pair conflicts.
    If that derived graph exceeds ``max_edges`` or the sweep performs more than
    ``max_checks`` overlapping extent comparisons, return the original domains
    unchanged. The second bound also covers repeated extents that describe the
    same candidate conflict. This fallback is never an absence conclusion.
    """
    if max_edges < 0 or max_checks < 0:
        raise ValueError('constraint budgets must be nonnegative')
    original = [list(domain) for domain in domains]
    stats = dict(constraint_complete=True, constraint_conflicts=0,
                 constraint_overlap_checks=0, constraint_support_checks=0,
                 constraint_removed=0)
    if any(not domain for domain in original) or len(original) < 2:
        return original, stats

    intervals = []
    for group, domain in enumerate(original):
        for candidate, (_, extents) in enumerate(domain):
            for device, inode, start, end in extents:
                if end < start:
                    raise ValueError('physical extent ends before its start')
                if end > start:
                    intervals.append((device, inode, start, end, group, candidate))
    intervals.sort()
    conflicts = [[{} for _ in domain] for domain in original]
    neighbors = [set() for _ in original]
    active = {}
    expiry = []
    identity = None
    for token, (device, inode, start, end, group, candidate) in enumerate(intervals):
        key = device, inode
        if key != identity:
            active.clear()
            expiry.clear()
            identity = key
        while expiry and expiry[0][0] <= start:
            _, old_token, old_group = heapq.heappop(expiry)
            del active[old_group][old_token]
            if not active[old_group]:
                del active[old_group]
        for other_group, entries in active.items():
            if other_group == group:
                continue
            for other_candidate in entries.values():
                stats['constraint_overlap_checks'] += 1
                if stats['constraint_overlap_checks'] > max_checks:
                    stats['constraint_complete'] = False
                    return original, stats
                previous = conflicts[group][candidate].get(other_group, 0)
                bit = 1 << other_candidate
                if previous & bit:
                    continue
                stats['constraint_conflicts'] += 1
                if stats['constraint_conflicts'] > max_edges:
                    stats['constraint_complete'] = False
                    return original, stats
                conflicts[group][candidate][other_group] = previous | bit
                reverse = conflicts[other_group][other_candidate]
                reverse[group] = reverse.get(group, 0) | (1 << candidate)
                neighbors[group].add(other_group)
                neighbors[other_group].add(group)
        active.setdefault(group, {})[token] = candidate
        heapq.heappush(expiry, (end, token, group))

    alive = [(1 << len(domain)) - 1 for domain in original]
    queue = deque((group, other) for group, values in enumerate(neighbors)
                  for other in sorted(values))
    queued = set(queue)
    while queue:
        group, other = queue.popleft()
        queued.remove((group, other))
        remaining = alive[group]
        removed = 0
        while remaining:
            bit = remaining & -remaining
            remaining -= bit
            candidate = bit.bit_length() - 1
            stats['constraint_support_checks'] += 1
            blocked = conflicts[group][candidate].get(other, 0)
            if blocked & alive[other] == alive[other]:
                removed |= bit
        if not removed:
            continue
        alive[group] &= ~removed
        stats['constraint_removed'] += removed.bit_count()
        if not alive[group]:
            # An empty domain already proves there is no full assignment.
            break
        for neighbor in sorted(neighbors[group]):
            arc = neighbor, group
            if arc not in queued:
                queue.append(arc)
                queued.add(arc)
    return [[entry for candidate, entry in enumerate(domain)
             if alive[group] & (1 << candidate)]
            for group, domain in enumerate(original)], stats
