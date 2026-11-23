fn main() {
    let config = pyo3_build_config::get();
    assert!(
        config.implementation() == pyo3_build_config::PythonImplementation::CPython
            && !config.is_free_threaded(),
        "VideoRebuild requires GIL-enabled CPython; other interpreters cannot use its cp313-abi3 wheels"
    );
    pyo3_build_config::add_extension_module_link_args();
}
