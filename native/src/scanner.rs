//! Resumable structural nominations over authorized immutable buffers.
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::HashMap;

type Header = (u64, bool);
type ScanBatch = (
    Vec<(u64, u64)>,
    u64,
    u64,
    u64,
    Option<u64>,
    bool,
    bool,
    usize,
);

pub fn advance_chain(
    position: u64,
    total: u64,
    n: u64,
    legal: bool,
    source: u64,
    maximum: u64,
) -> Option<(u64, u64)> {
    let next_total = total.checked_add(4)?.checked_add(n)?;
    let next_position = position.checked_add(4)?.checked_add(n)?;
    (legal && n >= 1 && next_total <= maximum && next_position <= source)
        .then_some((next_position, next_total))
}

pub fn header_fact(raw: &[u8]) -> Option<Header> {
    let prefix = raw.get(..5)?;
    let n = u64::from(u32::from_be_bytes([
        prefix[0], prefix[1], prefix[2], prefix[3],
    ]));
    let second = if n >= 2 { Some(*raw.get(5)?) } else { None };
    let first = prefix[4];
    let avc = first & 128 == 0 && (1..=23).contains(&(first & 31));
    let hevc = first & 128 == 0 && (first >> 1) & 63 <= 40 && second.is_some_and(|b| b & 7 != 0);
    Some((n, avc || hevc))
}

#[pyfunction]
pub fn nal_header(raw: &[u8]) -> Header {
    header_fact(raw).unwrap_or((0, false))
}

#[pyclass]
pub struct NalChain {
    position: u64,
    total: u64,
    source_size: u64,
    maximum: u64,
    visits: usize,
}

#[pymethods]
impl NalChain {
    #[new]
    fn new(position: u64, source_size: u64, maximum: u64) -> Self {
        Self {
            position,
            total: 0,
            source_size,
            maximum,
            visits: 0,
        }
    }
    fn position(&self) -> Option<u64> {
        (self.visits < 65536
            && self.position <= self.source_size
            && self.source_size - self.position >= 5)
            .then_some(self.position)
    }
    fn accept(&mut self, n: u64, legal: bool) -> Option<u64> {
        self.visits += 1;
        let (position, total) = advance_chain(
            self.position,
            self.total,
            n,
            legal,
            self.source_size,
            self.maximum,
        )?;
        self.position = position;
        self.total = total;
        Some(total - 4)
    }
}

#[pyclass]
pub struct NalScanner {
    base: u64,
    source_size: u64,
    maximum: u64,
    owned: usize,
    high: u8,
    at: usize,
    last_start: usize,
    chain: Option<(u64, u64, u64, usize)>, // start, position, total, visits
    pending: bool,
    heads: HashMap<u64, Header>,
    wanted: Option<Vec<u8>>,
    done: bool,
}

#[pymethods]
impl NalScanner {
    #[new]
    #[pyo3(signature=(base, owned, source_size, maximum, wanted=None))]
    fn new(
        base: u64,
        owned: usize,
        source_size: u64,
        maximum: u64,
        wanted: Option<Vec<u8>>,
    ) -> PyResult<Self> {
        if maximum < 4 || base > source_size || owned as u64 > source_size - base {
            return Err(PyValueError::new_err("invalid scanner bounds"));
        }
        Ok(Self {
            base,
            source_size,
            maximum,
            owned,
            high: (((maximum - 4) >> 16) & 255) as u8,
            at: 0,
            last_start: 0,
            chain: None,
            pending: false,
            heads: HashMap::new(),
            wanted,
            done: false,
        })
    }

    fn supply_header(&mut self, length: u64, legal: bool) -> PyResult<()> {
        if !self.pending {
            return Err(PyValueError::new_err("no pending NAL header"));
        }
        let Some((_, position, _, _)) = self.chain else {
            return Err(PyValueError::new_err("missing pending NAL chain"));
        };
        self.cache(position, (length, legal));
        Ok(())
    }

    /// Bound each call by visits, nominations, and bytes scanned for responsive
    /// interruption and worker cancellation even on zero-filled input.
    #[pyo3(signature=(data, remaining=None, max_rows=4096))]
    fn scan(
        &mut self,
        py: Python<'_>,
        data: &[u8],
        remaining: Option<u64>,
        max_rows: usize,
    ) -> PyResult<ScanBatch> {
        if data.len() < self.owned {
            return Err(PyValueError::new_err(
                "scanner buffer shorter than owned range",
            ));
        }
        if !(1..=4096).contains(&max_rows) {
            return Err(PyValueError::new_err("scanner batch bound must be 1..4096"));
        }
        py.detach(|| self.run(data, remaining, max_rows))
    }
}

