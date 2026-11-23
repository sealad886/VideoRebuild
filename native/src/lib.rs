//! CPU-only recovery kernels. Python owns evidence access and publication.
#![forbid(unsafe_code)]

mod constraints;
mod nal;
mod scanner;

use pyo3::prelude::*;

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(nal::sample_kind, module)?)?;
    module.add_function(wrap_pyfunction!(nal::nal_spans, module)?)?;
    module.add_function(wrap_pyfunction!(nal::fragment_nal_types, module)?)?;
    module.add_function(wrap_pyfunction!(constraints::prune, module)?)?;
    module.add_class::<nal::SampleValidator>()?;
    module.add_class::<scanner::NalScanner>()?;
    module.add_class::<scanner::NalChain>()?;
    module.add_function(wrap_pyfunction!(scanner::nal_header, module)?)?;
    Ok(())
}
