# Recovery performance audits

The first assessment below ran on 1 November 2026. Later assessments appear in
dated sections; each retains its own baseline and evidence limits.

## Rust matching core — 2026-11-02

Baseline: released v0.5.0 at `e257562a0194cec863bb89eb74d76a2c0847f844`,
tree-identical to archived local source `43671fdf4ecb20419fa0e47df9d494ffbe615f81`.
Native implementation: local `codex/rust-core`, source version 0.6.0. Measurements
ran on macOS 27.2 arm64, CPython 3.13.18 and Rust 1.99.1 / PyO3 0.29.2.
All fixtures are synthetic. Recovery and SpeechForge processes were not signaled
or restarted; the frozen recovery archive and interpreter executable were not
changed. The shared-environment mutation found in the final audit is disclosed
in [TEST_REPORT.md](../TEST_REPORT.md). Application indexes were cold on their first pass; the
operating-system file cache was warm. No privileged cache flushing was used.

The baseline profile selected complete encoded-sample validation, structural NAL
discovery and physical-domain support propagation. Python classification consumed
78% of the small-sample validation profile and about 90% of profiled whole-payload
matching. Python NAL traversal/callbacks consumed about 80% of dense discovery.
The migrated kernels preserve indexed nominations, full validation and all
feasible physical witnesses. See [ownership and invariants](RUST_CORE.md).

The same harness ran seven timing trials per workload in isolated subprocesses.
Allocation and cProfile runs were separate from the timing trials. Recovery and
discovery scenarios check their expected candidate/posting sets and encoded-media
hashes where applicable. Parser and support probes check framing validity,
domains and counters. The comparison also verifies discovery digests and
invariant work counters.

| Synthetic workload | Python baseline median | Native median | Baseline / native |
| --- | ---: | ---: | ---: |
| Validate 100,000 × 8-byte samples | 84.54 ms | 1.98 ms | 42.72× |
| Validate 16 MiB / 16,384 samples | 21.60 ms | 2.61 ms | 8.26× |
| Dense discovery, four sources, 1 worker | 338.30 ms | 56.52 ms | 5.99× |
| Dense discovery, 2 workers | 338.86 ms | 61.63 ms | 5.50× |
| Dense discovery, 4 workers | 344.59 ms | 88.44 ms | 3.90× |
| Sparse discovery, 16 MiB, 1 worker | 168.85 ms | 34.19 ms | 4.94× |
| Sparse discovery, 2 workers | 205.43 ms | 27.44 ms | 7.49× |
| Sparse discovery, 4 workers | 132.78 ms | 25.14 ms | 5.28× |
| Support pruning, 20 × 120 choices | 28.36 ms | 5.66 ms | 5.01× |
| Whole-payload matching through publication, 100,000 samples | 103.35 ms | 23.95 ms | 4.32× |
| Scattered full recovery plus completed resume | 57.38 ms | 59.34 ms | 0.97× |
| Reconstruct / hash / sync 64 MiB | 94.43 ms | 107.25 ms | 0.88× |

### Investigation of end-to-end variation

The last two results were investigated before acceptance. A bounded balanced
experiment alternated baseline → native → native → baseline in three blocks,
with three timing trials in each child: 18 timings per implementation per path.
It preserved the original measurements rather than replacing unfavorable rows.

| Balanced whole operation | Baseline median | Native median | Result |
| --- | ---: | ---: | --- |
| 64 MiB reconstruction | 119.45 ms | 119.24 ms | 0.18% difference; effectively unchanged |
| Scattered full recovery plus resume | 81.16 ms | 65.70 ms | 19.0% lower elapsed time; faster in all three blocks |
| Whole-payload matching through publication | 133.54 ms | 31.95 ms | 4.18× faster |

Reconstruction, range validation, mapping-range traversal and the entire
ReaderPool have identical ASTs between the measured revisions. Both reconstruction
profiles perform 16 reads, 19 writes and 51 hash updates. The initial native run
spent about 22 ms in buffered writes versus 10 ms in the baseline. Balanced runs
did not reproduce that slowdown; this is evidence of host/I/O variation in the
initial comparison, not a demonstrated optimization of byte copying.

The baseline 64 MiB profile spent approximately 57% in native hashing, 34% in
buffered writes and 7% in native reads, leaving less than 2% in Python work.
Moving that orchestration into Rust was not justified by these measurements.
Scattered recovery was dominated by SQL and identity gates; global assignment
was absent from its top 20 costs. Python therefore retains the lazy global
assignment search and checked reconstruction I/O. No result establishes a
universal complexity or speed bound for dense ambiguous assignments.