impl NalScanner {
    fn cache(&mut self, position: u64, header: Header) {
        if self.heads.len() >= 8192 {
            self.heads.clear();
        }
        self.heads.insert(position, header);
    }

    fn header(&self, data: &[u8], position: u64) -> Option<Header> {
        let local = usize::try_from(position.checked_sub(self.base)?).ok()?;
        header_fact(data.get(local..)?)
    }

    fn run(&mut self, data: &[u8], remaining: Option<u64>, max_rows: usize) -> PyResult<ScanBatch> {
        let (mut visits, mut reads, mut hits) = (0u64, 0u64, 0u64);
        let mut rows = Vec::new();
        let mut requested = None;
        let mut limited = false;
        let scan_end = self.at.saturating_add(256 * 1024).min(self.owned);
        while !self.done && visits < 4096 && rows.len() < max_rows {
            if self.chain.is_none() {
                let mut found = None;
                while self.at < self.owned && self.at < scan_end {
                    let at = self.at;
                    self.at += 1;
                    if let Some(prefix) = data.get(at..).and_then(|tail| tail.get(..4)) {
                        if prefix[0] == 0
                            && prefix[1] <= self.high
                            && prefix.iter().any(|&b| b != 0)
                        {
                            found = Some(at);
                            break;
                        }
                    }
                }
                let Some(at) = found else {
                    self.done = self.at >= self.owned;
                    break;
                };
                self.last_start = at;
                let start = self
                    .base
                    .checked_add(at as u64)
                    .ok_or_else(|| PyValueError::new_err("scanner offset overflow"))?;
                self.chain = Some((start, start, 0, 0));
            }
            let Some((start, position, total, chain_visits)) = self.chain else {
                break;
            };
            if chain_visits >= 65536 {
                self.chain = None;
                continue;
            }
            if !self.pending {
                if remaining.is_some_and(|budget| visits >= budget) {
                    limited = true;
                    self.done = true;
                    break;
                }
                visits += 1;
                if position > self.source_size || self.source_size - position < 5 {
                    self.chain = None;
                    continue;
                }
            }
            let header = if let Some(&value) = self.heads.get(&position) {
                if !self.pending {
                    hits += 1;
                }
                value
            } else {
                reads += 1;
                if let Some(value) = self.header(data, position) {
                    self.cache(position, value);
                    value
                } else {
                    self.pending = true;
                    requested = Some(position);
                    break;
                }
            };
            self.pending = false;
            let (n, legal) = header;
            let Some((next_position, next_total)) =
                advance_chain(position, total, n, legal, self.source_size, self.maximum)
            else {
                self.chain = None;
                continue;
            };
            let length = next_total - 4;
            let wanted = match &self.wanted {
                None => true,
                Some(bits) => usize::try_from(length >> 3)
                    .ok()
                    .and_then(|i| bits.get(i))
                    .is_some_and(|byte| byte & (1 << (length & 7)) != 0),
            };
            if wanted {
                rows.push((start, length));
            }
            self.chain = Some((start, next_position, next_total, chain_visits + 1));
        }
        Ok((
            rows,
            visits,
            reads,
            hits,
            requested,
            self.done,
            limited,
            self.last_start,
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn overflow_and_zero_buffers_are_bounded() {
        assert!(NalScanner::new(u64::MAX, 1, u64::MAX, 32, None).is_err());
        let mut scanner = NalScanner::new(0, 1024 * 1024, 1024 * 1024, 32, None).unwrap();
        let bytes = vec![0; 1024 * 1024];
        let first = scanner.run(&bytes, None, 4096).unwrap();
        assert!(!first.5);
        for _ in 0..4 {
            scanner.run(&bytes, None, 4096).unwrap();
        }
        assert!(scanner.done);
    }

    #[test]
    fn chain_budget_preserves_cumulative_starts() {
        let data = [0, 0, 0, 1, 0x65, 0, 0, 0, 1, 0x65];
        let mut scanner = NalScanner::new(0, data.len(), data.len() as u64, 32, None).unwrap();
        let result = scanner.run(&data, Some(2), 4096).unwrap();
        assert_eq!(result.0, vec![(0, 1), (0, 6)]);
        assert!(result.6);
    }
}
