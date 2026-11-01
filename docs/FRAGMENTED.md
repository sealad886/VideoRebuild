# Fragmented MP4 strategy and safety contract

## Shared inputs and identity

Canonical `run`/`recover` uses the shared Case, ReaderPool, settings, jobs and reports described in ARCHITECTURE.md. Sources can be recovered regular files, raw image ranges, or explicitly mapped logical filesystem files. Physical extents may be noncontiguous; logical gaps and unrescued image bytes remain unavailable.

With `--report`, PhotoRec XML supplies initialization locations and recovered headers provide comparison bytes. Report image size must match the source image. Byte-run sector padding is clipped to logical filesize. Header files must match the image mappings exactly. Bare filenames are resolved beside report.xml's recup_dir; qualified names use explicit recovery-folder/basename components. Filenames do not establish recording identity.

Without PhotoRec, the shared bounded scan discovers ftyp/moov initialization candidates. Each initialization location remains a separate job, including equal header hashes. Search stops at the next initialization or source boundary. Each candidate section has a bounded buffer; later indexed starts remain eligible beyond earlier buffers. Repeated headers identify configuration, not unique recordings. Raw-image association is reported as a candidate; explicit logical files retain their filesystem/recovered-file evidence classification.

Image/map/report identities and settings are bound to the case. This is not a full forensic custody ledger or protection against a hostile administrator preserving metadata. Consumed ranges and publications are hashed. Inputs remain read-only.

## Supported container subset

FFmpeg and ffprobe 8+ are required for strict validation; `--reconstruct-only` skips codec subprocesses. Initialization consists of ftyp followed by moov, mvex/trex, empty stts/stsc/stsz/stco, one sample description per track and self-contained data references. Video is H.264 avc1/avc3 or HEVC hvc1/hev1; optional audio is AAC mp4a. Audio-only jobs are not exposed. Every declared track must have samples; none is silently discarded.

Fragments use mfhd plus traf/tfhd, optional tfdt v0/v1 and one or more trun boxes. default-base-is-moof addressing and an explicit first data offset per track are required; later runs may continue contiguously. Supported defaults/per-sample fields include duration, size, flags, sample-description index 1 and signed/unsigned composition offsets. Each moof is immediately followed by a finite mdat whose payload is exactly accounted for, without gaps/overlap/out-of-bounds samples.

Every video's length-prefixed NAL layout is checked; 1-, 2- and 4-byte length fields are supported. AVC NAL types 1..23 and HEVC 0..40 are accepted. Codec decoding remains FFmpeg's responsibility. The first video sample must contain AVC IDR type 5 or HEVC IDR 19/20 and must not be flagged non-sync. CRA/open-GOP restarts are not supported.

Within a section, sequences must increment by one and explicit tfdt bases must match accumulated per-track durations. The original-prefix path requires initial sequence 0 or 1; without tfdt it accumulates each track from zero. A later independent section can begin at another sequence, but needs explicit restart decode timing for every declared track. Missing cross-track phase is blocked rather than guessed.

Free/skip boxes between fragments are retained; intact mfra or new ftyp ends a section. An unsupported structure, clock discontinuity or unavailable range stops the current section. Candidate later moofs are independently assessed up to the next initialization or source boundary; discovery does not silently join a gap or associate arbitrary orphans across recordings.

## Reconstruction-only mode

With `--reconstruct-only`, fragment parsing, NAL framing, IDR starts, timing continuity, track completeness and allocation/acquisition checks still define eligible sections. FFmpeg/ffprobe validation is skipped. Outputs use `reconstructed_prefix` / `reconstructed_segment` labels (job status `reconstructed_prefix` / `reconstructed_segments`) with `decode_checked: false` and `scope: structural_reconstruction_only`. Original bytes and metadata remain preserved; no codec quality or playability is claimed. Default strict mode retains the validation contract below. Mode changes require `--retry`, preserve prior settings in a backup and revisit previous coverage without reindexing.

## Outputs and validation

`decoded_prefix` is a validated original beginning under this model. `decoded_segment` identifies a separately exported later section; an initialization job containing such outputs has `decoded_segments` status. Output files live in reconstructed/ and jobs reference their exact paths/hashes. A later output includes copied initialization plus surviving section bytes; encoded samples remain unchanged, but its whole-file hash need not equal a contiguous image range.