### FFI, worker counts and memory

An explicit 100,000-sample FFI probe took 14.11 ms with individual native calls
versus 1.65 ms using the batch API, including sample-size transfer: 8.55× lower
crossing/dispatch cost. Complete validation uses batches rather than one call
per ordinary sample. Sparse screens retain narrow reads. Native discovery
returns bounded batches and asks Python for any header outside its authorized
buffer; it cannot open evidence files itself.

More workers are not uniformly faster. The small dense fixture runs best with
one native worker; its coordinator/SQLite overhead exceeds parallel gains.
Sparse discovery benefits from 2–4 workers in these trials. The default remains
one; results depend on storage and candidate density. Warm durable queries avoid
source reads and retain their source/scope-qualified coverage checks.

Peak process RSS includes fixture setup, all trials and the separate profile /
allocation run. It is not isolated kernel memory. Representative baseline →
native values were 33.16 → 36.08 MB for tiny-sample validation, 49.53 → 47.42 MB
for one-worker dense discovery, 41.35 → 38.31 MB for support pruning and
176.70 → 176.85 MB for reconstruction. The native module and transferred sample
metadata have a cost; there is no across-the-board process-memory reduction.
Support pruning's traced Python allocation peak falls sharply because its graph
now lives in Rust; traced Python allocations exclude Rust and SQLite allocations.

### Reproduction and evidence limits

```bash
PYTHONPATH=/path/to/archived-baseline/src .venv/bin/python tools/benchmark_rust_core.py \
  --trials 7 --output /tmp/videorebuild-rust-baseline.json
PYTHONPATH=src .venv/bin/python tools/benchmark_rust_core.py \
  --trials 7 --output /tmp/videorebuild-rust-after.json \
  --profile-dir /tmp/videorebuild-rust-profiles \
  --compare /tmp/videorebuild-rust-baseline.json
PYTHONPATH=src .venv/bin/python tools/benchmark_rust_core.py \
  --scenario ffi-crossings --trials 7 --output /tmp/videorebuild-rust-ffi.json
```

The harness fingerprints Python and native module bytes, records all elapsed/CPU
samples, mean intervals, peak RSS, Python allocations, I/O block counts, parser
and search counters, output counts and encoded hashes. `.DS_Store` is excluded
from its inventory. Local raw evidence is retained under ignored
`test-results/rust-migration/`: `baseline-final.json`, `after.json`,
`balanced.json`, `ffi-crossings.json`, `baseline-provenance.json` and their profile
directories/logs. Earlier baseline JSON files are superseded by `baseline-final`.

These results demonstrate improvement on the migrated synthetic paths and the
tested whole-payload operation. They do not establish throughput, yield or
practical completion on a private/live image, nor a speedup for strict FFmpeg
decoding. Background workload and filesystem variation limit small wall-time
comparisons. Correctness and package evidence are recorded in
[TEST_REPORT.md](../TEST_REPORT.md).

## Earlier Python optimization — 2026-11-01

## Scope and acceptance

Audit all runtime modules and existing tools for avoidable CPU, I/O, and Python
allocation costs. Optimize demonstrated bottlenecks in the canonical recovery
paths. Preserve candidate enumeration and ordering, acquisition/allocation rules,
source identity checks, exact encoded bytes, ambiguity reporting, packet timing,
strict decoding, and interruption/resume behavior.

Baseline is commit `c3b41f63b9a33d2a80cfa2d6e1b4cc66a3b49199`; the measured
optimization snapshot is commit `ad5f0b6`. Work uses branch
`codex/performance-optimization` in the existing checkout. Inputs are
synthetic bytes and locally generated test media. Existing recovery cases and
private recordings are outside this work.

This is a medium-risk performance audit with implementation authority. The
installed delivery profiles have no exact audit-with-remediation route. The
minimum route used is baseline/audit → bounded design and parallel implementation
→ fresh measurements and tests → independent review and corrections → handoff.
Acceptance requires reproducible improvements, behavioral regressions, full
local tests, and no unresolved correctness findings. No release action is part
of this route.

## Implemented changes

