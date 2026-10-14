# Core functionality and performance audit

## Outcome and scope

Four confirmed in-scope defects corrected locally; independent code/spec review
approved. Both recovery backends, validation, resume integrity, parsing budgets,
and sparse deep-search CPU behavior were inspected. All measurements use locally
generated or synthetic inputs. No private recordings or full recovery image were
processed, and no external system was modified.

Completion criteria: reproduce each defect, apply the smallest canonical-path fix,
preserve original bytes and complete-prefix semantics, prove regressions, measure
resource changes, and state remaining limits. The source checkout has no `.git`;
there is no branch/upstream/commit identity. Original source, tests, docs, zipapp,
and checksum manifest are backed up in `/tmp/videorebuild-audit-original`.

## System map and evidence index

- Runtime: Python 3.14.8, FFmpeg/ffprobe 9.0.2, macOS 27.2 arm64; exact tool paths
  recorded in `test-results/audit-core/zipapp-doctor.json`.
- `cli.py`: command dispatch and reports; `fragmented.py`: initialization,
  fragment parsing and prefix walks; `image_recovery.py`: PhotoRec anchors,
  source binding, byte-hash/decode validation and checkpointed prefix publication.
- `storage.py`: scanning, SQLite catalogue, source identity and bounded readers;
  `engine.py`: whole-payload/scattered matching, candidate validation/publication;
  `validate.py`: bounded subprocess output and decoding gates.
- Contracts: README, `docs/FRAGMENTED.md`, `docs/ARCHITECTURE.md`.
- Codanna 0.16.0 index was checked against this workspace; consequential claims
  confirmed in source. Graphify refreshed through a login shell; log retained.
