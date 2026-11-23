# Rust matching core

## Decision and ownership

The 0.6.0 development change starts from released v0.5.0, commit
`e257562a0194cec863bb89eb74d76a2c0847f844`. Its tree is identical to the local
implementation baseline `43671fdf4ecb20419fa0e47df9d494ffbe615f81`.
Implementation and synthetic verification use the existing checkout. Recovery
processes were not signaled or restarted; the frozen archive, interpreter
executable and case/media volumes were not changed. The shared project environment
was modified before ownership was checked; [TEST_REPORT.md](../TEST_REPORT.md)
records that isolation violation and its disposition.

The first profile identified Python CPU overhead in complete sample validation,
NAL-chain discovery and physical support pruning. In synthetic cProfile runs,
78% of a 100,000-sample validation workload was in sample/NAL classification;
80% of dense 32 KiB structural discovery was in cumulative NAL traversal and
callbacks. These observations select the migration boundary; they are not
before/after throughput measurements.

Rust owns bounded byte parsing and physical-domain computation through the direct
`videorebuild._native` PyO3 extension. Calls process sample batches, scan batches
or complete bounded domains. Python owns the CLI/TUI, metadata models, source
read authorization, scope/identity checks, indexes, durable coverage, search
coordination, reconstruction I/O, validation subprocesses and reporting.

| Boundary | Required behavior |
| --- | --- |
| NAL discovery | Preserve unaligned and overlapping starts, cumulative multi-NAL lengths, short AVC boundaries, complete wanted lengths and resumable bounded work |
| Sample validation | Check every complete encoded sample; preserve AVC/HEVC framing, length-field widths and classification rules |
| Physical support | Preserve every feasible physical witness, physical alias identities and original candidate order; budget exhaustion returns unpruned domains |
| Reads and writes | ReaderPool remains the only source read path; the coordinator retains database, receipt and publication ownership |

Native code cannot open a source, nominate an unavailable read, mark coverage
complete or publish a recovery. Source/path/descriptor checks and allocation /
acquisition preflights still happen in Python. A parser fact never substitutes
for those checks. Buffer ownership, checked arithmetic and explicit batch limits
prevent a native parser from turning malformed lengths into unchecked offsets.
Bounded calls give Python regular opportunities to process cancellation.

The fragmented Python API also accepts mutable buffers and views. It checks
sample bounds and snapshots only the selected sample into immutable bytes before
calling Rust. The normal immutable-buffer path needs no sample copy. Rust never
borrows a mutable Python buffer while running without the GIL.

## Alternatives and distribution

Per-NAL FFI callbacks retain the measured overhead, so the binding uses coarse
batches. A separate Rust CLI would add a second orchestration and evidence path.
A Python runtime fallback would retain competing production algorithms. Instead,
the superseded Python algorithms are retained only as differential test oracles.

PyO3's stable CPython ABI targets Python 3.13 and later on supported macOS/Linux
platforms. Setuptools-rust extends the existing build backend, preserving the
single canonical Python version literal. Maturin's default dynamic version comes
from Cargo metadata and would require an additional version synchronization
mechanism. Rust crate metadata is an internal implementation identity, not the
VideoRebuild release version.

Native wheels avoid requiring Rust at installation. Source builds require Cargo,
Rust and a platform linker. Standalone archives must carry a native library for
their stated platform; the former universal pure-Python archive cannot represent
the new runtime. Package checks must bind native bytes as well as Python source.

## Verification and migration gates

Implementation acceptance requires differential comparisons with the immutable
baseline, exhaustive small assignment oracles, deterministic generated malformed
inputs and Rust boundary tests, the full Python suite, Rust tests and lints,
independent review, installed-package consumers and reproducible before/after
benchmarks. Compare candidate/output sets and encoded bytes; faster rejection
alone does not establish equivalent recovery.

The existing first-tier/optional second-tier policy, owner exemptions, scoped
zero-hit and partial coverage, all feasible witnesses, ambiguity and incomplete
search outcomes remain required. Existing interruption/resume and ordered
provenance contracts remain required, including sequence and per-track timing.
Temporary and persistent search stores remain under `CASE/work`.

Publication remains a hard link to the validated candidate, or that candidate's
own path when links are unavailable. Completed-job resume must preserve the
canonical manifest. The two-distinct-movie `limit=1` then resume regression and
manifest-mutation regression must continue to pass. `.DS_Store` remains excluded
from discovery, indexing, browsing and benchmark enumeration without deleting it.

No case schema or media conversion is needed for a parser implementation change.
Existing source evidence and publications remain intact. Rollback means selecting
the previously installed release for a future command; it does not replace a
running process or roll back evidence already written by another run. The only
authorized release activity here is local 0.6.0 preparation. Publication requires
separate approval and supported-platform evidence.

Actual measurements and final readiness are recorded in
[TEST_REPORT.md](../TEST_REPORT.md) and [PERFORMANCE.md](PERFORMANCE.md).