| Area | Cost removed | Preserved requirement |
| --- | --- | --- |
| `engine._TranslationPlan` / `check_samples` | Repeated walks of all sample sizes for each six-sample sparse screen | Same selected samples and byte reads, fresh source checks, every-sample full validation |
| `bmff.Chunk.size` | Repeated sums of an immutable sample-size tuple | Same computed size and dataclass fields |
| `candidate_tiers._TierView._blocked` | Full extent-table traversal for narrow bounding candidates | Physical intersections, logical holes, tier exclusions, and actual-read acquisition checks |
| `storage.scan_logical` | Per-signature reads/stats for headers already inside the checked scan window | Bounded fallback reads across windows, source check before every block commit, unchanged atoms and digest |
| `reports.report` | Python JSON encoder chunk writes for every job/source | One serialized row at a time; same catalogue values, ordering, escaping, and atomic writes |
| `image_recovery.validate_prefix` | Three temporary rational products per packet | Exact integer cross-products, fractional mismatch rejection, negative PTS normalization, full independent decoding |
| `image_recovery._backfill_fragment_manifests` | Eager retention of the whole parsed job catalogue | Pages of 256 keys plus one parsed detail; completed SELECTs before updates; stable lexical pagination and resume |
| `image_recovery.recover_fragmented` | Copy of the retained section before writes/hashes | Same output/manifest bytes; prior view cleared before the next candidate read |

Metadata caches contain immutable offsets and sizes. They never stand in for
source bytes or a prior validation result. Full sample screening remains a
deliberate linear pass. Report memory still depends on the largest individual
job, source row, or serialized row; it does not retain the complete catalogue.

Independent review caught two implementation issues before completion: lazy
extent iteration reused a bound later changed to physical coordinates, and a
retained memoryview could pin the previous full read window. The corrections use
a distinct logical bound and clear the previous view before later candidate
reads. Multi-extent and multi-section checks cover the affected behavior.

## Audit coverage and deliberate costs

| Modules / tools | Disposition |
| --- | --- |
| `engine`, `bmff` | Optimizations above; reviewed parsing, sample framing, matching, grouped discovery, assembly/pruning, offset rewriting, and copying |
| `fragmented`, `image_recovery` | Reviewed initialization, fragment parsing/walking, discovery, restart prefilters, validation, publication, backfill, and resume; parser bounds and every-sample checks retained |
| `storage`, `candidate_tiers`, `reports` | Optimizations above; publication verification still checks accepted evidence, and tier refresh still checks live source/publication identity |
| `ranges` | Sorted extent and availability queries already use binary search; XML import streams bounded chunks/records; quotas and overlap checks retained |
| `workflow`, `migration` | Transactional import/rollback, streaming migration rows, SQLite backup, and full integrity hashes retained |
| `progress`, `cli`, `paths`, `__init__`, `__main__` | Throttled progress, one-time dispatch and tiny canonical helpers; no demonstrated bottleneck justified a change |
| `tui`, `tui_model`, `tui_browser` | Bounded log tail, database deadline, timed snapshots, and one directory sort per navigation retained |
| `validate` | Bounded subprocess output, independent probe, strict decode and timeout handling retained |
| Existing `tools/` scripts and launchers | Deterministic packaging, bounded diagnostics, selected-prefix checks and one-time preflight; no demonstrated bottleneck justified a change |

Search still scales with genuinely distinct hypotheses and validated alternatives.
Decode, source hashing and publication verification can dominate real workloads.
Their required accuracy/integrity work was not removed. No dependency, schema,
CLI recovery option, validation policy, version, or release contract changed.

## Reproduce measurements

The standard-library harness creates disposable cases outside the repository,
checks fixture integrity outside timed regions, and records runtime identity,
per-module source SHA-256, every trial and the median. It imports the revision
selected by `PYTHONPATH`; it does not override that path with the checkout source.

```bash
PYTHONPATH=src .venv/bin/python tools/benchmark_performance.py --trials 5 \
  --output /tmp/videorebuild-performance-after.json

# Point at an independently extracted baseline source tree to compare.
PYTHONPATH=/path/to/baseline/src .venv/bin/python tools/benchmark_performance.py \
  --trials 5 --output /tmp/videorebuild-performance-before.json

# Select individual workloads when investigating a regression.
PYTHONPATH=src .venv/bin/python tools/benchmark_performance.py \
  --scenario scan --scenario catalogue --trials 5
```

Run revisions serially on the same otherwise idle machine. Compare source hashes
and workload parameters before interpreting medians. There are no timing gates:
host load, filesystem cache and interpreter build affect wall time. Deterministic
regressions check work scaling and behavior instead of exact milliseconds.

Packet-validation measurements stub external subprocesses and isolate Python
hashing, JSON parsing and exact timing comparison. They cannot predict FFmpeg
decoding speed. Manifest memory uses a separate `tracemalloc` pass, excludes
fixture creation, and measures traced Python allocations rather than process RSS,
SQLite native allocations, decoder memory, or total disk usage.

## Measurements and verification

