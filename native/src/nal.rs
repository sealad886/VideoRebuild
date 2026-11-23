use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

fn framing(codec: &[u8], length: usize) -> Result<bool, &'static str> {
    if !matches!(length, 1 | 2 | 4) {
        return Err("unsupported_NAL_framing");
    }
    match codec {
        b"avc1" | b"avc3" => Ok(false),
        b"hvc1" | b"hev1" => Ok(true),
        _ => Err("unsupported_NAL_framing"),
    }
}

fn walk(
    data: &[u8],
    hevc: bool,
    length: usize,
    limit: usize,
    fragmented: bool,
    mut visit: impl FnMut(usize, &[u8], u8) -> Result<(), &'static str>,
) -> Result<(), &'static str> {
    let mut p = 0;
    let mut count = 0;
    while p < data.len() {
        if data.len() - p < length {
            return Err("truncated_NAL_length");
        }
        let mut n = 0usize;
        for value in &data[p..p + length] {
            n = (n << 8) | usize::from(*value);
        }
        p += length;
        if n < if hevc { 2 } else { 1 } || n > data.len() - p {
            return Err("invalid_NAL_length");
        }
        let nal = &data[p..p + n];
        let typ = if hevc {
            (nal[0] >> 1) & 63
        } else {
            nal[0] & 31
        };
        if nal[0] & 128 != 0 || (hevc && nal[1] & 7 == 0) {
            return Err("invalid_NAL_header");
        }
        if (hevc && typ > 40) || (!hevc && !(1..=23).contains(&typ)) {
            return Err(if fragmented {
                "unsupported_NAL_type"
            } else {
                "invalid_NAL_header"
            });
        }
        count += 1;
        if count > limit {
            return Err("too_many_NALs_in_sample");
        }
        visit(p, nal, typ)?;
        p += n;
    }
    if count == 0 {
        return Err("empty_video_sample");
    }
    Ok(())
}

fn valid_sei(nal: &[u8]) -> bool {
    if nal.first() != Some(&6) {
        return false;
    }
    let mut rbsp = Vec::with_capacity(nal.len().saturating_sub(1));
    let mut zeroes = 0;
    let mut i = 1;
    while i < nal.len() {
        let value = nal[i];
        if zeroes >= 2 {
            if value == 3 {
                if i + 1 == nal.len() || nal[i + 1] > 3 {
                    return false;
                }
                zeroes = 0;
                i += 1;
                continue;
            }
            if value <= 2 {
                return false;
            }
        }
        rbsp.push(value);
        zeroes = if value == 0 { zeroes + 1 } else { 0 };
        i += 1;
    }
    if rbsp.last() != Some(&128) {
        return false;
    }
    let end = rbsp.len() - 1;
    let mut p = 0;
    let mut messages = 0;
    while p < end {
        for field in 0..2 {
            let mut value = 0usize;
            while p < end && rbsp[p] == 255 {
                // Bounded by the input length, but check arithmetic explicitly.
                let Some(next) = value.checked_add(255) else {
                    return false;
                };
                value = next;
                p += 1;
            }
            if p >= end {
                return false;
            }
            let Some(next) = value.checked_add(usize::from(rbsp[p])) else {
                return false;
            };
            value = next;
            p += 1;
            if field == 1 {
                let Some(next) = p.checked_add(value) else {
                    return false;
                };
                p = next;
            }
        }
        if p > end {
            return false;
        }
        messages += 1;
    }
    p == end && messages > 0
}

fn classify(data: &[u8], hevc: bool, length: usize) -> Option<&'static str> {
    let mut vcl = false;
    let mut sei_only = !hevc;
    walk(data, hevc, length, 65536, false, |_, nal, typ| {
        vcl |= if hevc {
            typ <= 31
        } else {
            (1..=5).contains(&typ)
        };
        if !hevc && typ == 6 && !valid_sei(nal) {
            return Err("invalid_SEI");
        }
        sei_only &= typ == 6;
        Ok(())
    })
    .ok()?;
    if vcl {
        Some("vcl")
    } else if sei_only {
        Some("avc_sei")
    } else {
        None
    }
}

