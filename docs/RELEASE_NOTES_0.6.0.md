# VideoRebuild 0.6.0 (pending)

VideoRebuild 0.6.0 adds TUI progress, restoration of missing generated files,
recovered-video comparison, and a bundled Rust matching core. The added
features and native build requirements make this a minor increment from v0.5.0
under the project's pre-1.0 policy.

## TUI progress and recovery resume

The read-only TUI watch screen reports actual phase progress.

- Show actual completed/total counts and units for reported phases.
- Keep unknown, empty or inconsistent totals indeterminate.
- Retain observed work and outcomes for interrupted, failed and limited phases.
- Mark stale and historical observations, suppressing their ETA.
- Label fresh byte-phase ETAs as estimates based on the phase-average rate.
- Keep overall recovery completion and ETA unknown; historical job counts
  never provide an overall percentage.
- Bound observation to recent logs and events without reading attempt history
  or media. Closing watch mode leaves recovery running.

Existing case data and log formats require no migration. The viewer can observe
an already running recovery without restarting it. Recovery matching, source
reads, validation and second-tier defaults are unchanged by this TUI update.
Existing logs have no unique phase IDs; the viewer reports only phase evidence
available in its bounded log tail and does not invent missing parent totals.

Recovery resume also gains targeted restoration of missing generated files:

- Restore absent ordinary candidates/publications and fragmented outputs from
  verified case copies or retained source provenance, requiring the accepted hash.
- Recreate missing fragmented manifests and validation documents from committed
  proof without changing their recorded decode-validation scope.
- Preserve source checks, accepted job decisions and durable search progress;
  changed surviving artifacts and insufficient reconstruction proof remain errors.
- Retain recovery error messages in the case log after report refresh.

## Recovered-video comparison

Use `videorebuild compare` to review videos missing from an archive.

- Recursively compare a recovered folder against a transferred-video archive,
  keeping both input trees read-only and reports in a separate output folder.
- Recognize identical encoded video in different MP4 containers, including a
  complete recovered clip contained in a longer archived recording.
- Keep unmatched, unreadable and uncertain visual candidates in `missing.csv`.
  Optional decoded-image suggestions require review and never establish absence.
- Reuse a local metadata cache on subsequent runs and report comparison errors
  separately from missing candidates. Video matching does not establish audio
  equivalence, original recording completeness or recovery provenance.

See [comparison usage and evidence limits](COMPARISON.md).

## Native matching and installation

The bundled Rust extension handles matching and sample validation while Python
continues to coordinate recovery and own source reads.

- Process structural NAL discovery in bounded native batches while retaining
  the existing origin/structural indexes and scope-qualified coverage receipts.
- Validate complete encoded sample batches in Rust, preserving AVC/HEVC framing
  and all-sample checks. Fragmented timing and section decisions remain in Python.
- Compute bounded physical-domain support in Rust while preserving aliases,
  alternative witnesses, deterministic ordering and unpruned budget fallback.
- Keep the Python CLI/TUI, checked source readers, orchestration, reporting and
  reconstruction/publication policy. Encoded bytes are copied without re-encoding.
- Build a native wheel for each supported platform and platform-specific
  standalone archives. Source installation now requires a Rust toolchain and
  platform linker. Prebuilt wheels do not require Rust at installation.

CPython 3.13 and 3.14 remain the supported interpreter matrix. The extension uses
the stable CPython ABI; free-threaded interpreters and PyPy are not qualified.
Distribution remains through GitHub Releases; PyPI publication is not configured.
See [installation](../README.md) and [release procedure](RELEASING.md).

Existing case schemas, source/container indexes and media remain intact.
The version participates in ordinary recovery run identities, so a future run
with this version can reassess prior search decisions. Scope-qualified parser
facts remain reusable only under their bound source and range identities.
First-tier and optional second-tier behavior, ambiguity/incomplete labels,
source immutability, interruption/resume and ordered provenance remain required.
Completed canonical manifests and hard-link/candidate-path publication behavior
remain protected by regressions.

Read [Rust ownership and verification](RUST_CORE.md),
[performance evidence](PERFORMANCE.md) and [executed tests](../TEST_REPORT.md)
before assessing release readiness. Synthetic speed measurements do not establish
private-image recovery throughput or real-camera acceptance. Local process and
environment boundaries, including the shared-environment isolation violation,
are recorded in the validation report.