Executed on macOS 27.2 arm64, Python 3.13.18. Each revision ran five trials
serially, baseline first, using the same harness and workload parameters.
Source hashes in both JSON receipts were independently checked against the
baseline commit and optimization snapshot `ad5f0b6`. Medians were independently
recomputed.

| Synthetic workload | Baseline median | Optimized median | Ratio |
| --- | ---: | ---: | ---: |
| Plan construction + 100 sparse screens of 100,000 samples | 0.506943 s | 0.010560 s | 48.01× faster |
| 1,000 tail-byte tier checks over 100,000 noncontiguous extents | 4.816062 s | 0.007074 s | 680.84× faster |
| Index 30,000 small `mdat` boxes, including database commits | 0.551110 s | 0.090553 s | 6.09× faster |
| Generate JSON, CSV and HTML reports for 30,000 jobs | 0.849850 s | 0.438776 s | 1.94× faster |
| Compare 100,000 packets using stubbed probe/decode responses | 0.423894 s | 0.208689 s | 2.03× faster |
| Manifest startup over 2,000 jobs with 8 KiB detail padding | 0.024350 s | 0.028557 s | 4.2 ms slower |

The tier benchmark has no recovered publications and measures a narrow unclaimed
lookup; multi-extent claimed-byte correctness is established by separate
regressions. The catalogue benchmark times every report pass and independently
checks JSON jobs/details. Existing and new tests exercise the other report
formats. Many small report rows do not establish worst-case single-row memory.

Manifest startup's separately measured traced Python peak fell from
**17,845,694 to 76,944 bytes (99.57% lower)**. The extra per-job lookup trades about
4.2 ms in this fixture for bounded catalogue memory. A separate structural
two-section recovery probe reduced traced peak from 41,998,592 to 37,796,998
bytes while preserving both 4,194,795-byte outputs and the source hash. That
single memory probe is additional mechanism evidence, not a speed measurement.

Verification against optimization snapshot `ad5f0b6`:

- Baseline suite: 352 tests in 109.886 s; two optional fiwalk tests skipped.
- Optimization source suite: **370 tests in 76.526 s; two optional fiwalk tests skipped**.
  FFmpeg/ffprobe 9.0.2 performed the generated-media codec checks.
- Isolated fresh zipapp: **176 relevant regression/recovery tests passed in
  46.962 s**, no skips. Version, doctor and TUI-help smoke checks passed. All 19
  packaged runtime modules match tested source byte-for-byte. The checkout's
  existing standalone archive was preserved.
- Independent review of the six changed runtime modules, benchmark harness and
  this document found no remaining blockers after corrections. Independent
  reference comparisons covered 2,000 random sparse-screen size tables and
  10,000 candidate overlap ranges; final targeted review run passed 45 tests.
- Syntax compilation and `git diff --check` passed. Graphify's local code graph
  was refreshed without semantic/network extraction.

Suite durations are validation evidence, not recovery-throughput measurements.
The two optional skips are real fiwalk integration cases because the tool was
unavailable on PATH. Linux, Python 3.14, wheel/sdist installation, hosted CI,
full private-image recovery and real-camera acceptance were not executed here.
No end-to-end full-image speed or recovery-yield improvement is established.

Raw local logs, baseline snapshots, probes and JSON live under ignored
`test-results/performance-20261101/`: `baseline-benchmarks.json`,
`patched-benchmarks.json`, `benchmark-script.sha256`, `baseline-suite.log`,
`final-suite.log`, `zipapp-tests.log`, `package-smoke.json`, and per-area receipts.
The benchmark harness SHA-256 is
`6ca52b98a62c5ab6da8ebbe5e6b5a116d25355939ae230484580b32d32117a07`.

This report records the optimization validation before integration. Commit
`ad5f0b6` contains the measured snapshot; subsequent full-stack integration
checks and review repairs are recorded in [TEST_REPORT.md](../TEST_REPORT.md).
No worktree, dependency, package-version change, deployment, or release was
needed for the optimization.

## API references

