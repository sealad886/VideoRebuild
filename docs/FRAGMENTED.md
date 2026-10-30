# Fragmented backend: scope and safety contract

## Runtime

Both FFmpeg and ffprobe must be version 8 or newer. `doctor` rejects either
executable below this floor before recovery begins. FFmpeg 7.1.5 can report a
codec-inferred packet duration instead of the MOV container's sample duration
when composition-time entries are present; FFmpeg 8 records that duration in
its packet timing data. The independent timing gate remains exact rather than
accepting a guessed duration. See the [versioned source references](REFERENCES.md).

## Inputs and identity

The authoritative media source is a regular raw disk-image file, opened read-only. PhotoRec `report.xml` supplies byte locations; recovered `.mp4` headers provide exact comparison bytes. XML byte-run lengths can include sector padding, so comparisons are clipped to the logical `filesize`. For example, a 1,174-byte header mapped over 1,536 bytes is compared using 1,174 bytes, not 1,536.

Every initialization location is a separate SQLite job. A repeated header hash identifies a codec configuration, not a unique recording. Numeric PhotoRec names and `mfhd` sequence numbers are not calendar timestamps or globally unique recording identifiers.

The report is streamed into SQLite, not loaded as a document tree. Small MP4 entries up to 1 MiB are indexed. Record candidates are processed in source-offset order. Each source window ends at the next reported initialization location, the per-record size ceiling, or image end, whichever comes first. This boundary is intentionally conservative: an overlapping or false-positive initialization can cause a false negative, but not a cross-boundary concatenation claim.

PhotoRec may use bare filenames for files beside `report.xml`, then qualified paths for later recovery folders. A bare filename is mapped only to the report's containing `recup_dir.*` folder. Qualified paths use their final recovery-folder and basename components. Image-byte comparison remains required; filenames never establish recording identity.

The image size must equal the report's source size. The code does not infer whole-disk versus partition rebasing. Each processed recovered header must match its report-mapped image bytes exactly. A split header must also appear physically contiguous at its starting location before this backend can follow its media.

Source binding uses path, size, inode/device, mtime/ctime, sampled image hashes, a full XML hash, and tool/recovery settings. It is not a full-image forensic hash, hardware write blocker, or defense against an adversarial local administrator preserving metadata. The exact consumed image window and published byte prefix are hashed separately. The image is checked for changes before/after each candidate; recovered headers are checked before/after reads.

## Implemented format subset

An initialization consists of an exact `ftyp` followed by `moov`, with `mvex`/`trex`, empty `stts`/`stsc`/`stsz`/`stco`, one sample description per track, self-contained data references, H.264 (`avc1`/`avc3`) or HEVC (`hvc1`/`hev1`) video, and optional AAC (`mp4a`). Audio-only recovery is not exposed in this backend. All declared tracks need samples; none are silently discarded.

Fragments contain `mfhd` and one or more `traf` boxes. Each track fragment uses `tfhd`, optional `tfdt` version 0 or 1, and one or more `trun` boxes. `default-base-is-moof` addressing is required. The first run for each track has an explicit moof-relative data offset; subsequent runs may continue contiguously. Supported defaults are duration, size, flags, and sample-description index 1. Per-sample durations, sizes, flags, first-sample flags, and signed/unsigned composition offsets are parsed.

Each `moof` is immediately followed by one finite-length `mdat`. Referenced samples must account for its payload exactly, without gaps, overlap, or out-of-bounds data. Every video sample's length-prefixed NAL layout is checked. HEVC is limited to recognized NAL types 0..40; H.264 to 1..23. Supported length fields are 1, 2, or 4 bytes. Codec decoding remains FFmpeg's responsibility.

The first fragment sequence is 0 or 1; successors must increment by one. Sequence wraparound is not guessed. Without `tfdt`, timestamps accumulate independently per track from zero along the verified sequence. With `tfdt`, each later base must agree exactly with that track's accumulated end. Clock discontinuities stop the prefix. This supports the observed recorder layout without pretending that browser MSE requirements are a universal file-validation rule.

