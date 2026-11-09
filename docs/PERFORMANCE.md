# Recovery performance audits

The first assessment below ran on 1 November 2026. Later assessments appear in
dated sections; each retains its own baseline and evidence limits.

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
