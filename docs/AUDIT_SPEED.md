# Performance and accuracy audit — 2026-10-05

## Scope and acceptance

Audit both recovery backends for avoidable CPU, I/O, and allocation cost. Preserve
every-sample framing checks, exact packet hashes, packet/frame counts, strict decode,
ambiguity reporting, source identity checks, and contiguous decoded-prefix semantics.
Use synthetic data and locally generated media only. Existing recovery cases and
private source images are outside this audit.

Done criteria: reproduce bottlenecks, implement bounded canonical-path fixes,
measure comparable before/after trials, run accuracy regressions, and obtain
independent review. This checkout has no Git metadata; no branch or commit exists.

## Evidence index and lifecycle

- `engine.check_samples`: ordinary recovery screening before candidate decoding.
- `ReaderPool.read/check`: read bounds and filesystem identity checks.
- `image_recovery.validate_prefix/run/export_report`: packet verification,
  source-window publication, and catalogue export.
- `tests/test_performance.py`: deterministic read-window and accuracy regressions.
- `tests/test_fragmented.py`: decoder-tested prefix preservation and reporting.
- `test-results/audit-speed/`: baseline snapshots, benchmark scripts/results,
  suite logs, and local archive diagnostics.
- Existing architecture and acceptance: `docs/ARCHITECTURE.md`,
  `docs/FRAGMENTED.md`, `docs/SUPPORT.md`; prior audit remains historical evidence.

The installed delivery profiles have no exact audit-with-remediation profile.
The minimum route is scoped audit and quality evidence → bounded design and
implementation → fresh verification and independent review → this handoff.
Medium risk: cached reads affect recovery screening. External coordination,
release, migration, and worktrees are not applicable. No dependency added.
Required orchestrator 1.4.1 path was absent; installed 1.5.0 was used.
Codanna index scope and source symbols were verified; Graphify queried and updated.
Requested verification-before-completion and systematic-debugging skill files
were unavailable; their evidence and root-cause principles were applied directly.

## Findings and remediation ledger

| ID | Severity | Root cause and patch | Status |
| --- | --- | --- | --- |
| S1 | Medium | Full sample screening performed a filesystem stat, seek, and read per sample. Read bounded windows, validate each exact sample slice, and recheck identity after each chunk. Quick screening retains sparse reads. | fixed, verification recorded below |
| S2 | Medium at large catalogue size | Successful fragmented runs exported the complete catalogue twice. Suppress final export only after the first export succeeds; retain exception/interruption export. | fixed, verification recorded below |
| S3 | Low | Packet hashing and prefix writes copied large immutable byte slices. Use memoryview slices for identical hashes and writes. | fixed, verification recorded below |
| S4 | Low | Repeated identical moov metadata is parsed before database deduplication. A 100-copy synthetic case took about 9 ms. | accepted residual risk: measured cost is small; retain current implementation in this performance slice |

S1 design: windows are at most 4 MiB unless a single existing sample is larger;
clip each read to the chunk end. Reload at the sample start when a sample crosses
the window boundary. Every sample still reaches `valid_sample`. Read bounds,
short-read failures, and source identity errors remain enforced by ReaderPool.
Two windows can coexist transiently during replacement; peak is bounded, not
claimed to be exactly 4 MiB. Quick checks and small chunks can incur a little
extra identity/summing overhead. This does not weaken decoding acceptance.

S2 design: set the successful-export flag only after export returns. Exceptions
before that point keep the existing final report attempt. No database schema,
result format, ordering, CLI option, exit code, or checkpoint rule changes.

S3 design follows Python's buffer API: memoryview exposes existing bytes without
copying; hashlib accepts bytes-like input. Primary references:
[Python 3.13 memoryview](https://docs.python.org/3.13/library/stdtypes.html#memoryview)
and [hashlib](https://docs.python.org/3.13/library/hashlib.html).

Rollback trigger: any encoded-byte, sample acceptance, source-mutation,
interruption/resume, or decoding regression. Original source snapshots are kept
in the evidence directory; original archive and checksum manifest were backed up
under `/tmp/videorebuild-speed-*`. Any rollback must preserve existing case data.

## Measurements and verification

Baseline: Python 3.13.16, FFmpeg/ffprobe 9.0.2, macOS arm64. Existing suite:
103 tests passed in 27.927 s; after source patches, the same 103 passed in 28.732 s.
Suite duration is not a recovery throughput benchmark.

Three comparable trials of 100,000 six-byte AVC samples (600,000 source bytes):
baseline median 1.010 s, patched median 0.1065 s, approximately 9.5× faster.
Reads fall from 100,000 to one; both modes read 600,000 bytes and return valid
framing. This is sample screening, not a whole-recovery speed claim. Command:
`PYTHONPATH=src .venv/bin/python test-results/audit-speed/sample-probe.py`.
Raw runs: `sample-results.json`; baseline source: `engine-before.py`.

Five trials exporting 100,000 results: median 0.3833 s and 11,588,890 JSONL bytes
per pass. Successful recovery now avoids one complete pass; no claim of a 2×
whole-recovery speedup. Regression proves one actual export still includes all
results, interruption exports committed results, and failed export retries.
Command: `PYTHONPATH=src .venv/bin/python test-results/audit-speed/report-memory-probe.py --results /tmp/videorebuild-report-probe.json`.
Recorded trials: `report-memory-results.json`.

The same five-trial probe hashes a 64 MiB slice: identical SHA-256 with traced
slice allocation falling from 67,109,130 bytes to 545 bytes. These figures exclude
the original input, parser data, process RSS, and decoder allocations. Timings
were noisy (bytes median 0.0339 s, view 0.0452 s), so S3 is a memory improvement,
not a measured hashing speedup. Existing generated AVC/HEVC tests verify exact
output bytes and packet hashes through real ffprobe and strict FFmpeg decode.

Final verification:

- Python 3.13 source: 112 tests passed in 27.997 s (`final-unittest-313.log`).
- Python 3.11 source: 112 tests passed in 27.333 s (`final-unittest-311.log`).
- Rebuilt zipapp on Python 3.13: 112 tests passed in 26.310 s
  (`zipapp-unittest.log`); doctor passed (`zipapp-doctor.json`).
- Independent source/spec and final test/benchmark review approved S1–S3.
- Nine new regressions plus existing encoded-byte preservation, ambiguity,
  short/truncated input, corrupted publication, timed prefix, and decode tests.
- Graphify refreshed after final changes. Audit-owned checksum entries refreshed.

No accepted fix remains open. S4 is a bounded low-cost tradeoff. Whole-recovery
benchmarking and ordinary report allocation investigation remain proof gaps,
not passing performance claims.

## Boundaries

Synthetic screening, hashing, and catalogue measurements isolate mechanisms.
They do not establish full-image recovery speed or disk capacity. No private media,
full-image workload, Linux runtime, or hosted CI run is included. Required full
decode passes and resume publication hashes remain deliberate accuracy/integrity
costs. Ordinary report memory scaling is an unmeasured follow-up hypothesis;
it is not classified as a confirmed defect.

The rebuilt zipapp is a local development artifact. Existing wheel/sdist remain
older artifacts and were not rebuilt. Version remains 0.2.0. No commit, push,
pull request, publication, deployment, release, or worktree was created.
