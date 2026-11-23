# Executed validation — VideoRebuild

## Native matching integration — November 23, 2026

The Rust migration was reapplied to the initial AVC pruning tree
`98d59e77a4146ab0f33d24f5e54b134bf15c3f5f`, retaining current deep
association receipts, sample certificates, and strict validation. Builds and
tests ran in a disposable source export with isolated Python environments
and the locked Cargo dependencies.

Both CPython 3.13.18 and 3.14.10 ran 641 tests with 639 passes and two
optional `fiwalk` tests skipped. FFmpeg and ffprobe were version 9.0.2.
Rust 1.100.0 passed formatting, five Rust tests, and Clippy with warnings
denied. A wheel built from the sdist and its matching standalone archive
passed package verification and clean consumer checks on both CPython
versions on macOS arm64. These local checks cover the wheel's macOS 27.0
deployment tag. Older macOS, Linux, macOS Intel, and Rust 1.83 were outside
this local run.

Five synthetic trials per scenario compared the same source baseline before
and after the Rust migration. Assertions confirmed equivalent outputs and
posting sets where applicable.

| Scenario | Python median | Native median |
| --- | ---: | ---: |
| Validate 100,000 small samples | 111.036 ms | 1.845 ms |
| Sparse structural discovery with one worker | 127.721 ms | 29.955 ms |
| Whole-payload matching through publication | 111.163 ms | 24.186 ms |
| Scattered recovery plus completed resume | 56.362 ms | 52.727 ms |

These measurements use synthetic fixtures and warm OS caches. The scattered
case differs by only a few milliseconds. Performance on live recovery
volumes was outside the test scope.

## Missing generated-file restoration — November 4, 2026

Pending source version **0.6.0**, local branch
`codex/recreate-missing-outputs`, working changes based on `87c7014`.
The complete local suite ran **529 tests in 59.185 seconds**: **527 passed**,
with two optional real-fiwalk tests skipped because Sleuth Kit fiwalk was
unavailable. Python 3.13.18 and FFmpeg/ffprobe 9.0.2 were used.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests \
  .venv/bin/python -m unittest discover -s tests -v
