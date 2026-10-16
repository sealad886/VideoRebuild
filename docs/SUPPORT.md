# Legacy sample-table reconstruction models

This page describes `run`/`recover`. Version 0.2.0 separately adds `recover-fragmented`; see [FRAGMENTED.md](FRAGMENTED.md).

## Ordinary MP4/MOV

Supported metadata uses one sample description per track, self-contained media references, `stsc`, `stts`, `stsz` or `stz2`, and `stco` or `co64`. Composition offsets (`ctts`) and sync-sample tables (`stss`) are checked and retained. AVC entries `avc1`/`avc3` and HEVC entries `hvc1`/`hev1` are accepted. Audio must be a version-0 `mp4a` sample entry that decodes as AAC. Every track must be supported; unsupported tracks are not silently discarded.

Normal reconstruction supports 1-, 2-, and 4-byte NAL length prefixes. It copies every encoded sample and rewrites every chunk offset as 64-bit `co64`. It preserves the surviving `moov` hierarchy except for offset tables and enclosing box sizes. A new generic ISO BMFF `ftyp` and `mdat` are written. Top-level proprietary boxes outside `moov` are not copied. This is media reconstruction, not a byte-identical copy of the original container.

The whole-payload model assumes the first referenced sample begins at the candidate `mdat` payload start; it preserves original inter-chunk gaps when locating bytes, then omits unused gaps from the rebuilt payload. Candidates with more than `--max-padding` trailing bytes are outside that search model. Original-relative source mappings are tested separately.

## Scattered extents

Deep recovery does not require surviving `mdat` headers. A complete, contiguous `moov` still must exist somewhere among the indexed sources.

A deep extent is a video chunk plus the subsequent audio chunks before the next video chunk. Any leading audio chunks are assigned to the first video extent. The original relative offsets of chunks within an extent are preserved while matching. Therefore:

- A recording can be assembled from many out-of-order extents, even across many recovered files.
- Video and associated audio must still be physically contiguous according to this grouping.
- A filesystem fragment boundary inside the group is not solved.
- One long video-only MP4 chunk remains one extent even if it contains many frames. Deep search is not frame-by-frame reconstruction inside that chunk.

Candidate anchors use selected sample sizes and 4-byte NAL prefixes. The search accumulates up to 65,536 NAL units per chain, matching the ordinary sample validator's bound, then validates every video sample against the sample-table boundaries. A terminal one-byte AVC NAL is supported; HEVC requires its two-byte header. Candidate sample anchors are selected from the first eight and last four samples of each video chunk, with sizes from 8 bytes to less than 16 MiB. `--max-anchor-checks` separately bounds both target associations and total NAL-chain checks. Exhaustion stops the scan with `search_limited`; result details distinguish `nal_check_budget` and `anchor_check_budget` and retain both counters. These search limits are part of the documented model, not evidence that other layouts cannot be recovered.

AAC has no equivalent length-prefix identity signature here. Its association comes from the surviving layout and the contiguous-extent hypothesis; AAC is independently decoded and counted before publication. A decodable wrong audio association remains theoretically possible. This release does not prove source identity or use face/audio recognition.

Byte-identical extent hits are deduplicated to one representative physical location. Source-range reuse is prohibited in a proposed assembly. If a physically different duplicate location would be required to avoid overlap, deduplication can cause a false negative. This is conservative; it never fills a missing extent by looping known footage.

## Explicit non-support

| Situation | Behavior |
|---|---|
| No surviving usable `moov` | No reconstruction from media alone; source/atom counts are still reported. |
| Truncated, split, or compressed metadata | No inferred metadata or guessed sample tables. |
| Fragmented MP4 (`moof`, `mvex`, `trun`) | Rejected by this legacy backend. Use the separately implemented `recover-fragmented` backend with the source image. |
| Encrypted tracks / CENC auxiliary offsets | Rejected. |
| External data references | Rejected before FFmpeg. |
| Unsupported codec or auxiliary track | Whole metadata record rejected, rather than silently dropping the track. |
| Annex-B `.h264`/`.h265` elementary streams | May be scanned for signatures, but are not converted or rebuilt as MP4 in this release. |
| Proprietary DVR filesystem/container | No proprietary filesystem parser. |
| Arbitrarily shuffled sectors or bytes within a sample-table extent | Not solved. |
| Overwritten/missing video data | No content generation, frame duplication, gap filling, or fabricated repair. |
| Intact short recordings without reliable dates | Reconstructed individually if supported; not joined into a guessed timeline. |
| Live device nodes / compressed images / mounted filesystem repair | No raw-device access, decompression, or filesystem modifications. |

## Validation meaning

A successful probe alone is insufficient. Publication requires an exact structural assignment of all chunks, nonoverlapping in-bounds source ranges, correct length-prefixed NAL framing for every video sample, full frame/packet counting without reported errors, expected stream codec/types and counts, and a second full strict decoding pass. Edit lists are ignored for validation counts only and are preserved in output metadata.

Conservative validation can reject playable but unusual files, including field-coded streams, nonstandard packet/frame relationships, permissively decoded corruption, or unsupported track arrangements. Such rejection is preferable to announcing an unverified repair. No successful result proves chronology, source-camera identity, or preservation of information absent from the supplied sources.
