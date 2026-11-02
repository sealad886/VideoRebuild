"""Exact pruning must retain every feasible physical assignment."""
import itertools
import random
import unittest

from videorebuild.constraints import prune_domains


def entry(key, start, end, *, device=1, inode=1):
    return key, ((device, inode, start, end),)


def feasible(domains):
    result = set()
    for choices in itertools.product(*domains):
        extents = [extent for _, footprint in choices for extent in footprint]
        valid = all(a[:2] != b[:2] or a[3] <= b[2] or b[3] <= a[2]
                    for index, a in enumerate(extents) for b in extents[index + 1:])
        if valid:
            result.add(tuple(key for key, _ in choices))
    return result


class ConstraintDomainsTests(unittest.TestCase):
    def test_alternative_witness_and_aliases_are_preserved(self):
        domains = [[entry('overlap', 0, 16), entry('alternate', 32, 48)],
                   [entry('required', 8, 32)]]
        pruned, stats = prune_domains(domains)
        self.assertEqual(feasible(pruned), feasible(domains))
        self.assertEqual([key for key, _ in pruned[0]], ['alternate'])
        self.assertEqual(stats['constraint_removed'], 1)

    def test_half_open_ranges_and_distinct_physical_sources(self):
        domains = [[entry('first', 0, 8)],
                   [entry('adjacent', 8, 16), entry('different', 0, 8, inode=2)]]
        pruned, stats = prune_domains(domains)
        self.assertEqual(pruned, domains)
        self.assertEqual(stats['constraint_conflicts'], 0)

    def test_support_loss_propagates_across_groups(self):
        domains = [[entry('a0', 0, 4), entry('a1', 8, 12)],
                   [entry('b0', 0, 4), entry('b1', 4, 8)],
                   [entry('c', 4, 12)]]
        pruned, stats = prune_domains(domains)
        self.assertEqual(feasible(pruned), feasible(domains))
        self.assertTrue(any(not domain for domain in pruned))
        self.assertGreaterEqual(stats['constraint_removed'], 2)

    def test_pairwise_consistency_does_not_claim_global_feasibility(self):
        domains = [[entry((group, slot), slot * 4, slot * 4 + 4)
                    for slot in range(2)] for group in range(3)]
        pruned, stats = prune_domains(domains)
        self.assertEqual(pruned, domains)
        self.assertEqual(feasible(pruned), set())
        self.assertTrue(stats['constraint_complete'])

    def test_resource_limit_falls_back_without_partial_exclusion(self):
        domains = [[entry('a', 0, 8)], [entry('b', 0, 8)], [entry('c', 0, 8)]]
        pruned, stats = prune_domains(domains, max_edges=1)
        self.assertEqual(pruned, domains)
        self.assertFalse(stats['constraint_complete'])
        self.assertEqual(stats['constraint_removed'], 0)

    def test_disjoint_domains_generate_no_pair_graph(self):
        domains = [[entry((group, choice), group * 100 + choice * 2,
                          group * 100 + choice * 2 + 1)
                    for choice in range(20)] for group in range(20)]
        pruned, stats = prune_domains(domains)
        self.assertEqual(pruned, domains)
        self.assertEqual(stats['constraint_overlap_checks'], 0)
        self.assertEqual(stats['constraint_support_checks'], 0)

    def test_duplicate_extent_work_is_bounded_separately_from_unique_edges(self):
        repeated = ((1, 1, 0, 8),) * 100
        domains = [[('a', repeated)], [('b', repeated)]]
        pruned, stats = prune_domains(domains, max_edges=10, max_checks=32)
        self.assertEqual(pruned, domains)
        self.assertFalse(stats['constraint_complete'])
        self.assertEqual(stats['constraint_overlap_checks'], 33)
        self.assertEqual(stats['constraint_conflicts'], 1)
        self.assertEqual(stats['constraint_removed'], 0)

    def test_small_exhaustive_oracle(self):
        rng = random.Random(7331)
        for _ in range(150):
            domains = []
            for group in range(rng.randrange(2, 5)):
                domain = []
                for choice in range(rng.randrange(1, 5)):
                    start = rng.randrange(12)
                    domain.append(entry((group, choice), start,
                                        start + rng.randrange(1, 5), inode=rng.randrange(1, 3)))
                domains.append(domain)
            pruned, _ = prune_domains(domains)
            self.assertEqual(feasible(pruned), feasible(domains))


if __name__ == '__main__':
    unittest.main()
