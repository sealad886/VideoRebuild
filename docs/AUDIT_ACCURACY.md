# Fragment reconstruction accuracy audit — October 16, 2026

## Outcome and scope

The fragmented backend preserves an exact contiguous range of original-image
bytes. Inspection and executed checks found no fragment-order or encoded-content
corruption in that path. A confirmed independent-timing validation gap and two
ordinary-MP4 search false negatives were corrected. This is bounded evidence,
not proof that every historical recording has been recovered completely.

Baseline revision: `5c748c6`, local branch `codex/fragment-accuracy-audit`.
Existing untracked `TEST_REPORT.md`, `SHA256SUMS.txt`, and earlier test evidence
were preserved. No recovery job was resumed; existing cases and source media
were opened read-only. No external system or release was changed.

## Context, acceptance, and route

The user requested an adversarial audit with particular emphasis on accurate
reconstruction of real video fragments and invoked the MLX Optimizer plugin.
This project has no MLX runtime or Python runtime dependency; no MLX changes or
installation were warranted. Environment: macOS 27.2 arm64, Python 3.13.16 and
3.11.17, FFmpeg/ffprobe 9.0.2. Existing `.venv` used for Python 3.13.

The delivery route uses the small-feature-change profile, small-or-medium
scale, medium risk, change authority supplied by the explicitly invoked
adversarial-audit remediation workflow. Context and acceptance precede bounded
implementation, testing, documentation and independent review. Solution design
was activated for exact timing normalization and resource budgeting. Separate
delivery planning, release, coordination and retrospective workflows were not
needed: no migration, deployment, external writes, or observed new operational
outcome. Routine source immutability/privacy checks remained within the audit;
no separate security assessment was claimed.

Acceptance criteria:

1. Preserve source bytes and original sample ordering. Never join unrelated
   orphan fragments, fill gaps, or claim historical recording identity.
2. Independently verify parser-derived per-track DTS, PTS and durations, ordered
   packet hashes, frame/packet counts and full decoding.
3. Prove signed/unsigned composition offsets, AVC/HEVC B frames, absent/explicit
   decode times and independent nonzero track start times.
4. Correct demonstrated search misses without unbounded traversal or claims of
   uniqueness after a limited search.
5. Keep private media local and distinguish sampled real evidence from generated
   fixtures, complete-case recovery and physical/user acceptance.

## System map and evidence index

- `fragmented.py`: bounded initialization and moof/mdat parsing, exact payload
  coverage, sequence continuity, per-track clocks, IDR eligibility and prefix
  stopping boundaries. No source change was needed here.
- `image_recovery.py::validate_prefix`: independent packet hashes/counts,
  timestamp comparison and strict decoding. Publication remains exact prefix
  copying; source/report/header identity and resume-output integrity remain.
- `engine.py::deep_search`: ordinary sample-table-guided extent discovery;
  independent from the image-first fragmented backend.
- `tests/test_fragmented.py`, `tests/fragmented_helpers.py`: generated AVC/HEVC
  fixtures and timing regression checks.
- `tests/test_performance.py`: search completeness and bounded-work regressions.
- Evidence folder: `test-results/audit-accuracy/`. No private media is stored
  there. `check_real_prefixes.py` reads existing publications and original-image
  ranges; it writes only hashes, offsets, counts and validation results.
  Its portable successor is [the recovered-prefix audit tool](../tools/check_recovered_prefixes.py).
- Graphify was queried before source investigation and refreshed after code
  changes; Codanna index identity was checked and consequential claims confirmed
  directly in source.

