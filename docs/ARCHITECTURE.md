# Legacy backend architecture and safety contract

The `fragmented.py` and `image_recovery.py` backend added in 0.2.0 has a separate source-binding, validation, database, and resume contract in [FRAGMENTED.md](FRAGMENTED.md). The remaining sections describe the retained ordinary-MP4 backend only.

## Modules

`bmff.py` parses only required box/sample-table structures and rewrites chunk offsets. `storage.py` owns the SQLite catalogue, source identity checks, streaming signature scan, bounded read-only file cache, and shared JSON-writing and file-hashing helpers. `engine.py` implements candidate construction, whole-payload matching, scattered-extent search, ambiguity handling, and publication. `validate.py` runs FFmpeg/ffprobe without a shell and enforces timeouts. `cli.py` supplies commands and local reports.

The use of an intentionally narrow parser is deliberate: recovery requires reasoning about offsets outside intact container files, while conventional demuxers expect a readable container. FFmpeg remains the codec implementation. There is no attempt to reimplement H.264/HEVC decoding in Python.

## Data path

```text
Recovered files and optional raw images
    -> sequential signature scan + SHA-256
    -> complete moov records + candidate media extents in SQLite
    -> original sample/chunk model
    -> indexed whole-mdat candidates
    -> optional global NAL/sample-size anchor scan for unresolved records
    -> bounded combinations of scattered extents
    -> rebuilt moov offsets + copied encoded samples
    -> FFprobe full counting/decoding
    -> independent FFmpeg strict decoding
    -> unique result / ambiguous alternatives / unresolved / explicit search limit
```

The algorithm is evidence-driven but not an identity oracle. Sample-table compatibility and decoding can reject many incorrect associations, not all possible incorrect associations. `matched` is deliberately weaker than “authentic” or “forensically proven.”

## Storage and provenance

The database records source paths, size/mtime/device/inode identities, full SHA-256 at initial scan completion, candidate atom locations, metadata bytes/hashes, attempt mappings, validation output, settings, and version history. Each candidate's sidecar records every copied source byte range and its SHA-256, output offsets, metadata hash, and exact validation commands.

Original inputs are opened read-only. The tool refuses device nodes and a case/input containment relationship. It does not repair source filesystems, change ownership, remount media, or modify recovered files. Input symlinks discovered during directory traversal are skipped; explicit paths are resolved before checks. Case writes reject known symlink targets. An advisory process lock prevents concurrent writers.

New ordinary case roots are created with owner-only access (`0700`, further restricted by the process umask). Existing case permissions are preserved; opening a case does not silently change its sharing policy. The privacy of an existing case therefore depends on its current permissions and surrounding storage.

Report writers have two distinct policies. `atomic_text` replaces an owned report through a fresh private temporary file and atomic rename, so an existing hard link is detached rather than truncated. JSON, CSV, and HTML reports use this path; `write_json` supplies JSON serialization on top of it. `create_private_json` creates a fresh owner-only report and refuses an existing destination. Fresh diagnostic output uses the latter policy because its paths and recovery details can be private. These helpers do not grant permission to overwrite arbitrary user files.

This is not a hardware write blocker, a signed custody ledger, or a sandbox against a hostile local administrator. It also cannot determine whether two different mount paths ultimately point to the same physical recording medium. The operator must place the case on healthy output storage.

Source size/mtime/inode/device are checked on resume and range access. They are change detectors, not cryptographic verification of the entire source on every read. The full source hash is calculated on the first completed scan, not on every subsequent run. Selected source bytes are hashed again during reconstruction. A malicious modification that preserves filesystem identity fields is outside this protection model.

Reconstructed outputs and duplicate candidates use hard links only within the case when supported; unsupported filesystems use independent copies with an explicit warning and additional space checks. Copy them before editing. Successful candidate hashes are verified before reuse. Failed candidate MP4s are deleted after validation; their rejection records remain in SQLite.

## Resume semantics

A source interrupted while scanning is rescanned from its beginning. Complete sources are reused after identity checks. Each candidate attempt has a stable mapping digest. Completed successes and rejections can be reused when the validation tools and timeout are unchanged. Timed-out candidates are not cached as definitive rejections.

Completed result keys include source catalogue identities/hashes, tool versions, search options, and application version. `--limit` is deliberately excluded so a 50-record pilot can continue without invalidating completed work. Adding sources marks prior results stale and detaches publication names while retaining candidate data. Deep work has an `awaiting_deep` state so interruption between the two stages cannot accidentally suppress the deep pass on resume.

Reusing a completed result verifies both validated candidates and published output hashes. A missing or changed publication stops recovery without overwriting it, including when publication used an independent copy on a filesystem without hard links.

## Resource bounds

Source scanning uses 4 MiB blocks with lookahead, not a full-image byte array. The file cache has 32 handles. Metadata and sample counts have explicit parser ceilings. Whole-payload candidates are indexed by available size. Deep matching groups target anchors and scans sources once per bounded metadata batch. Search hits, tested target associations, and assembly combinations have explicit ceilings. Reaching one produces `search_limited`, not a uniqueness claim.

The shared `file_sha256` helper hashes files in fixed-size blocks. Recovery and diagnostic hashing do not allocate a byte array the size of the complete file. This bounds the hashing buffer, not the total memory used by parsing, decoding, or the operating system.

FFmpeg subprocess output is spooled to temporary files so pipe output does not accumulate unboundedly in Python memory; retained text is truncated to documented code limits. The subprocess timeout bounds execution time. Temporary log files can still use disk space while a process runs. Candidate output size and current free space are checked before writing. There is no total-case quota or automatic deletion of successful alternatives.

## Development and release discipline

Version 0.1.0 is an initial, experimentally validated release. No production-readiness, arbitrary-camera support, Apple Silicon execution, or large real-world recovery success is claimed. The exact local validation environment is recorded in `TEST_REPORT.md`. The provided CI runs the generated-media regression suite and builds/installs the package from clean environments. CI must actually be run in a repository before its platform coverage is treated as executed evidence.

Regression fixtures are generated, not downloaded, and compare recovered encoded media byte-for-byte with the undamaged original. Negative cases cover wrong candidates, ambiguity, missing media, malformed metadata, search limits, changed inputs, and interrupted operation. Future changes to supported layouts should introduce a failing fixture first and must not weaken ambiguity or preservation guarantees to increase apparent recovery rates.

The whole-codebase audit and shared-helper simplification are tracked in [AUDIT_SIMPLIFICATION.md](AUDIT_SIMPLIFICATION.md), including confirmed defects, deferred protection proposals, and the limits of local synthetic evidence.