The first video sample must contain an IDR (HEVC type 19/20 or AVC type 5) and must not be flagged non-sync. CRA-only starts and recovery beginning in the middle of a GOP are not claimed. Full decoding errors—including stderr diagnostics with a zero process exit code—prevent publication.

`free` and `skip` boxes between fragments are retained unchanged. An intact `mfra` or a new `ftyp` ends the prefix. Other top-level layouts stop the prefix. There is no blind signature hunt to bridge an unexpected gap in the recovery path. A separate diagnostic inspector can find valid embedded pairs in an over-carved `mdat`, but it never automatically associates those orphans with a recording.

## Outputs and interpretation

`decoded_prefix` means the exact contiguous image byte prefix passed: structure, sequence, timing, all-track presence, IDR-start checks, exact encoded-packet hashes, packet/frame counts, and full decoding. Packet DTS, PTS, and duration are independently compared with the parser's per-track timeline using exact rational time bases. For negative composition offsets, the comparison accounts for FFmpeg MOV demuxing's per-track PTS normalization (the largest negative offset's magnitude); arbitrary timestamp shifts or tolerances are not accepted. Missing timing fields or a mismatch prevent publication. It does not establish completeness, original recording identity, wall-clock date, or whether the camera originally dropped frames. A partial prefix may be published after a later truncation; its stop reason and original-image stopping offset are retained.

`blocked` means no eligible prefix under the implemented model. `decode_failed` means a structurally eligible candidate failed decoding, packet hashes, counts, or a validation timeout. Validation logs are retained. No video or audio is generated, re-encoded, interpolated, duplicated, or filled to conceal missing bytes.

Both the attempt copy and published prefix consume disk space (up to approximately twice the accepted encoded bytes). Failed attempt candidates remain for diagnosis. There is no automatic retention deletion or whole-case disk quota. Input files are never altered; disk-full errors abort explicitly. Choose healthy output storage with sufficient capacity.

## Resume and limits

Completed jobs are checkpointed per image offset. An interrupted uncommitted job is retried; prior uniquely named attempt folders remain as evidence. A publish-before-commit interruption can reuse an output only if its bytes match the new candidate exactly. A conflicting existing output causes a hard error. Completed results are not silently invalidated.

Before processing pending jobs, resume verifies every completed published prefix against its stored size and SHA-256. Missing or changed output or validation evidence stops with exit code 2 and preserves the case for investigation. This reads completed output bytes again but does not repeat decoding.

Only the pilot count and diagnostic-window settings may change while reusing a case. A source fingerprint, tool, timeout, application version, or record-window limit change requires a new case. Thus a timeout is not a claim that recovery is impossible, but raising the timeout is an explicit new run/case.

A per-record image window defaults to 16 MiB, configurable from 1 KiB to 256 MiB. Initialization is capped at 1 MiB and moof at 4 MiB. A fragment has at most 200,000 samples; each sample is at most 64 MiB. NAL counts are capped at 4,096 per sample. Temporary subprocess output is spooled to disk, retained probe JSON is capped at 64 MiB (including packet hashes and timing), retained stderr at 64 KiB, and each external validation process defaults to a 120-second timeout. Hitting a limit is a reported stop/rejection, not proof of missing source data.

The retained prefix also has a total budget of 200,000 samples across all fragments. A fragment exceeding the remaining budget is not retained; parsing stops with `prefix_sample_budget_exceeded`, preserving the preceding complete prefix for validation. This is an operational memory bound, not a claim that later source samples are missing or malformed.

## Not implemented

Arbitrary filesystem cluster-chain reconstruction; noncontiguous original fragments; cross-recording fragment matching; missing initialization reconstruction; absolute `tfhd` offsets; browser streaming MSE conformance; encryption/CENC; external references; edit lists or track-reference relationships; auxiliary/proprietary tracks; Annex-B conversion; timestamp OCR; wall-clock chronology; joining separate recordings; compressed image containers; raw device access; repair of source filesystems.

The distinction is important: fragmented MP4 (the container layout) is not the same as a file being fragmented across filesystem clusters. This backend handles the former in contiguous source ranges. It does not silently claim to solve the latter.
