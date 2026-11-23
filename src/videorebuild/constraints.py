"""Bounded, exact support pruning for physical candidate domains.

This removes impossible pairwise choices; it does not replace the assembler's
global occupied-range history or prove that the surviving domains are feasible.
"""
from __future__ import annotations

from . import _native


def prune_domains(domains, *, max_edges: int = 200_000, max_checks: int = 800_000):
    """Prune exact physical support in Rust; budget exhaustion retains originals."""
    if max_edges < 0 or max_checks < 0:
        raise ValueError('constraint budgets must be nonnegative')
    original = [list(domain) for domain in domains]
    retained, complete, conflicts, overlaps, supports, removed = _native.prune(
        [[tuple(extents) for _, extents in domain] for domain in original], max_edges, max_checks)
    stats = dict(constraint_complete=complete, constraint_conflicts=conflicts,
                 constraint_overlap_checks=overlaps, constraint_support_checks=supports,
                 constraint_removed=removed)
    return [[domain[index] for index in indexes] for domain, indexes in zip(original, retained)], stats