#[pyfunction]
pub fn sample_kind(
    py: Python<'_>,
    data: &[u8],
    codec: &[u8],
    length: usize,
) -> Option<&'static str> {
    let hevc = framing(codec, length).ok()?;
    py.detach(|| classify(data, hevc, length))
}

#[pyfunction]
pub fn nal_spans(
    py: Python<'_>,
    data: &[u8],
    codec: &[u8],
    length: usize,
) -> PyResult<Vec<(usize, usize)>> {
    let hevc = framing(codec, length).map_err(PyValueError::new_err)?;
    py.detach(|| {
        let mut result = Vec::new();
        walk(data, hevc, length, 65536, false, |start, nal, _| {
            result.push((start, nal.len()));
            Ok(())
        })
        .map_err(PyValueError::new_err)?;
        Ok(result)
    })
}

#[pyfunction]
pub fn fragment_nal_types(
    py: Python<'_>,
    data: &[u8],
    codec: &[u8],
    length: usize,
    offset: usize,
    size: usize,
) -> PyResult<Vec<u8>> {
    let hevc = framing(codec, length).map_err(PyValueError::new_err)?;
    let data = data
        .get(offset..)
        .and_then(|tail| tail.get(..size))
        .ok_or_else(|| PyValueError::new_err("invalid_NAL_length"))?;
    py.detach(|| {
        let mut result = Vec::new();
        walk(data, hevc, length, 4096, true, |_, _, typ| {
            result.push(typ);
            Ok(())
        })
        .map_err(PyValueError::new_err)?;
        Ok(result)
    })
}

#[pyclass]
pub struct SampleValidator {
    sizes: Vec<usize>,
    index: usize,
    hevc: bool,
    length: usize,
}

#[pymethods]
impl SampleValidator {
    #[new]
    fn new(sizes: Vec<usize>, codec: &[u8], length: usize) -> PyResult<Self> {
        let hevc = framing(codec, length).map_err(PyValueError::new_err)?;
        if sizes
            .iter()
            .any(|&size| size == 0 || size > 64 * 1024 * 1024)
        {
            return Err(PyValueError::new_err("zero or oversized sample"));
        }
        Ok(Self {
            sizes,
            index: 0,
            hevc,
            length,
        })
    }

    /// Validate complete samples in one already-authorized byte window.
    /// The next sample remains untouched if it crosses the window boundary.
    fn validate_window(&mut self, py: Python<'_>, data: &[u8]) -> (usize, usize, bool) {
        py.detach(|| {
            let mut pos = 0;
            let first = self.index;
            while self.index < self.sizes.len() {
                let size = self.sizes[self.index];
                if size > data.len() - pos {
                    break;
                }
                self.index += 1;
                if classify(&data[pos..pos + size], self.hevc, self.length).is_none() {
                    return (self.index - first, pos + size, false);
                }
                pos += size;
            }
            (self.index - first, pos, true)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifications_and_complete_framing() {
        assert_eq!(classify(&[0, 0, 0, 2, 0x65, 0x80], false, 4), Some("vcl"));
        assert_eq!(
            classify(&[0, 0, 0, 4, 6, 5, 0, 128], false, 4),
            Some("avc_sei")
        );
        assert_eq!(classify(&[0, 0, 0, 2, 6, 128], false, 4), None);
        assert_eq!(classify(&[0, 0, 0, 2, 0x65, 0x80, 0], false, 4), None);
    }

    #[test]
    fn malformed_fuzz_never_panics() {
        let mut state = 123456789u64;
        for length in [1, 2, 4] {
            for size in 0..256 {
                let bytes: Vec<u8> = (0..size)
                    .map(|_| {
                        state ^= state << 13;
                        state ^= state >> 7;
                        state ^= state << 17;
                        state as u8
                    })
                    .collect();
                for hevc in [false, true] {
                    let _ = classify(&bytes, hevc, length);
                }
            }
        }
    }
}
