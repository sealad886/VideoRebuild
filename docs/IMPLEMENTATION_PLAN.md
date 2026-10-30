# Shared byte-range reconstruction — implementation and release plan

Status: implementation in progress. Baseline: `9db774d` on `codex/fragment-accuracy-audit`; work branch: `codex/shared-byte-range`. The user authorized implementation and release, including merge into main only after Simplify Code and a performance-focused Adversarial Audit. No new worktree is authorized or required.

## Accepted scope and invariants

The product reconstructs supported MP4 video from recovered files and optional original image evidence. Files-only intake must work. Optional GNU ddrescue mapfiles qualify image-backed bytes; optional PhotoRec/fiwalk DFXML supplies explicit original byte mappings. Missing map evidence means unknown acquisition status. Unrescued or unmapped ranges cannot silently become media bytes. Filesystem/container evidence, acquisition status, and decode validation remain separate assessments.

Keep inputs read-only, source/output containment checks, regular-file checks, bounded parsing and subprocesses, exact encoded-sample preservation, exact timing checks, all declared tracks, ambiguity reporting, durable resume, and verification of published outputs. Do not infer recording identity from equal codec headers, disk adjacency, successful decoding, or counters alone. Never interpolate missing frames or silently bridge missing bytes.

The initial supported filesystem path imports real Sleuth Kit 4.14.0 fiwalk DFXML raw byte runs. This does not promise all filesystems or proprietary recorder support. Source filesystem and actual rescue yield remain unverified. Compressed/resident mappings are unsupported; sparse/missing extents stay unavailable. Missing-initialization inference, reference-assisted repair, blind cluster permutation, and automatic wall-clock chronology remain research follow-up.

## Keep, replace, and add

| Area | Decision | Reason |
|---|---|---|
| Python 3.13+, SQLite, standard-library runtime | Keep | Existing environment and bounded local processing fit this workload |
| `bmff.py`, ordinary sample-table matching, fragmented format rules | Keep and adapt consumers | Separate format knowledge remains useful |
| FFmpeg/ffprobe packet, timing and full-decode validation | Keep | Playback alone cannot prove encoded-byte preservation or correct timing |
| ReaderPool | Extend as canonical reader | Logical offsets resolve through ordered physical extents and rescue status |
| Separate fragmented DB, locks, resume/report lifecycle | Replace with canonical Case | One source catalogue and typed outcome ledger |
| Separate legacy/fragmented launch flows | Replace with canonical CLI flow | One input and recovery contract |
| Extent and acquisition model | Add `ranges.py` | Indexed lookup across noncontiguous image/file byte ranges |
| XML filesystem/PhotoRec import | Stream with explicit budgets | Real mappings without a Python filesystem implementation |
| Case schema changes | Explicit backed-up migration | Preserve old evidence; never silently upgrade validation guarantees |
| Later sections after damage | Separate validated segment outputs | Recover additional samples without joining uncertain intervals |

## Ordered work and ownership

- [x] WI-1: inspect source, tools, instructions and release state; freeze plan and range contracts (root orchestration with two independent design assessments).
- [x] WI-2: implement indexed ranges, mapfile parsing, bounded DFXML import and contract tests (range owner); canonical Case/schema/read/scan and migration (storage owner); fragmented strategy and later-segment validation (fragmented owner).
- [x] WI-3: integrate canonical CLI/workflow/report, launchers and documentation; exercise files-only, image/report, filesystem extents and mapped holes (root).
- [x] WI-4: run complete regressions and source/zipapp/sdist/wheel checks; perform Simplify Code then independent performance-focused Adversarial Audit; resolve in-scope blocking findings and reverify (root and independent reviewers). Reviews, final source/zipapp/wheel suites and local artifact verification passed; hosted CI recorded separately.
- [ ] WI-5: reconcile version and release artifacts, create reviewed PR, require actual CI results or report a blocker, merge into main, tag/publish immutable GitHub release, and verify downloaded release artifacts (root). **Active: remote main creation is blocked by automatic approval review; prepare focused draft PR against existing default branch without merging.**

Critical path: frozen ranges/Case APIs → integrated strategies → behavioral proof → simplification → adversarial performance review → final artifact checks → merge/release. Independent files have single owners. No private recovery image is used for stress or failure-injection checks.

## Acceptance and evidence

| ID | Required outcome | Verification |
|---|---|---|
| AC-1 | Reads cross noncontiguous extents with exact logical ordering | Deterministic range tests and generated media sample-byte comparison |
| AC-2 | Map status and coordinate transforms are enforced | +/?/*///- cases, holes, shifted offsets, stale map bindings |
| AC-3 | Files-only, PhotoRec and filesystem metadata inputs share one case | Canonical CLI integration tests |
| AC-4 | Later decodable media is separately recoverable | Generated damaged interval followed by valid independent start; no silent join |
| AC-5 | Shared codec headers do not collapse recording locations | Distractor recordings and duplicate initialization fixtures |
| AC-6 | Source bytes and provenance remain intact | Before/after source hashes, per-range hashes, encoded-packet/timing/full-decode checks |
| AC-7 | Interrupted work and case migration preserve evidence | Transaction/backup/failure fixtures, resume verification and changed-output rejection |
| AC-8 | Resource use is bounded and scales with relevant ranges | Large interval/map/XML inputs, indexed lookup measurements, bounded subprocess/segment behavior |
| AC-9 | Release is installable outside checkout | Build sdist, wheel-from-sdist, clean isolated install and zipapp CLI/media smoke |

Baseline executed locally: 147 tests passed with project Python 3.13.18 and FFmpeg/ffprobe 9.0.2. Hosted platform evidence must be recorded separately. No universal recovery percentage or filesystem-support claim follows from synthetic fixtures.

## Release and rollback

Target 0.3.0: pre-1.0 minor release with intentional CLI/case-schema changes; do not declare 1.0 stability. Versions in pyproject/runtime/zipapp and package metadata must agree. Existing version 0.2.0 has no verified published GitHub release/tag in this checkout; retain that distinction. Publish a GitHub release with zipapp, source distribution, wheel, checksums and migration/limitations notes; no PyPI publication or deployment is assumed.

Migration creates a verified local SQLite backup before replacing a case artifact. Old validation records retain their original evidence/version. Failed conversion leaves originals usable. Code rollback uses the prior commit/release with its original case or migration backup; never reuse a new-schema case with old code. Preserve original sources and historical outputs.

Open risks: source filesystem unknown; filesystem metadata may be incomplete; mapfile/image alignment must be explicit; real-data yield and multi-platform performance need evidence. Release blockers include false associations, unavailable-byte consumption, source mutation, unbounded normal-path growth, failed migration preservation, failing required checks, unresolved review defects, or artifact/version mismatch.