```

FFmpeg/ffprobe were available on PATH. Regression tests cover missing ordinary
candidate/publication pairs, fragmented MP4 plus manifest and validation document
loss, restoration before cross-format tier loading, unchanged job/attempt/resume
state, retained decode-validation scope, source identity/range/hash failures,
changed surviving artifacts, unsafe paths, interrupted staging/copying, and
case-log error retention. Repairs check referenced sources rather than walking
the whole source catalogue per missing output. Historical device-number changes
retain source ownership while inode mismatches still fail.

An independent read-only review found no blocking issues and passed **54 focused
tests**. A zipapp built in a disposable source copy contained the restoration
module, passed version/recover-help checks, and passed **21 restoration/logging
tests using the packaged modules**. The checkout's existing standalone archive
was preserved. `git diff --check` passed, and the local code graph was refreshed.

No live case recovery, hosted CI, wheel/sdist build, real remount, or power-loss
qualification was performed. Read-only case metadata confirmed retained proof
for the reported missing fragmented output. Ordinary reconstruction still needs
mapping-bearing sidecar proof when both media copies are absent. On filesystems
without hard links, forced termination during exclusive copy fallback can leave
a partial generated destination; it remains an integrity error on resume.
No commit, push, release, installation or external publication was performed.

## Rust matching core, local 0.6.0 preparation — November 2, 2026

Baseline released v0.5.0 at `e257562a0194cec863bb89eb74d76a2c0847f844` has
the same tree as local `43671fdf4ecb20419fa0e47df9d494ffbe615f81`. A safely
archived baseline passed **456 tests in 62.222 s, zero skips**. Implementation
uses local branch `codex/rust-core` in the existing checkout; no worktree was
created. The frozen recovery archive and interpreter executable were unchanged.
The final ownership audit found that the recovery run card names the project
`.venv/bin/python`: build dependencies and editable package metadata had already
been installed into that shared environment before this was checked. This
violated the requested environment-isolation boundary. No package-removal or
environment-restoration attempt was made during the active recovery. This caveat
is separate from the passing code, synthetic recovery and artifact checks below.

The final integrated source suite passed **470 tests in 89.184 s on CPython
3.13.18** and **470 in 89.611 s on CPython 3.14.10**, with no failures or skips.
These two suites ran concurrently in separate generated fixtures, so durations
are validation evidence rather than comparative throughput. FFmpeg/ffprobe
checks used only generated synthetic media. Rust 1.99.1 tests passed five unit
cases and doc-test execution; `cargo fmt --check`, `cargo clippy --all-targets
-- -D warnings` and whitespace checks passed. Rust's declared 1.83 floor was
not executed locally.

Rust now owns complete NAL framing/SEI checks, ordinary full-sample window batches,
resumable structural discovery, shared point-origin chain rules and bounded
physical support propagation. Python retains checked source reads, allocation /
acquisition and identity gates, tier policy, durable indexes/receipts, SQLite
coordination, lazy global assignment, encoded-byte reconstruction, provenance
and publication. Superseded algorithms live only in a test oracle frozen from
the exact baseline; no Python production fallback competes with native code.

Correctness evidence includes:

- 12,000 deterministic encoded-sample mutations comparing ordinary classifications
  and fragmented NAL results/errors against the released Python oracle.
- 600 generated scanner comparisons covering unaligned/overlapping starts,
  multi-NAL chains, availability holes, counters and explicit limits.
- 1,200 generated physical-domain cases comparing baseline output/counters and
  every exhaustive feasible assignment; zero-width ranges, aliases, alternative
  witnesses and budget fallback remain represented.
- Independent review added 56 scanner comparisons, 1,296 exhaustive physical
  cases, 130-choice bitset/budget-boundary probes and 20 mutable/immutable
  fragmented-buffer comparisons. All nine frozen oracle function bodies were
  independently checked against baseline ASTs.
- Existing full-suite regressions protect hard-link publication, candidate-path
  fallback without a second payload, two distinct movies with `limit=1` followed
  by resume, immutable completed manifests, scope-qualified coverage, `.DS_Store`
  exclusions, worker cancellation, source aliases and ordered output evidence.

The first integrated run exposed a mutable-buffer boundary defect: fragmented
callers supplying bytearray input reached a bytes-only FFI and raised TypeError.
The correction checks bounds and snapshots only the selected sample before
releasing the GIL. All 65 fragmented tests and the new buffer regression passed,
then both complete suites above passed. Obsolete pure-Python launcher/progress
archive fixtures were updated to use the actual native bootstrap while retaining
their behavior assertions. No failing test was waived.

Seven-trial synthetic comparisons preserve exact discovery posting sets,
invariant counters, candidate counts and encoded-media hashes where applicable.
Complete validation improved 42.72× for tiny samples and 8.26× for larger samples;
dense one-worker discovery improved 5.99×, support pruning 5.01× and whole-payload
matching through publication 4.32×. Initial reconstruction and scattered-operation
medians were slower; three balanced ABBA blocks did not reproduce a regression:
64 MiB reconstruction was effectively unchanged, scattered recovery plus resume
improved 19.0%, and whole-payload matching improved 4.18×. The original slower rows
remain in [PERFORMANCE.md](docs/PERFORMANCE.md), with memory and host/I/O caveats.
No private/live-corpus throughput or recovery-yield claim is made.

Package preparation uses pinned setuptools / setuptools-rust build dependencies,
locked Rust dependencies, stable CPython ABI wheels and matching platform-specific
standalone archives. Native and Python bytes, source Rust inputs and bundled
license notices are checked. Tests reject missing/mutated native bytes, mismatched
tags/platforms and missing notices. Standalone normal return and synthetic SIGTERM
cleanup pass. Local wheel-from-sdist / standalone consumers passed on CPython
3.13 and 3.14 outside the checkout, with scoped pipx (3.13) and uv tool (3.14)
installation/doctor checks. Final artifact refresh receipts live with the logs
below; package success must be tied to those artifact hashes.

The local Homebrew interpreter conservatively tags the wheel `macosx_27_0_arm64`
even with deployment target 11.0; Mach-O inspection records minimum OS 11.0.
The tag was preserved rather than relabelled. That local consumer result does
not qualify older macOS, Linux or Intel. Configured CI builds and consumes
manylinux_2_28 x86_64, macOS arm64 and macOS x86_64 wheels/archives, plus the
Linux/macOS CPython 3.13/3.14 integration matrix. Hosted jobs were not run here.
Actionlint syntax/expression checks pass; optional shellcheck/pyflakes analyzers
were disabled, and the installed linter's stale Ubuntu 26.04 label warning was
checked against current GitHub runner documentation.

Readiness: local implementation, synthetic equivalence/performance and macOS
arm64 interpreter qualification are complete. A published 0.6.0 release remains
gated on hosted supported-platform artifacts/consumers, required review/CI and
explicit publication authorization. No push, PR, merge, tag, publication,
deployment, active-case write, recovered-file decoding/playback or media move
occurred. The existing local `videorebuild.pyz` was preserved.

Local evidence: ignored `test-results/rust-migration/` contains archived baseline
source/provenance, `baseline-unittest.log`, `final-python313.log`,
`final-python314.log`, `baseline-final.json`, `after.json`, `balanced.json`,
`ffi-crossings.json`, profiles, package-build/consumer logs and
`PERFORMANCE_ADJUDICATION.md`. [RUST_CORE.md](docs/RUST_CORE.md) records the design;
[0.6.0 notes](docs/RELEASE_NOTES_0.6.0.md) and
[RELEASING.md](docs/RELEASING.md) describe installation and release gates.

## Indexed matching and incremental reconstruction — November 2, 2026

Pending source version **0.5.0**, matching revision **5**, local branch
`codex/indexed-matching`, assessed from published v0.4.0
(`ee46785efa8df7adab7fc19bc7e755e785f70ef1`). The final local Python 3.13.18
suite passed **449 tests in 125.298 seconds**, with no failures or skips.
Independent reviews covered ordinary/indexed matching, physical-support pruning,
fragmented incremental reads, worker ownership and tier interval updates.

Executed synthetic operation counts, with equivalent nominations, encoded
outputs or physical witnesses checked as appropriate:

| Workload | Previous work | New work |
| --- | ---: | ---: |
| 5,000-chunk cold/warm translated negative | Full chunk-row digest | 0 canonical digest calls |
| Required-anchor group sample checks | 200 | 2 |
| 21 templates × 40 payload origins, one shared anchor offset | 840 first-sample comparisons | 40 origin starts; 60 NAL visits |
| Four-source structural coverage | 96 bytes read cold | 0 bytes read warm |
| Small physical assignment fixture | 6 partial assignments checked | 4, same feasible witnesses |
| Fragmented fixture with unused trailing window | 131,194 bytes requested | 162 bytes, same prefix |

The interval microbenchmark used 20,000 intervals and 100 owner queries, visiting
1,403 tree nodes; adding one new owner required one interval update. Publication
integrity checks and database work are excluded from that microbenchmark. Worker
tests preserve ordered postings at counts 1, 2 and 4. A deliberate I/O-wait model
benefited from parallel workers; the CPU-only scanner model did not materially
scale. Default worker count remains one. These are synthetic measurements, not
an end-to-end speedup or completeness result on private/live recordings.

Regressions cover short AVC boundary discovery, multi-NAL samples, source/map
identity changes, zero-hit and interrupted coverage, source/length-set receipt
normalization, cold origin joins against an all-pairs oracle, allocation holes,
aliases, alternate physical witnesses, capped fallback, overlapping accepted
owners, context-close failures, and worker-count-only resume without invalidating
completed jobs. Sparse propagation has comparison/edge budgets and falls back to
unpruned domains; the global overlap-aware assembler remains authoritative.

Wheel, source distribution and zipapp builds pass package-source identity and
version checks. The wheel installs into an isolated consumer environment outside
the source tree, with matching installed/runtime version and CLI/TUI help.
Reconstruction-only evidence remains `decode_checked=false`. The existing
recovery, its frozen runtime, SpeechForge and live media were untouched. Hosted
Linux/Python 3.14 CI and a new live-corpus benchmark were not run for this change.

Detailed local commands, counts, full test log and package checks are retained
under the ignored `test-results/matching-efficiency-analysis-20261102/implementation/`.
This validation does not authorize a release or active-run transition.

## Full-stack integration repair validation — November 1, 2026

The integration branch includes the eight earlier local prerequisite commits,
optimization snapshot `ad5f0b6`, and the review repairs described here. Fresh
net-change review against main `bf5d230` reproduced and repaired three recovery
correctness defects plus one terminal preference defect:

- Incomplete first-tier ordinary searches retain `search_limited` and cannot
  publish a second-tier fallback as unique. Search scope changes only when
  fallback starts; internal matching revision 4 reopens earlier decision caches
  without rebuilding source/atom indexes.
- Fragmented revalidation preserves referenced manifests and commits pointers
  to distinct new sidecars, keeping prior evidence verifiable after interruption.
- Explicit retries revisit failed or limited requested-mode validation despite
  preserved outputs; accepted same-mode ranges remain complete.
- Both accepted TUI positions for `--no-progress` propagate to foreground recovery.

The complete repaired source suite passed **378 tests in 111.212 seconds**,
with two optional real-fiwalk integration tests skipped because fiwalk was
unavailable. Python 3.13.18 and FFmpeg/ffprobe 9.0.2 ran on macOS 27.2 arm64.
Fresh independent verification passed **60 focused tests** and compared
**1,000 fixtures / 60,665 exhaustive physical assignments** against a reference
oracle. Syntax compilation, whitespace checks and local Graphify refresh passed.

Before these repairs, a wheel built from the committed optimization snapshot's
sdist passed 370 tests with the same two optional skips; all 19 installed runtime
modules matched that snapshot. Its installation, version, doctor and TUI-help
checks passed. This earlier wheel result is not evidence for the repaired source.
Hosted CI and external PR review are separate integration gates. No private
recordings, existing recovery cases, deployment, or release were processed.

Existing historical publications remain preserved; the matching repair does not
bulk-migrate or revoke them. The existing owner-revalidation and whole-before-deep
search model still determines their scope. Performance measurements below remain
bound to snapshot `ad5f0b6`, rather than later integration revisions.

## Whole-codebase performance optimization — November 1, 2026

Baseline `c3b41f6`, local branch `codex/performance-optimization`; macOS 27.2
arm64, Python 3.13.18, FFmpeg/ffprobe 9.0.2. Final source completed **370 tests
in 76.526 s**, with two optional real-fiwalk integration tests skipped because
the executable was unavailable. An isolated fresh standalone archive passed
**176 relevant regression/recovery tests in 46.962 s**, with no skips; all 19
runtime module bytes match tested source. Version, doctor, TUI help, syntax
compilation and `git diff --check` passed. The checkout's existing archive was
preserved. Independent review found and verified corrections to lazy logical
extent bounds and retained-buffer lifetime; no reviewed blocker remains.

Same-harness, serial five-trial synthetic medians improved sparse screening
48.01×, narrow unclaimed extent checks 680.84×, dense atom indexing 6.09×,
catalogue generation 1.94×, and Python packet-evidence validation 2.03×.
Manifest startup's traced Python peak fell from 17,845,694 to 76,944 bytes,
with about 4.2 ms extra time in the 2,000-job fixture. These measurements do
not establish full-image recovery throughput or yield. See
[PERFORMANCE.md](docs/PERFORMANCE.md) for exact workloads, invariants, audit
coverage, reproducible commands and limitations. Raw source-bound receipts live
under ignored `test-results/performance-20261101/`.

No Linux/Python 3.14 execution, wheel/sdist installation, hosted CI, private
image processing, or real-camera acceptance occurred. This validation preceded signed local commit `ad5f0b6`; no publication,
deployment, or release was part of the optimization validation.

## Performance and exhaustive matching repairs — October 30, 2026

Source and rebuilt standalone archive each passed all **271 tests** on macOS
with Python 3.13.18: 128.199 seconds for source and 106.416 seconds for the
archive. The archive's 14 source modules were also compared byte-for-byte
with the tested source. These runs used generated fixtures; no recovered-file
codec quality checks or playback were performed.

New regressions cover physical-witness multiplicity and ambiguity, allocation
and rescue-map exclusions, historical negative-cache promotion, feasible-only
assembly budgets, physical conflict pruning, repeated identical content,
long singleton-group recordings, shared discovery across metadata/group
batches, source mutation (including zero-hit and completed-skip sources),
fragmented timing, publication integrity and resume limits.

Independent exhaustive comparisons preserved the complete feasible content
sets for 70 generated fixtures (35 allocation-restricted), and physical
assignment sets for 80 fixtures. The 512-singleton-group proof used 512
partial checks and 1,024 provenance calls; ten identical groups/copies needed
one content attempt and 55 partial checks. The known ambiguity counterexample
retained both byte-distinct outputs.

A bounded negative-candidate benchmark reduced cold source-identity checks
from 262,192 to 64 while reading the same 96 bytes. Profiled CPU time changed
from 9.989 to 0.222 seconds. Eight uncapped deep batches shared one source
pass. Completed fragmented-job resumes eliminated initialization reads and
parses, while the missing-timing fixture reduced requested bytes from
34,591,393 to 1,052,097 with unchanged zero-output behavior. These are
synthetic operation counts/timings, not physical-I/O measurements or
production throughput guarantees.

Private reproduction scripts, independent review and operational receipts
remain under ignored `test-results/performance-audit-20261030/`. The completed
source index is reused for reconstruction; no raw reindex is required.
Real-image completeness, historical association and decoded quality are not
established by these tests. The dated evidence below retains its original
scope.

## PR preparation — October 30, 2026

Source and rebuilt zipapp each passed all 123 tests on macOS 27.2 arm64 with
Python 3.13.18 and FFmpeg/ffprobe 9.0.2: 37.047 seconds for source and 36.715
seconds for the zipapp. This includes the portable read-only audit helper,
publication-corruption checks, independent runtime-version validation, and
terminal video/audio sample-duration regressions. An isolated source-distribution
build included this report and all four launchers while excluding injected local
evidence. Launcher syntax and fragmented-launcher help checks also passed.

Hosted baseline testing exposed an unsupported-runtime mismatch: FFmpeg 7.1.5
can supply codec-derived packet duration instead of the container's duration.
The current implementation therefore requires both FFmpeg and ffprobe 8+ and
rejects older or unidentifiable releases during preflight. macOS CI and launcher
guidance now select modern FFmpeg; the exact timing gate remains unchanged.
These local results do not establish hosted CI parity or a new real-image audit.
The historical measurements below retain their original scopes.

## Accuracy audit — October 16, 2026

The October 16 full-suite evidence covers 118 tests on macOS 27.2 arm64
with FFmpeg/ffprobe 9.0.2. Source execution passed on Python 3.11.17 and 3.13.16;
the rebuilt standalone archive passed on Python 3.13.16. Installed-wheel results
below belong to the earlier 102-test snapshot and are not current wheel parity.

| Target | Result | Time |
| --- | --- | --- |
| Source, Python 3.11.17 | 118 passed | 23.570 s |
| Source, Python 3.13.16 | 118 passed | 23.807 s |
| Standalone zipapp, Python 3.13.16 | 118 passed | 23.722 s |

Nine existing recovered prefixes were sampled deterministically across successful
source offsets. All nine matched both their original-image byte ranges and saved
SHA-256 evidence, passed independent packet timing/hash/count checks and full
decoding, and retained unchanged source/output signatures. The sample covers
695,417 encoded bytes, 220 fragments and 220 samples. It does not certify every
output, the entire image, recording completeness, historical identity or viewing
and listening acceptance.

See [AUDIT_ACCURACY.md](docs/AUDIT_ACCURACY.md) for findings, corrections,
verification methodology and bounded residual risks. Detailed local evidence
remains under `test-results/audit-accuracy/`; it is intentionally excluded from
version control and source distributions because operational receipts may contain
private source paths. The dated sections below preserve earlier evidence rather
than making claims about the current checkout or a live recovery process.

## Historical real-image reconstruction — October 4, 2026

The source was a regular raw image (63,277,046,784 bytes), with matching PhotoRec
source geometry in `recup_dir.1/report.xml`. Recovered headers and output case
were stored separately from the image. The earlier legacy scan was preserved.

Real input exposed an importer defect: PhotoRec writes first-folder filenames
without `recup_dir.1`, unlike later records. The importer now maps a bare name
only to the report's containing recovery directory, retaining existing filename
validation and exact original-image byte checks. A generated-media regression
proves successful recovery and unchanged image bytes. Pre-fix rejection reproduced
in `test-results/completion/report-basename-before.log`; that snapshot's full
suite passed 103 tests in `report-basename-after.log`.

The real-image pilot completed all 50 requested locations: **40 decoded prefixes,
10 blocked, no decode failures**. The report indexed 325,125 locations. Pilot
blocking reasons concern incomplete first `mdat` boxes; accepted prefixes stop
at later boundaries and are not asserted to be complete original recordings.
Exact packet hashes and full decoding passed for all 40 accepted outputs.

The saved supervisor receipt in `test-results/reconstruction/run-status.json`
records a stopped run with exit code 130 after 182,568 of 325,125 locations:
174,722 decoded prefixes and 7,846 blocked locations. This is a historical
checkpoint, not a live process check or completion claim. The local receipt
retains command, process ownership and exit details; `full-run.log` retains
stdout/stderr, and the case's `fragmented.log` is canonical per-location evidence.
No full population success or viewing/listening acceptance is claimed.

The source and zipapp at this historical stage included the importer correction.
Wheel/source archives in `dist/` and `test-results/completion/artifacts.json`
describe the earlier local completion snapshot below; those receipts do not
establish artifact identity or verification for the current source.

## Historical local completion — October 4, 2026

Scope: finish the existing CLI and distribution against documented recovery
contracts, preserve original inputs and existing work, and close executable
validation gaps. No new recovery model or full private-image run was undertaken.

One distribution defect was reproduced and corrected: the source manifest omitted
three documented Mac launchers, including the target executed by `run-macos.command`.
All four launchers are now included. CI builds its wheel from the source archive,
checks extracted launcher syntax, and starts the extracted fragmented launcher.
This is a local CI configuration change; hosted CI was not triggered.

Fresh execution on macOS 27.2 arm64 with FFmpeg/ffprobe 9.0.2:

| Target | Result | Time | Evidence under `test-results/completion/` |
| --- | --- | --- | --- |
| Source, Python 3.11.17 | 102 passed, no skips | 25.748 s | `python311-unittest.log` |
| Source, Python 3.13.16 | 102 passed, no skips | 26.536 s | `python313-unittest.log` |
| Installed wheel, Python 3.13.16 | 102 passed, no skips | 25.869 s | `wheel-unittest.log` |
| Standalone zipapp, Python 3.13.16 | 102 passed, no skips | 25.192 s | `zipapp-unittest.log` |

The wheel was built from the source archive using hash-verified setuptools 82.0.1
and installed without dependencies into a fresh virtual environment created
without pip. Isolated import confirmed execution from that environment's
site-packages, rather than the source tree. Both media backends, exact byte
preservation, malformed inputs, ambiguity, bounds, interruption/resume, and
publication-integrity regressions were exercised through the existing suite.

`sdist-before.log` reproduces missing launchers. `package-build.log` and
`package-install.log` retain intermediate build/install checks. A staging omission
of LICENSE in that temporary copy was corrected before final package generation.
Final package evidence is recorded separately in `package-final.log` and
`artifacts.json`: required source members and license, wheel source-byte parity,
dependency-free metadata, isolated installation, doctor/version, extracted
launcher startup, and deterministic zipapp regeneration. Runtime identity is in
`runtime.json`. Final archives are local development artifacts, not releases.

An initial shell used system Python 3.9 without FFmpeg on PATH; `unittest.log`
records that environmental failure. The supported-runtime results above supersede
it. An interactive login shell also encountered startup errors and automatic
approval rejection of an unrelated completion-cache deletion. No shell files
were changed. Graphify query and AST refresh subsequently succeeded through a
noninteractive login shell; their logs are retained.

Independent read-only review of CLI/recovery preflight and resume, launchers,
packaging, documentation, and CI found the source-manifest defect and no further
concrete completion blocker. The prior four core fixes remain documented in
[AUDIT_CORE.md](docs/AUDIT_CORE.md). Current Linux execution, FFmpeg 7 execution,
full original-image recovery, recording identity, and real-camera viewing/listening
acceptance remain unverified. The historical Linux results below describe the
earlier 91-test snapshot and do not establish current Linux CI parity.

At that historical stage, no commit, branch, worktree, push, PR, publication or
deployment was created; the checkout then had no Git metadata. Original changed
files were copied to a unique temporary snapshot. The local `paths.json` receipt
records that snapshot and the isolated package-build workspace.

## Historical validation — October 2, 2026

Executed on Linux with Python 3.13.5 and FFmpeg 7.1.5-0+deb13u1. At that time, no macOS/Apple Silicon execution or full original-image recovery had been performed.

## Automated suite

**91 tests passed; 0 failures; 0 skipped.** This includes 50 retained ordinary-MP4/extent tests and 41 new fragmented-backend tests. The full suite completed in 12.788 seconds in this environment; this is an executed measurement, not a prediction for a user's recovery.

New integration fixtures cover HEVC and AVC plus 8 kHz AAC, actual-style 76-/80-byte per-sample fragments, missing and explicit tfdt, multi-track fragments, initialization reuse at distinct source offsets, independent encoded-packet SHA-256 comparisons, full decoding, pilot/resume, interrupted publish-before-commit recovery, and read-only inputs.

Negative cases cover missing starting references, sequence gaps, tfdt discontinuities, truncated boxes/payloads, unsupported addressing/flags, unknown tracks, malformed NAL lengths, absent audio, external references, invalid XML declarations, wrong images, modified inputs, device nodes, unsafe output paths, timeout, and stderr errors with exit code zero. Truncated-header checks exercise multiple byte-level truncation positions within one test.

## Real diagnostic samples

The structural parser verified 77 real samples (288,339 media bytes) against the supplied manifest hashes. It accepted all 37 identical 1,174-byte initialization headers. All other 40 samples begin with mdat. In the largest carve it validated nine embedded moof/mdat pairs and rejected the truncated tenth moof. Its observed fragment fields match the new parser's tested layout.

A separate temporary construction containing the initialization and nine complete pairs decoded four AAC frames without error. HEVC emitted `Could not find ref with POC 0` despite returning status 0. The construction is not released as a recovered recording. No private media binary is included in this project.

These real-data checks verify diagnosis and parsing, NOT successful recovery from the full source image.

## Large XML import

The report importer indexed **325,125 synthetic file records**, matching the reported MP4 entry count, from a 61,364,040-byte generated XML file in **5.765 seconds** in this environment. The resulting SQLite anchor count matched exactly. This is an XML indexing test only, not an image-recovery benchmark.

## Packaged execution

The `.pyz` was executed from a clean virtual environment created without pip or third-party Python packages. It recovered two separate synthetic source-image recordings with byte-identical initialization headers and no tfdt. Both published outputs matched their original synthetic byte streams exactly. This checks that configuration deduplication does not collapse recording locations.

The executable uses only standard-library Python modules; FFmpeg/ffprobe remain external runtime requirements. Native Mac launcher syntax was checked with `bash -n`. A successful Linux execution of shell/Python code is not represented as an Apple Silicon runtime test.

## Reproduce

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 tools/build_zipapp.py
python3 videorebuild.pyz doctor
```

