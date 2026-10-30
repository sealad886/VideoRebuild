# Whole-codebase audit and simplification ledger

Assessment baseline: `0d60ba232677b5f3c021d426d8474e960e0d990e` (`0d60ba2`). Scope covered the whole VideoRebuild codebase: ordinary and fragmented reconstruction, image recovery, parsing, storage, validation, CLI/diagnostics, launchers, tests, and supporting documentation. Separate assessments completed before source repairs began. The user then authorized repairs and simplification. This document is the canonical ledger for that pass.

Experiments used disposable generated fixtures. No private recordings or recovery cases were inspected, and no real-media recovery completeness or throughput claim follows from these results.

## Confirmed findings and repair boundaries

| ID | Severity | Baseline behavior and consequence | Authorized repair | Verification state |
| --- | --- | --- | --- | --- |
| F1 | Medium | JSON replacement used a predictable `*.tmp` path: a hard link planted there caused source truncation before rename. CLI CSV/HTML reports opened their final destinations directly, so hard links at `results.csv` or `index.html` also caused source truncation. Both cases were reproduced with disposable source files, including the public scan/report flow. | Share `atomic_text` for JSON, CSV, and HTML reports: use a fresh private temporary file and atomic rename, detaching destination links without truncating the linked inode. | Fixed; public three-format hard-link regression and atomic failure/cleanup tests pass. |
| F2 | Medium | The XML declaration check matched raw ASCII bytes. UTF-16 XML bypassed it and expanded an internal entity in both import and diagnostic parsing. | Reject DTD/entity declarations at the XML parser boundary regardless of input encoding; preserve normal PhotoRec XML parsing. | Fixed; encoded XML, split-declaration, normal import, and diagnostic tests pass. |
| F3 | Medium | New ordinary case roots used ambient permissions. Under umask `022`, the root was `0755` and reconstructed media/database files were `0644`, allowing other users with parent access to read them. | Create new ordinary roots with `0700`; preserve existing case permissions. | Fixed; new-private/existing-permissions regression passes. |
| F4 | Medium | Fresh diagnostic JSON used ambient permissions. Under umask `000`, a report containing private paths was created as `0666`. | Use the shared fresh private JSON writer, with owner-only creation and refusal to overwrite an existing path. | Fixed; permissive-umask and existing-output refusal regressions pass. |
| F5 | Low | Diagnostic archive hashing read the whole file into one Python byte array. A 16,779,157-byte synthetic archive produced a traced peak of 16,801,110 bytes. | Use the shared streaming file hash helper so hashing buffer size does not grow with archive size. | Fixed; hash regressions pass and the synthetic before/after allocation experiment confirms reduced peak memory. |

F3 is a confirmed confidentiality default, with the new-case privacy policy made explicit by the repair. Existing cases retain their permissions and require an operator decision if their sharing policy should change.

## Simplification decisions

`storage.py` is the common home for `atomic_text`, `write_json`, `create_private_json`, and `file_sha256`. Recovery and diagnostic callers use those helpers; duplicated engine/image-recovery implementations and the CLI's private engine JSON import are removed. `write_json` delegates file replacement to `atomic_text`, which also serves CSV/HTML reports. Diagnostic XML parsing shares the import reader. Fresh-file creation and replaceable-report writing remain separate operations because refusing an existing user destination and updating an owned report are different contracts.

The repairs preserve reconstruction decisions, encoded-byte copying, ambiguity reporting, and bounded-search semantics. They do not broaden supported media layouts or substitute successful decoding for proof of authentic source association.

## Deferred proposal: source changes after the last read

O2 is confirmed behavior, not a demonstrated contract breach: a generated source changed after candidate decoding, and recovery still published the already validated output. Its copied bytes remained the earlier snapshot. The documented ordinary-backend contract checks source identity on resume and range access; it does not promise continuous source monitoring or a final publication-time check. No image corruption was demonstrated by this experiment.

A final identity check could strengthen change detection, but that is a separate protection-policy decision. It is deferred rather than silently added as a prerequisite for publishing a correct snapshot. Filesystem identity checks also cannot detect malicious modifications that preserve all checked fields.

## Evidence and acceptance limits

Before repairs, the full suite passed **129 tests in 35.661 seconds**. Passing tests did not cover the confirmed defects above. An independent ordinary-backend subset also passed five tests in 2.019 seconds: scattered AVC/AAC recovery, scattered HEVC/AAC recovery, intact faststart B-frames, changed-source resume rejection, and missing-extent rejection.

Disposable baseline proof artifacts were recorded at `/tmp/videorebuild-diagnostic-assessment.json`, `/tmp/videorebuild-ordinary-source-change-probe.py`, and `/tmp/videorebuild-engine-storage-audit-tests.log`. Temporary paths are session evidence, not durable repository fixtures. The diagnostic record showed UTF-8 declaration rejection, UTF-16 entity expansion, matching archive hashes, diagnostic exit status 0, and report mode `0666` under the permissive test umask.

After the diagnostic repairs, the same 16,779,157-byte padded ZIP produced a traced peak of 2,119,482 bytes, down from 16,801,110 bytes; both runs matched an independent streaming hash oracle. UTF-8 and UTF-16 declarations were both rejected, and fresh diagnostic JSON was created as `0600`. These measurements describe that synthetic diagnostic run, not total recovery memory or a universal peak-memory ceiling.

Ten focused storage, diagnostic, and audit tests passed in 4.063 seconds before the CSV/HTML extension. Final integrated verification passed **140 tests in 53.452 seconds** with:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

The 11 added test methods comprise seven storage tests, two XML reader/import tests, and two diagnostic tests. The storage suite includes public scan/report regressions for all three report formats. All 103 preexisting retained evidence files had unchanged hashes. `doctor` passed with Python 3.13.18 and FFmpeg/ffprobe 9.0.2; `graphify update .` and `git diff --check` passed. The canonical zipapp builder also passed isolated smoke checks for version, doctor, and `recover-fragmented --help`; source and builder were copied to a temporary directory so the existing local archive was preserved. An independent final review of the complete repair diff and regression tests reported no actionable findings; it did not rerun the suite.

PR review identified one additional failure-path issue: fresh private JSON could leave a partial report after serialization or syncing failed, blocking retry at the same filename. The shared writer now removes its own incomplete file after failure, checking file identity before cleanup to preserve a replacement path. Two additional regression methods cover failure/retry and replacement-path preservation. After this correction, the full suite passed 142 tests in 50.120 seconds.

The subsequent PR review also found that stream-close failures lay outside the cleanup handler; cleanup now covers the full stream context, with a close-failure/retry regression. During review, a concurrent commit raised the package minimum to Python 3.13. The user confirmed that policy, so current launcher checks, runtime enforcement, CI and usage documentation are aligned with it; dated validation receipts retain their original interpreter versions. The combined final source suite passed 147 tests in 76.280 seconds on Python 3.13.18.

Scope excludes private-media acceptance, large real-world image recovery, exhaustive malformed-container exploration, hostile administrator resistance, and performance certification on other machines. The assessment found no confirmed ordinary reconstruction-algorithm defect within the inspected paths and bounded experiments; that is not a completeness proof.
