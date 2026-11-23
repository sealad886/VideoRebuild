# Disk image recovery release plan

Recover more verifiable footage per hour from a read-only disk image. The
release should keep source location, byte availability, playback validation,
recording association, and completeness separate. An image may contain bytes
from several generations of a file. Correct playback alone cannot identify
the generation. A replaced payload with no surviving copy cannot be recovered
from that image.

## What this increment changes

VideoRebuild already reads DFXML byte runs through the same checked reader as
direct image recovery. It can reconstruct complete ordinary MP4 sample tables
and export validated sections of supported fragmented MP4. The ordinary path
has required every sample-table chunk. A damaged chunk can therefore block
publication even when later groups of pictures are intact.

The first increment adds separate ordinary MP4 salvage after indexing a mapped
image. It uses a complete `moov` from the mapped logical file, honors the
acquisition map, and starts a clip at a verified instantaneous decoder refresh
(IDR) picture. It retains supported AVC or HEVC video bytes and local media
timing. It omits audio and ends each clip at a gap. The result is a validated
clip associated with a filesystem mapping. Historical identity and recording
completeness remain undetermined. Use the source sample range and physical
extents in its manifest to audit the association. [GNU ddrescue mapfile manual](https://www.gnu.org/software/ddrescue/manual/ddrescue_manual.html)

The user workflow is to scan the image with `--extents` and, when available,
`--mapfile`, then run `videorebuild salvage --case CASE`. Ordinary recovery
remains the path for complete files. Positive mapfile ranges allow reads. Their
status does not say which file last occupied the sectors. A map hole remains
unavailable even when bytes are visible at that offset in the image.

Salvage drops the affected GOP as a unit. It can split a decoder-rejected span
at proven IDRs, up to the configured attempt count. `--retry` revisits jobs
that stopped at a validation limit and verifies previously emitted clip and
manifest hashes before reuse. An intact video track can report
`salvage_not_needed` even if its audio is damaged. Tracks larger than the
per-clip byte bound may produce independent excerpts while the full track's
decode status remains unknown.

## Next recovery increments

1. **Read filesystem evidence and scan the uncovered image in one case.**
   Start with one measured camera-card filesystem. Preserve file-record and
   directory-entry allocation states separately. Bind runs and free-space
   maps to the partition and image identity. Prefer surviving byte runs and
   visit remaining eligible spans once with the existing structural index.
   Report allocated, deleted, reused, and unknown ranges separately. Deduplicate
   physical witnesses before counting output. A deleted name may outlive and
   disagree with a reused file record. [Sleuth Kit `fls`](https://wiki.sleuthkit.org/fls/) · [DFXML schema](https://github.com/dfxml-working-group/dfxml_schema/blob/main/dfxml.xsd)
2. **Search fragment associations under a budget.** Use sample-table sizes,
   decode order, codec configuration, and physical non-overlap to reject
   impossible assignments early. Rank the remainder using sequence and codec
   continuity, then apply exact checks. If two assignments remain defensible,
   preserve the ambiguity. If time, memory, or disk limits stop a search,
   record the unsearched scope and retain the frontier for resume. False joins
   between plausible metadata and media are a documented failure mode.
   [Casey and Zoun, DFRWS 2014](https://dfrws.org/sites/default/files/session-files/2014_USA_paper-design_tradeoffs_for_developing_fragmented_video_carving_tools.pdf)
3. **Add a separate path for missing initialization.** Prefer in-band decoder
   parameters, then a verified same-profile donor. Test constrained inference
   only when neither exists. Identify each supplied or inferred parameter and
   timing assumption in the manifest. Keep competing configurations separate.
   H.264 parameter search has demonstrated fragment decoding, with a narrower
   goal than establishing original metadata, audio sync, or recording identity.
   [Altinisik and Sencar, IEEE TIFS](https://doi.org/10.1109/TIFS.2021.3118876)
4. **Expand supported formats using labeled evidence.** Prioritize audio with
   independently verified sample timing and measured camera-specific layouts.
   Add filesystems or codecs only after both recovery yield and false joins are
   measured on held-out images. Recent carving systems offer bounded parallel
   search ideas. Published Scalpel3 experiments on PNG and GIF do not measure
   MP4 performance. [Scalpel3 preprint](https://arxiv.org/abs/2608.20363)

## How to qualify a release

Create small reproducible image fixtures with pristine originals and known
logical-to-physical offsets. Include intact files, shuffled extents, a mapfile
hole with old bytes left in the image, a known overwritten sample, a same-codec
replacement from another recording, a missing `moov`, and supported fragmented
MP4. Cover decode and presentation order, short and long GOPs, and an interrupted
run that must reproduce the same accepted result on resume. Exercise real deleted
entries and reused clusters in filesystem-specific fixtures before claiming
deletion support. Keep acquisition and original file identity as separate truth.

Run an independent packet oracle on pristine originals and output. Capture
packet hashes, ordinals, codec configuration, DTS, PTS, and duration in the
original timescale. Rebase timestamps only as allowed by the output contract.
Test strict decode and expected frame counts independently of byte equality.
Check that every claimed physical range is eligible and that the source image
and mapfile hashes are unchanged. [ffprobe documentation](https://ffmpeg.org/ffprobe.html)

| Measure | Release evidence |
| --- | --- |
| Exact sample precision | Correct source recording, track, ordinal, and encoded bytes among attributed outputs. Count foreign or wrongly attributed samples separately from omitted samples. |
| Exact sample recall | Unique verified samples recovered divided by the verified surviving samples within the tested support scope. Also report coverage against the whole original. Repeated copies do not improve either score. |
| Uncertainty and usable duration | Count explicit ambiguous and unsupported cases. Report independently decodable duration and identify gaps. Keep inferred or unattributed clips in their own category. |
| Time and resources | Record wall time by stage, bytes read, validation work, peak memory, scratch growth, and resume cost after cancellation or a disk-space stop. |

Establish a baseline with the current release and appropriate comparison tools
under equivalent inputs. Record exact revisions, commands, map coverage,
hardware, image size, media formats, and tolerances. Use the public NIST carving
images and originals as a second, independently sourced baseline. For a claim
beyond synthetic cases, test recent camera files on a held-out corpus with
recording-level truth. Do not tune parameters after unsealing its ground truth.
[NIST carving images](https://cfreds-archive.nist.gov/FileCarving/index.html) · [NIST originals](https://cfreds-archive.nist.gov/FileCarving/TestFiles/index.html) · [NIST scientific foundation review](https://nvlpubs.nist.gov/nistpubs/ir/2022/NIST.IR.8354.pdf)

Use 24 hours per image as an operating target on declared hardware and image
sizes. Reading 1 TB once in 24 hours alone requires about 11.6 MB/s. Candidate
validation, repeated reads, and output add to that cost. Measure terminal
results and progress at the deadline. A run that reaches the limit must retain
accepted clips, record what it did not examine, and resume without repeating
committed work. Report test results for supported cases separately from an
unbounded or larger search.

The first increment is ready for a release candidate when the public CLI
recovers exact surviving intervals around known damage, excludes known invalid
and unavailable data, preserves source evidence, and resumes correctly. A
structurally plausible replacement must carry association uncertainty.
Require the normal test and artifact checks on the exact release revision.
Broader recovery and performance claims require the measured comparison and
hold-out results above. Package publication follows the separate release gates
in [the release procedure](RELEASING.md).