Publication requires supported structure, sequence, timing, all-track presence, IDR start, exact encoded-packet hashes/counts and full strict decoding. Packet DTS/PTS/duration are independently checked using rational time bases, including documented MOV normalization for negative composition offsets. Decoder diagnostics prevent publication even with process exit zero. A successful clip does not establish complete historical recording boundaries, camera identity, wall-clock dates or absence of original dropped frames.

Each output has an accompanying JSON sidecar with `ordered_fragments`: initialization, retained moof/mdat fragments and any container padding in exact output order. Entries record ordinals, output offsets and lengths, hashes, logical source ranges, bound physical extents and identities, plus sequence, per-track sample counts and decode-time summaries where available. Validation status distinguishes strict decoding from structural reconstruction. Existing outputs are backfilled from retained provenance and bounded structural reads of their own bytes; insufficient historical proof is explicitly marked `historical_manifest_unavailable`. Sidecar verification accompanies publication reuse. Manifests also separately record acquisition status, association, validation and gaps. Blocked/decode_failed/search_limited outcomes retain reasons. No frames are generated, duplicated, interpolated or filled to conceal missing bytes. Clips across missing intervals are not combined automatically. Private diagnostic windows are no longer exported during ordinary recovery; diagnostic tools are explicit separate operations.

## Candidate tiers

Accepted output provenance demotes recovered physical intervals from first-tier references without deleting inputs, atom indexes or media files. Demotion follows physical identity through aliases and mapped streams. Initialization metadata remains readable; media windows are clipped around demoted intervals, and a resumed target may reuse its own accepted ranges for revalidation.

`--include-recovered` enables fallback only after every eligible first-tier start for that initialization has been examined without an output and without a limiting stop. Otherwise the job records that second-tier evidence remains unsearched. Fallback retains allocation/acquisition and section-boundary restrictions. Results describe searched tiers and do not establish absence or global uniqueness across an unsearched tier. Existing publications remain intact while retrying changed coverage.

## Resume, bounds and limits

One shared case lock prevents concurrent writers. Initialization jobs checkpoint after their bounded attempt. Interruption retries uncommitted work; an existing output can be reused only if bytes agree exactly. Completed publications and validation evidence are verified before reuse. Historical migrated evidence retains its original validation scope. Search policy is bound separately from immutable initialization-buffer, tool, version and timeout settings. Expanding a previous finite start cutoff requires `--retry`; a legacy combined binding is backed up and migrated only when its immutable settings match. Retried limited jobs verify and preserve existing publications while searching additional starts. Explicit `--retry` also revisits failed or limited requested-mode validation even when other accepted outputs remain; starts already covered by an accepted section in that mode stay complete. Revalidation writes a distinct sidecar when one already exists, retaining the prior committed manifest while new evidence and its pointer are committed. Interruption before that commit therefore leaves the previous publication verifiable.

The per-section buffer defaults to 16 MiB and is capped at 256 MiB. This is not a cumulative read quota or a limit on how far later indexed starts may occur. Initialization is capped at 1 MiB; moof at 4 MiB. Each fragment and retained prefix have explicit sample budgets (200,000); samples are at most 64 MiB, and NAL counts are capped at 4,096 per sample. First-tier candidate starts, including rejected starts, have no default count cutoff; `--max-segments` optionally sets one. Prefilter bytes are reused during assembly. Allocation and acquisition masks split available spans; a section never bridges an excluded span. Budget stops retain validated clips and report search_limited. Subprocess timeouts and output-spooling bounds remain enforced. Hitting a limit is not proof that later media is absent.

Candidate plus publication can consume about twice retained encoded bytes, with additional evidence/log space. There is no total-case quota or automatic retention deletion. Use healthy output storage with capacity for attempted alternatives.

Missing-initialization reconstruction, automatic cross-recording association, arbitrary cluster permutation, absolute tfhd addressing, encryption/CENC, external references, edit lists/auxiliary/proprietary tracks, Annex-B conversion, timestamp OCR and wall-clock joining remain unsupported. Explicit raw filesystem extent imports support physical fragmentation without claiming a general proprietary filesystem recovery engine.
