//! Exact bounded sparse support propagation. Budget exhaustion returns originals.
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, BinaryHeap, VecDeque};

type Extent = (i128, i128, i128, i128);
type Domains = Vec<Vec<Vec<Extent>>>;
type Pruned = (Vec<Vec<usize>>, bool, usize, usize, usize, usize);

#[pyfunction]
pub fn prune(
    py: Python<'_>,
    domains: Domains,
    max_edges: usize,
    max_checks: usize,
) -> PyResult<Pruned> {
    py.detach(|| prune_inner(&domains, max_edges, max_checks).map_err(PyValueError::new_err))
}

fn prune_inner(
    domains: &Domains,
    max_edges: usize,
    max_checks: usize,
) -> Result<Pruned, &'static str> {
    let original: Vec<Vec<usize>> = domains
        .iter()
        .map(|domain| (0..domain.len()).collect())
        .collect();
    let (mut edges, mut checks, mut supports, mut removed) = (0, 0, 0, 0);
    if domains.len() < 2 || domains.iter().any(Vec::is_empty) {
        return Ok((original, true, edges, checks, supports, removed));
    }
    let mut intervals = Vec::new();
    for (group, domain) in domains.iter().enumerate() {
        for (candidate, extents) in domain.iter().enumerate() {
            for &(device, inode, start, end) in extents {
                if end < start {
                    return Err("physical extent ends before its start");
                }
                if end > start {
                    intervals.push((device, inode, start, end, group, candidate));
                }
            }
        }
    }
    intervals.sort_unstable();
    let mut conflicts: Vec<Vec<BTreeMap<usize, Vec<u64>>>> = domains
        .iter()
        .map(|domain| domain.iter().map(|_| BTreeMap::new()).collect())
        .collect();
    let mut neighbors = vec![BTreeSet::new(); domains.len()];
    let mut active: BTreeMap<usize, BTreeMap<usize, usize>> = BTreeMap::new();
    let mut active_order = Vec::new();
    let mut expiry = BinaryHeap::new();
    let mut identity = None;
    for (token, &(device, inode, start, end, group, candidate)) in intervals.iter().enumerate() {
        if identity != Some((device, inode)) {
            active.clear();
            active_order.clear();
            expiry.clear();
            identity = Some((device, inode));
        }
        while let Some(&Reverse((old_end, old_token, old_group))) = expiry.peek() {
            if old_end > start {
                break;
            }
            expiry.pop();
            if let Some(entries) = active.get_mut(&old_group) {
                entries.remove(&old_token);
                if entries.is_empty() {
                    active.remove(&old_group);
                    active_order.retain(|&g| g != old_group);
                }
            }
        }
        for &other_group in &active_order {
            if other_group == group {
                continue;
            }
            for &other_candidate in active[&other_group].values() {
                checks += 1;
                if checks > max_checks {
                    return Ok((original, false, edges, checks, supports, removed));
                }
                let blocked = conflicts[group][candidate]
                    .entry(other_group)
                    .or_insert_with(|| vec![0; domains[other_group].len().div_ceil(64)]);
                let (word, bit) = (other_candidate / 64, 1u64 << (other_candidate % 64));
                if blocked[word] & bit != 0 {
                    continue;
                }
                edges += 1;
                if edges > max_edges {
                    return Ok((original, false, edges, checks, supports, removed));
                }
                blocked[word] |= bit;
                let reverse = conflicts[other_group][other_candidate]
                    .entry(group)
                    .or_insert_with(|| vec![0; domains[group].len().div_ceil(64)]);
                reverse[candidate / 64] |= 1u64 << (candidate % 64);
                neighbors[group].insert(other_group);
                neighbors[other_group].insert(group);
            }
        }
        if !active.contains_key(&group) {
            active_order.push(group);
        }
        active.entry(group).or_default().insert(token, candidate);
        expiry.push(Reverse((end, token, group)));
    }
    let mut alive: Vec<Vec<bool>> = domains
        .iter()
        .map(|domain| vec![true; domain.len()])
        .collect();
    let mut queue: VecDeque<_> = neighbors
        .iter()
        .enumerate()
        .flat_map(|(group, values)| values.iter().map(move |&other| (group, other)))
        .collect();
    let mut queued: BTreeSet<_> = queue.iter().copied().collect();
    while let Some((group, other)) = queue.pop_front() {
        queued.remove(&(group, other));
        let mut rejected = Vec::new();
        for candidate in 0..domains[group].len() {
            if !alive[group][candidate] {
                continue;
            }
            supports += 1;
            let blocked = conflicts[group][candidate].get(&other);
            let unsupported = alive[other].iter().enumerate().all(|(index, &present)| {
                !present
                    || blocked.is_some_and(|bits| bits[index / 64] & (1u64 << (index % 64)) != 0)
            });
            if unsupported {
                rejected.push(candidate);
            }
        }
        if rejected.is_empty() {
            continue;
        }
        removed += rejected.len();
        for candidate in rejected {
            alive[group][candidate] = false;
        }
        if !alive[group].iter().any(|&present| present) {
            break;
        }
        for &neighbor in &neighbors[group] {
            if queued.insert((neighbor, group)) {
                queue.push_back((neighbor, group));
            }
        }
    }
    let retained = alive
        .iter()
        .map(|domain| {
            domain
                .iter()
                .enumerate()
                .filter_map(|(index, &present)| present.then_some(index))
                .collect()
        })
        .collect();
    Ok((retained, true, edges, checks, supports, removed))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fallback_and_zero_width() {
        let domains = vec![vec![vec![(1, 2, 0, 5)]], vec![vec![(1, 2, 4, 8)]]];
        assert!(!prune_inner(&domains, 0, 100).unwrap().1);
        assert!(prune_inner(&domains, 100, 100).unwrap().0[0].is_empty());
        let disjoint = vec![vec![vec![(1, 2, 5, 5)]], vec![vec![(1, 2, 4, 8)]]];
        assert_eq!(
            prune_inner(&disjoint, 100, 100).unwrap().0,
            vec![vec![0], vec![0]]
        );
    }
}