- External tool reference: [official FFprobe documentation](https://ffmpeg.org/ffprobe.html),
  checked alongside installed versions. No third-party Python dependency added.
- All proof files below live in `test-results/audit-core/`.

## Measurement method and baseline

Baseline suite: `PYTHONPATH=src python3 -m unittest discover -s tests -v`;
91 tests passed in 21.182 seconds (`baseline-unittest.log`).

Sparse benchmark invokes real `deep_search`/`ReaderPool` on 1/4/16 MiB zero-filled
files, substituting a single synthetic extent and recording result metadata.
Its before mode substitutes the original regex; after mode uses current code.
This isolates candidate-generation CPU cost, not end-to-end recovery throughput.
Two runs confirm the mechanism; elapsed times are single trials, not a statistical
capacity claim. `performance-probe.py`, `performance-results.json`, and
`performance-independent.json` retain commands and measurements.

Memory probe uses an unmodified FFmpeg-generated AVC/AAC initialization with two
structurally valid 100,001-sample video fragments. Tiny synthetic IDR payloads are
not asserted decodable; allocation happens before decoder validation. `tracemalloc`
measures Python allocations during parsing, excluding input construction and
FFmpeg/process RSS. See `aggregate-sample-probe.py` and before/after/independent JSON.

## Findings, root causes, and remediation ledger

| ID | Severity | Confirmed failure | Patch | Final status |
| --- | --- | --- | --- | --- |
| F1 | High | Fragmented resume claimed `decoded_prefix` after publication corruption/removal | `image_recovery.run` completed-evidence and output size/hash gate | fixed |
| F2 | High | Ordinary-MP4 resume claimed `matched` with missing or corrupted independent publication | `engine.recover` cached-publication hash gate | fixed |
| F3 | High at large image scale | Zero-filled deep search entered Python at every byte, outside structural anchor counter | `engine.deep_search` rejects zero/incomplete lengths in zero-width regex | fixed |
| F4 | Medium | Prefix sample objects accumulated across fragments without aggregate retention bound | `walk_prefix`/`parse_fragment` remaining sample budget | fixed |

### F1 and F2: completed publication integrity

Reproduce: recover generated media, corrupt or remove the published MP4, then
resume. Before the patches, success remained cached. Candidate hashes alone
cannot protect independent-copy publications used on no-hardlink filesystems.

The smallest fix verifies publication bytes before reusing a completed result.
Fragmented recovery checks all successful publications and their evidence before
processing new jobs; a failed gate does not refresh historical success reports.
Missing, truncated, same-size changed, or unsafe publications fail closed with
`SafetyError`/CLI exit 2. Conflicting bytes and database history are preserved.
Existing intact resumes and publish-before-commit recovery still work.

Proof: `correctness-regression.log` (four red failures, six focused green tests,
full fragmented checks); `contracts-regression.log` (two red failures and three
green tests). Tests prove independent candidate bytes stay intact, pending jobs
are not processed after the fragmented failure, and intact resume avoids decoding.
Residual tradeoff: hashing reads accepted output bytes again; legacy recovery
also hashes retained candidates. Chunked reads bound memory but resume I/O grows
with retained data. No automatic repair or overwrite of conflicting artifacts.

### F3: sparse deep-search CPU cost

Root cause: plausible-length regex matched all-zero length fields; Python rejected
those only after one callback per byte. Even `max_anchor_checks=1` recorded zero
structural checks while scanning the full gap.

For 16 MiB, worker measurement changed from 7.88 seconds to 0.583 seconds;
independent rerun changed from 9.44 to 0.590 seconds (about 27 MiB/s after).
The regex now excludes zero lengths and requires four available bytes, while
remaining zero-width to preserve adjacent/overlapping starts. No search option,
source interpretation, decoder, or matching criterion changed.

Proof: two deterministic `test_performance` regressions cover zero gaps and
retained overlapping starts; full suite covers real scattered AVC/AAC, HEVC,
multiple NALs, ambiguity, and scan boundaries. Residual: scanning remains linear
in image size; other data patterns and storage speeds were not benchmarked.

### F4: aggregate retained sample metadata

Root cause: 200,000 samples were bounded per fragment, while all fragments stayed
resident in `Prefix`. A 1,201,444-byte input retained 200,002 sample objects and
reached 56,008,500 traced bytes before decoding.

New operational budget: at most 200,000 samples per retained prefix. Remaining
budget is passed into fragment parsing and checked before run allocation.
An oversized next fragment stops with `prefix_sample_budget_exceeded`; previously
accepted complete fragments, end offsets, and clocks stay unchanged. No partial
fragment is published. The same probe now retains 100,001 samples, peaks at
33,602,780 bytes, and stops before its second fragment.

Proof: `aggregate-sample-regression.log`, before/after/independent JSON; regressions
cover exact boundary, unchanged source bytes and longest complete timed prefix,
and whole multitrack-fragment rejection. Documentation describes the operational
limit. Residual: long recordings can stop earlier at this explicit budget; window,
NAL, parser and FFprobe-output ceilings remain independent resource limits.

## Validation and review

- Final source suite: 102 tests passed in 21.910 seconds (`final-unittest.log`).
  Standalone-archive focused regressions: 10 passed (`zipapp-regressions.log`).
- Rebuilt `videorebuild.pyz` through the existing deterministic builder; doctor
  and version checks passed. `zipapp-import.txt` confirms tests import the archive.
- Independent spec/code review approved all four fixes and inspected regression
  tests; three sample-budget tests independently passed.
- Intermediate proof is retained: a suite run while the zero-gap test was still
  being refined failed, and an initial archive selection used an incorrect test
  method name. Final commands use corrected fixtures/selections.
- SQLite connections in two test contexts now explicitly close; an existing
  Python 3.14 resource warning was traced to context-manager-only fixture usage.
- Backend documentation updated; existing historical test evidence left intact.
- Checksum manifest refreshed for changed/new audit-owned artifacts and verified.

## Remaining limits and recovery

No full-image benchmark, real recording listening/viewing acceptance, Linux run,
Python 3.11/3.13 CI execution, or wheel install was performed. These are proof gaps,
not passing claims. Current synthetic evidence does not establish arbitrary-camera
support, historical recording identity, or complete recording recovery.

No accepted finding remains unimplemented. Bounded operational tradeoffs above
are retained under the delegated audit scope. If a regression appears, preserve
case data, compare the original local snapshot, and prepare a scoped reversal;
do not overwrite recovery artifacts or silently reuse stale result claims.
No commit, push, PR, release, deployment, or worktree was created. Version remains
0.2.0; the rebuilt archive is a local development artifact, not a new release.

## Lifecycle routing

Medium risk because recovery integrity is affected; four small canonical patches.
The installed delivery profiles lack an exact audit-with-remediation route, so
orchestration used a minimal composite: scoped context/acceptance → audit and
quality evidence → minimal patch design/implementation → independent quality/review
→ documentation/handoff. External coordination and release were not activated.