Primary references checked during the audit: [FFprobe options and packet data](https://ffmpeg.org/ffprobe.html),
[FFmpeg 9.0.2 MOV implementation](https://github.com/FFmpeg/FFmpeg/blob/n9.0.2/libavformat/mov.c),
and the [W3C ISO BMFF browser profile](https://www.w3.org/TR/mse-byte-stream-format-isobmff/).
The browser profile is not treated as a universal local-file requirement.

To repeat a bounded read-only sample audit with a new report outside the case:

```bash
python3 tools/check_recovered_prefixes.py CASE IMAGE OUTPUT.json
```

The tool selects up to nine successful prefixes and returns exit code 0 when
all sampled checks pass, 1 on a mismatch, or 2 for input/tool errors. It does not
resume recovery or modify the case. Generated-media tests cover unchanged source,
publication and database bytes, plus rejection of a modified publication. Reports
include local paths and must be inspected before sharing. The archived October
16 evidence below describes the original helper and remains unchanged.

## Measurement and real-source methodology

The baseline suite passed 112 tests in 67.022 seconds. Different run contention
makes later suite durations unsuitable as performance comparisons.

An existing case contained 174,722 successful-prefix rows and 7,846 blocked rows
at read time. Nine successful rows were selected deterministically at evenly
spaced positions in source-offset order, including first and last. These counts
are catalogue observations, not fresh validation of the entire case.

For every selected row the audit read the saved prefix length from the raw
image, compared its SHA-256 with both the published file and saved evidence,
parsed the prefix again, checked eligibility and exact parsed endpoint, then
ran current packet-hash/count/timing checks and full strict decoding. File
signatures were checked before/after reads. No previews, extracted frames or
media uploads were produced.

Both before and after the timing gate, all nine passed. Total inspected:
695,417 encoded bytes, 220 fragments, 220 samples. Each publication exactly
matched its contiguous source range. The stronger timing run is recorded in
`real-prefixes-with-timing.json`; final rerun evidence is recorded separately.

## Findings and remediation ledger

| ID | Severity | Confirmed deficiency | Change and proof | Status |
| --- | --- | --- | --- | --- |
| A1 | Medium | Ordered packet hashes and decoding accepted deliberately incorrect parsed duration, composition offset and track start | `validate_prefix` now compares exact rational DTS/PTS/duration; timing mutation regression rejects all three | fixed |
| A2 | Medium | Ordinary deep search stopped NAL accumulation at 64 despite structural validation accepting up to 65,536 | Same existing 65,536 ceiling now applies in discovery; 65-NAL fixture reaches candidate dispatch | fixed |
| A3 | Medium | Six-byte lookahead missed an eligible terminal AVC NAL requiring only five bytes | Codec-aware header bounds; source-end AVC framing regression reaches candidate dispatch | fixed |

### Root causes, design and patch plan

A1 concerned validation coverage, not demonstrated damage to recovered media.
The parser already retains composition offsets and source copying already
preserves timing bytes. The validator previously never requested FFprobe timing
fields, so a parser regression could pass byte/decoder checks while producing
wrong continuity or duration claims. The smallest fix extends that existing
probe. It compares integer packet ticks through rational stream time bases;
missing fields or mismatches reject publication. FFmpeg normalizes signed
negative composition offsets using a whole-track PTS shift equal to their
largest negative magnitude. That explicit rule is allowed; arbitrary observed
shifts or numerical tolerances would conceal synchronization errors and were
rejected as an alternative. The probe-output cap rises from 32 to 64 MiB to
accommodate timing fields; sample count remains bounded at 200,000. Timing
comparisons run one packet at a time rather than retaining rational-valued
timeline arrays.

A2 and A3 concerned discovery false negatives in ordinary-MP4 recovery, not
incorrect fragment concatenation. Their proving fixtures exercise real source
reads, grouping, structural checks, hashing and dispatch. Only decoder/finish
boundaries are mocked in those focused tests; tiny synthetic NAL payloads are
not asserted decodable. Existing generated-media integration tests exercise
actual decoding separately.

Increasing the NAL chain ceiling alone would amplify worst-case CPU cost.
Therefore traversal separately consumes a global NAL-check budget using the
existing `max_anchor_checks` value. Hitting either target-association or NAL
budget stops the scan and retains `search_limited` plus a specific stop reason.
A dense-chain regression proves exhaustion at three checks without candidate
dispatch. Dense images may now stop earlier explicitly; the operator can raise
the existing limit when warranted.

Patch order: reproduce baseline failures, correct independent timing and add
generated timing fixtures, correct discovery and bound its work, update canonical
contracts, verify source/zipapp and sampled real data, independent full-diff
review. Regressions or valid-fixture rejection trigger correction before handoff.
All changes remain local and reviewable; no data migration or rollback action
against an existing case is required.

## Verification

Evidence files retain exact commands/output:

- `baseline.log`: 112 baseline tests passed.
- `before-regressions.log`: baseline functions fail five assertions across the
  three targeted tests; no setup errors. Original functions were loaded from
  `git show HEAD` in an isolated Python process, leaving the checkout intact.
- `fragmented.log`: 55 fragmented tests passed, including new timing cases.
- `final-source-313.log`, `final-source-311.log`, `final-zipapp-313.log`: full
  suites, refreshed after final changes.
- `real-prefixes.json`, `real-prefixes-with-timing.json` and final sampled-run
  evidence: nine real publications passed exact-byte and full decoder checks.
- `graphify-update.log`: successful AST graph refresh.

Final results:

| Target | Command | Result |
| --- | --- | --- |
| Source Python 3.13 | `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` | 118 passed, 23.807 s |
| Source Python 3.11 | `PYTHONPATH=src python3.11 -m unittest discover -s tests -v` | 118 passed, 23.570 s |
| Zipapp Python 3.13 | `PYTHONPATH=videorebuild.pyz .venv/bin/python -m unittest discover -s tests -v` | 118 passed, 23.722 s |
| Real source sample | `PYTHONPATH=src .venv/bin/python test-results/audit-accuracy/check_real_prefixes.py CASE IMAGE OUTPUT` | 9/9 passed; exact image/publication/saved hashes, timing, full decode, unchanged signatures |
| Final archive | `.venv/bin/python tools/build_zipapp.py`, then archive `doctor` and `recover --help` | passed; clarified dual-budget help verified after final rebuild |
| Patch hygiene | `git diff --check` | passed |

The Python 3.11 command above is shown with a portable interpreter name; the
audit invoked the installed Homebrew Python 3.11 executable directly.
The three full suites ran against the final functional code. A subsequent
CLI-help-only edit was checked through the rebuilt archive; full suites were not
repeated for that wording change. Final archive/source digests are recorded in
`artifact-hashes.json`. The archive is a local build, not a published release.

Independent full-diff review found no actionable correctness or resource
regression in supported layouts and independently ran all six added test methods
successfully (2.429 s). Review covered both specification and code-quality lenses.
No CodeRabbit API upload was used; review was performed by independent local
agents. All three findings are fixed; remaining limitations are listed below.

## Bounded residual risks and exclusions

- Nine real prefixes are a deterministic sample, not statistical certification
  of all outputs, blocked candidates or the complete raw image. No complete
  historical recording identity, wall-clock chronology or user acceptance is
  proven.
- Arbitrary filesystem cluster reassembly, orphan joining, missing initialization
  recovery and unsupported layouts remain outside the existing backend contract.
- Timing behavior was tested against installed FFmpeg 9.0.2. Extreme unsigned
  composition offsets interpreted differently by FFmpeg, or unusual optional
  initialization timing tables, may fail the independent timing gate. They are
  rejected rather than silently accommodated. No decoder-version matrix or
  Linux run was performed.
- Temporary subprocess output still spools to disk; the readback cap does not
  impose a whole-case disk quota. Existing timeout, window and sample budgets
  remain. Full-image throughput and worst-case maximum-prefix RSS are unmeasured.
- Existing completed cases are not retroactively relabeled as having passed the
  new timing gate. Their saved evidence remains unchanged; the separate sampled
  audit supplies new evidence only for the selected outputs.