The implementation follows the project's Python 3.13 floor:
[binary search with a key](https://docs.python.org/3.13/library/bisect.html),
[cached properties for immutable data](https://docs.python.org/3.13/library/functools.html#functools.cached_property),
[buffer views](https://docs.python.org/3.13/library/stdtypes.html#memoryview), and
[JSON serialization](https://docs.python.org/3.13/library/json.html).

## Adversarial throughput audit - 2026-11-09

Recovery performance changes must preserve eligible candidates, physical
alternatives, ownership and allocation rules, encoded bytes, requested validation,
publication evidence and interruption/resume behavior. A faster search with less
coverage is a different operation and cannot establish a speedup.

### Optimizations assessed on 9 November 2026

The comparison baseline was commit
`1bd3e60a09c5aa1e906fc9744dd2639228cd5d74`. Measurements used Python 3.13.18,
SQLite 3.54.1 and FFmpeg/FFprobe 9.0.2 on an arm64 Mac with 12 CPUs and 36 GiB RAM.
A separate recovery remained active on the host, so results include shared-host
noise. The actual implementations were compared with the baseline after warmup,
using five matched pairs and alternating execution order.

| Measured operation | Fixture | Median baseline | Median changed | Supported conclusion |
| --- | --- | --- | --- | --- |
| Tool completion and validation | Eight six-frame H.264 validations, four valid and four invalid | 0.977 s | 0.879 s | About 10% less validation time in this fixture |
| Ordered whole-payload origin query | 60,000 matches across 64 sources | 0.0622 s | 0.0295 s | About 2.1 times the query throughput, with identical ordered rows |
| Rejected-candidate pipeline | Three 10 MiB, 1,816-chunk attempts from one source | 0.8205 s | 0.5773 s | About 1.42 times the throughput, with identical attempt evidence |
| Rejected-candidate pipeline | The same shape across eight round-robin sources | 0.8300 s | 0.5943 s | About 1.40 times the throughput, with identical attempt evidence |

These operations are measured separately. Their ratios must not be multiplied
to predict a complete recovery's throughput or completion time. Initial prototype
results are superseded by measurements of the actual implementation.

#### Subprocess completion

Timed `Popen.wait` uses polling sleeps on supported POSIX runtimes. A blocking
waiter now reports completion through an event while the caller retains the
deadline and five-second progress heartbeat. Cancellation, launch errors, waiter
errors and timeouts still clean up the owned child and waiter before output
spools close. Strict FFmpeg arguments and validation receipts remain unchanged.

#### Whole-payload origin lookup

The origin index stores the header and payload-size integers needed to return
candidates in their canonical order. Its covering primary key removes the join
and temporary sort previously required before returning ordered candidates.
The measured ordered row count and SHA-256 agree exactly with the baseline.

The fixture's metadata database grew from 2,088,960 to 2,293,760 bytes, about
9.8%. This stores metadata integers, not another copy of source media. The query
benchmark measures warm lookup; it does not include initial source indexing,
cache rebuilding or media reconstruction.

On the first use of the revised layout, a verified old origin cache is closed
and renamed with a `.v1.bak` suffix, then rebuilt at its usual path with current
source coverage. The backup is never overwritten. A conflicting backup or unsafe
file path stops the transition for operator review. Other case indexes and
completed recovery records remain available. The extra one-time rebuild and
retained backup need disk space; this is not a canonical case-schema migration.
The backup retains the old layout for diagnosis or a separately planned rollback;
older runtimes cannot reuse revision-2 origin caches.

#### Path-name filtering

The `.DS_Store` path-name predicate is pure and has a 4,096-entry LRU cache.
Only that string/path classification is cached. Source dictionaries are mutable
and remain uncached; current source, descriptor, map, ctime and eligibility checks
still run. The metadata files themselves remain untouched.

The combined candidate-pipeline comparison uses the actual subprocess and path
filter implementations together, against both baseline implementations. All ten
matched pairs improved. The source-stat counts stayed exactly 14,550 per attempt
for one source and 14,557 for eight sources. Candidate hashes, reconstruction and
attempt evidence, strict tool arguments, sync barriers and source bytes agreed;
only volatile FFmpeg diagnostic allocator addresses were normalized. Cache usage
was one or eight entries in these fixtures, and no waiter threads remained.

#### Rejected optimization

A bounded experiment deferred the speculative candidate's `fsync` in four
10 MiB rejected-candidate pipelines. The median changed by only about 1%, with
one pair regressing; the four sync calls together took roughly 2 ms per batch.
That did not justify a durability change. The implementation retains existing
sync barriers. This result does not establish sync cost on other storage.

### Verification and reproducibility

Focused lifecycle, source/filter and ordered-query checks can be run from the
project environment:

```bash
PYTHONPATH=src:tests .venv/bin/python -B -m unittest -v \
  test_subprocess_wait test_origin_index_streaming test_path_filter_cache
PYTHONPATH=src .venv/bin/python -B -m unittest discover -s tests -v
```

The qualification workspace retains paired benchmark scripts, numeric results,
source hashes and the audit ledger under
`test-results/adversarial-performance-20261109/`. Synthetic timing evidence and
passing tests do not establish real-media yield, whole-case speedup or quality.
Updating source or preparing a zipapp does not update an already running recovery.