Supporting files: `test-results/unittest.log`, `fragmented-unittest.log`, `runtime.json`, `xml-scale.json`, `package-smoke.json`, and `real-sample-checks.json`.

## Limits

The corrected backend recovers uninterrupted fMP4 prefixes at reported initialization locations. It is not a general filesystem-fragment reassembler and does not claim every prefix is an entire original recording. It cannot prove recording identity, chronology, or recovery of overwritten bytes. Read `docs/FRAGMENTED.md` before a bulk run.


## Shared byte-range release candidate 0.3.0 — 2026-10-30

Baseline `9db774d`, work branch `codex/shared-byte-range`. Python 3.13.18, macOS/Apple Silicon, FFmpeg/ffprobe 9.0.2; optional fiwalk 4.14.0. The pre-change source suite passed 147 tests. Final reviewed source passed **189 tests in 42.220 s**; standalone-zipapp imports passed **189 in 61.045 s**; isolated installed wheel, built from the sdist and tested outside the checkout, passed **189 in 59.893 s**. No skips occurred locally, including real fiwalk integration. Build requirements were installed from the existing hash-pinned manifest into the project .venv (setuptools 84.1.0).

Commands/evidence: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` (`/tmp/videorebuild-release-tests.log`), zipapp imports (`/tmp/videorebuild-zipapp-tests.log`), wheel-from-sdist isolated environment and extracted-source launcher verification (`/tmp/videorebuild-distribution-verification.log`). Source AST syntax, shell launcher syntax, package metadata/runtime version agreement and `git diff --check` also passed.

A generated H.264/AAC MP4 was stored across noncontiguous FAT16 clusters inside an MBR partition starting at 1 MiB. Actual fiwalk output plus a matching mapfile drove canonical CLI reconstruction. Encoded video/audio payload, strict decoding and unchanged input hashes passed. Other fixtures cover physically noncontiguous fragmented MP4, files-only recovery, rescued prefix and later IDR/tfdt section around a rescue hole, missing restart timing, duplicate header locations, malformed XML rollback, map transforms/unknown ranges, migration backup/WAL/rollback, source/output changes and bounded interruption/resume.

Requested Simplify Code and independent performance-centric Adversarial Audit passes completed before any main merge. All confirmed in-scope findings were fixed and independently rechecked; see [SHARED_RANGE_REVIEW.md](docs/SHARED_RANGE_REVIEW.md). Selected-stream setup is linear in relevant mappings; migration streams rows; source checks cache physical IDs; narrow availability uses binary search; malformed fragment candidates consume attempt and aggregate-read budgets. Synthetic performance results do not establish real-image throughput or recovery yield.

Hosted CI passed all four Linux/macOS Python 3.13/3.14 jobs for [PR #5](https://github.com/sealad886/VideoRebuild/pull/5) ([run](https://github.com/sealad886/VideoRebuild/actions/runs/41041942049)), and again after merge ([run](https://github.com/sealad886/VideoRebuild/actions/runs/41042309272)). The user explicitly authorized creating absent main and setting it as default; main was created from the previous published default head without losing history. PR #5 merged at `7adb2a1602d21772fe76a29eb36ad72c4e750825`. Signed tag `v0.3.0` and the [public GitHub release](https://github.com/sealad886/VideoRebuild/releases/tag/v0.3.0) were published. All downloaded asset SHA-256 hashes match local verified artifacts; downloaded zipapp version/doctor passed. Both fully merged task/audit branches were deleted locally/remotely after ancestry and unique-commit checks; main remains clean and tracks origin/main.

No private image, recording or existing recovery case was processed. Original filesystem support/yield remains unverified. Optional fiwalk tests skip on hosts without the tool; configured Linux/macOS Python 3.13/3.14 CI coverage must be distinguished from executed results. Forced termination during final migration installation may require restoring the retained complete backup; ordinary exception rollback is tested. No PyPI publication is implied.


Published release SHA-256:

```text
3676511c2961b8c6bd5e7191d31bb226ee2545b4747df9049c6c6e20eafa86d7  videorebuild.pyz
751d93402b754267eccb58c598e3ed1a0d84ba8003a50c66b9fda8ff8cb6e207  videorebuild-0.3.0.tar.gz
047b0ac29ef01ff33d3bccb20283246a981519df8b8a015f9d5170e46b230ee7  videorebuild-0.3.0-py3-none-any.whl
```

## Search policy and allocated-reference assessment — 2026-10-30

Local branch `codex/search-matching`, based on `60f1167`; Python 3.13.18 and FFmpeg/ffprobe 9.0.2 on macOS. The integrated source suite passed **233 tests in 90.699 s**, with no skips. A rebuilt standalone zipapp passed doctor/help smoke checks and all **23 new regression tests in 4.760 s**. This includes new allocation-boundary, uncapped-enumeration, explicit-limit, fragmented retry/policy-transition, duplicate-media summary and index-preserving CLI-resume regressions. Independent review also reproduced and repaired buffered physical-read prefetch beyond an allocation boundary. Search hypotheses spill to temporary SQLite; summaries still grow with genuinely distinct validated alternatives. Removing default cutoffs does not establish practical completion time or historical uniqueness.

An optional, independently supplied unallocated-range policy constrains `recover` against an existing index. Synthetic integration verifies unchanged source/atom rows, preserved encoded media and no intake call. The flag is intentionally unavailable on `run`, whose intake can scan unrestricted image bytes before reconstruction. Parser, per-section buffer, output-size, disk-space and subprocess guards remain. Historical outputs retain their original scope when an allocation policy changes.

Private allocated-reference assessment used installed Sleuth Kit 4.14.0 read-only filesystem metadata, independent `icat` extraction and canonical extent reads. The two extraction methods agreed exactly. Five typical allocated controls contain periodically stored standalone AVC SEI packets: their picture-bearing NALs pass strict independent decoding after a temporary diagnostic conversion, with unchanged VCL NAL hashes; their audio also passes strict decoding. These results do not qualify the original whole-file inputs under the current validator, which requires a picture in each video sample and one decoded frame per packet. No generic error suppression, SEI acceptance change or source repair was applied. This demonstrates a validation-model limitation rather than establishing filesystem corruption.

A separately labelled three-second packet-copy prefix from an allocated reference qualified under the existing strict contract. Both pinned baseline and current matching implementations recovered it from metadata plus shuffled fragments, without receiving the true extent map. Encoded media bytes, independent packet hashes/timing and strict decode agreed. Current matching took 0.988 s in this small fixture; this is not full-image throughput evidence or whole-original acceptance.

An image-bound unallocated policy was prepared from the actual exFAT allocation bitmap, with independent allocated/free boundary samples. Production reconstruction remains paused at the whole-file control gate, with its existing index and SQLite backup preserved; the new policy has not yet been applied to the production case. Local private receipts are retained under ignored `test-results/search-reevaluation/`. No private media or source-volume writes, archive imports, Git publication or release occurred in this assessment.

## Standalone-SEI reconstruction without codec checks — 2026-10-30

The user selected reconstruction without recovered-file quality checks. `--reconstruct-only` now bypasses FFmpeg/ffprobe discovery and validation while retaining structural sample/NAL framing, source identity, mapped-range overlap and allocation/acquisition constraints. Valid standalone AVC SEI metadata is preserved alongside all video/audio samples. Unique ordinary exports use `reconstructed`; fragmented exports use `reconstructed_prefix` / `reconstructed_segments`, with explicit `decode_checked: false`. Default strict validation remains unchanged and can still reject nonstandard packet/frame relationships.

The integrated source suite passed **243 tests in 63.436 s**. A subsequently added mode-cache regression also passed; all three revision/cache tests passed in **3.756 s**. The rebuilt standalone zipapp passed **11 reconstruction/mode/cache tests in 2.887 s** and CLI-help smoke checks. Tests prove codec-tool entry points are not invoked in reconstruction-only mode, mode/revision changes revisit earlier decisions without index changes, structural malformed input remains blocked, and reconstructed bytes are preserved. Independent source review found no remaining blockers. Existing default-mode tests use generated synthetic media; no new quality checks were run on recovered recordings after the user's steering.

Five privately retained whole allocated references were exported in reconstruction-only mode. All video samples, including 14–15 standalone SEI samples per file, and all audio samples matched by exact bytes, ordered hashes and counts. Full original hashes remained unchanged. Decoder, probe and subprocess entry points were patched to fail if called. These checks establish byte-preserving reconstruction under the supplied mapping, not playability or codec quality. A separate full-original blind-fragment experiment was interrupted during a large assembly search and remains inconclusive; no success or accuracy-failure claim is made for it.

Before production reprocessing, the running job stopped gracefully with exit 130; its completed reports and **145,627 prior attempt records** were preserved in a consistent SQLite backup whose `quick_check` passed. The **23,466 sources, 159,375 atoms and 29,592 metadata records** remained unchanged. A validation revision and explicit mode enter both cache eligibility and job coverage, so old rejections cannot silently bypass the new reconstruction rules. Private evidence and the deferred, unused decoder-validation work are preserved under ignored `test-results/sei-validation/`. No original-media or archive writes, uploads or Git publication occurred in this implementation.
