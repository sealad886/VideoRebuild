# Shared-range simplification and adversarial performance review

Review scope: the 0.3.0 shared-range implementation on `codex/shared-byte-range`, based on `9db774d`. User-required gates run before merging into main. Independent reviewers inspected code authored by other agents; the range author did not self-certify ranges, and the storage/fragment authors reviewed each other's modules. The root integrates findings and final evidence.

Environment: project Python 3.13.18, macOS/Apple Silicon, FFmpeg/ffprobe 9.0.2; optional Sleuth Kit fiwalk 4.14.0. Experiments used disposable synthetic data, at most 256 MiB disk per scenario and bounded subprocess time. No private image, recording, provider or live device was stress-tested.

## Finding ledger

| ID | Finding and consequence | Disposition / required proof |
|---|---|---|
| SIM-01 | Each mapped-file scan loaded/validated the entire logical catalogue, producing quadratic setup work | Fixed: selected source plus referenced physical IDs only; independent recheck and unrelated-malformed-stream regression |
| SIM-02 | Invalid fragmented limits were rejected after ordinary recovery could publish files | Fixed: one shared fragmented policy validator runs at CLI preflight and strategy entry; generated-media regression proves exit 2 before case/output creation |
| SIM-03 / PERF-MIG-01 | Migration retained all legacy results and duplicate converted detail strings | Fixed: staging database conversion streams results and attempts; late-invalid-row rollback and independent memory remeasurement |
| SIM-04 | Old test-only scan/scan_source/path block reader duplicated surviving intake/reader logic | Fixed: callers/tests migrated to canonical intake, scan_logical and ReaderPool.iter_blocks; obsolete implementations removed |
| PERF-READ-02 | Every source check rebuilt physical IDs across all extents; narrow availability queries scanned every extent | Fixed: cache referenced source IDs once; binary-search the first overlapping extent and stop at requested end; counted-visit regression and independent recheck |
| PERF-01 | Restart prefilter read large candidate payloads before charging attempts; malformed moofs bypassed the budget | Fixed and independently rechecked: every candidate start charged before payload reads; aggregate media-read budget twice the bounded window; bytes reused for assembly; limit preserves outputs and reports search_limited |

No finding is closed merely because reviewers agree. Source recheck, behavioral regressions and actual bounded measurements supply the scope below. Any later source change requires affected proof to be reconsidered.

## Measurements and meaning

Selected-stream scanning, identical eight-byte logical files:

- Before, 300/600 streams validated 90,000/360,000 mappings and took 0.828/3.596 seconds.
- After, 300/600 streams validated 300/600 mappings and took 0.0429/0.0788 seconds.
- Independent post-fix 600/1,800-stream run took 0.0780/0.2249 seconds.

Migration, 600/1,800 legacy jobs with 8-KiB detail each:

- Before Python traced peak: 10,668,198 / 30,712,430 bytes.
- After exact-fixture traced peak: 473,307 / 68,574 bytes. First-run interpreter/cache warmup explains the larger small-case value; this is bounded Python allocation evidence, not total OS RSS or SQLite cache measurement.
- All historical jobs remained present. Backup/WAL, late-invalid data, installation rollback and corrupted-publication regressions run separately.

Mapped reader, 2,000/20,000 extents:

- Before: 1,000 source checks took 0.0214/0.1577 seconds; 100 one-byte availability queries took 0.0121/0.1182 seconds.
- After implementation-owner measurement: checks 0.00398/0.00400 seconds; narrow queries 0.000564/0.000572 seconds. Independent final remeasurement: checks 0.00534/0.00617 seconds; narrow queries 0.000683/0.000676 seconds.
- The regression bounds irrelevant extent visits rather than asserting a fragile wall-clock threshold.

Restart prefilter adversarial reproduction used a 2.1-MiB image with 64/256 malformed indexed moofs and max_segments=1. Before repair it caused 134,304,232 / 538,196,200 read bytes (193/769 calls) without a budget reason. Independent final reproduction with max_segments=1 uses four reads and 2,101,000/2,108,680 bytes, reporting segment_attempt_budget. With max_segments=64 it uses nine reads and 4,200,736/4,216,096 bytes, reporting segment_read_byte_budget. Both results report search_limited. Dedicated regressions also preserve earlier validated outputs at a reached budget.

These are synthetic local cost measurements. They do not establish throughput on a 63-GB original image, filesystem universality, complete-recording recovery rates, or physical storage latency.

## Behavioral and release proof

The final reviewed source suite passed 189 tests in 42.220 seconds. Independent fragmented verification passed 65 tests, and existing performance regressions passed 11 tests. Artifact checks are recorded separately. TEST_REPORT.md records the final count, versions, package identities, hosted CI state and any release limitations.

End-to-end filesystem evidence uses a generated H.264/AAC MP4 scattered across FAT16 clusters inside an MBR partition starting at 1 MiB. Real fiwalk DFXML and a matching rescue map drive the actual canonical CLI. Exact encoded A/V payload, strict all-track decoding and unchanged original/image/report/map SHA-256 are checked. This qualifies that synthetic raw-run path, not the user's unknown original filesystem.

Additional contracts cover files-only and physically noncontiguous fragmented sources, separate later IDR/tfdt sections after a rescue hole, rejection of missing restart timing, duplicate header locations, unavailable-byte rejection, malformed XML rollback, changed inputs/outputs, and interrupted resume.

## Bounded residuals

No missing-initialization inference, proprietary filesystem parsing, wall-clock joining or generation of absent video bytes is included. Fiwalk integration tests skip when the optional tool is absent. Source checks are ordinary change detectors rather than a hostile-administrator defense. Forced termination during final migration database installation can require restoring the retained complete backup; ordinary exception rollback is tested. Total-case disk quota and automatic retention deletion remain outside this release.
