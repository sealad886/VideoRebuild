# Legacy sample-table backend

This is the retained manual for the `scan`, `run`, `recover`, and `report` commands. For the new image-first fragmented-MP4 backend, read the root README and FRAGMENTED.md.

# VideoRebuild 0.1.0

Local, open-source MP4 reconstruction for jumbled PhotoRec recoveries and raw disk images. MIT licensed. No cloud service, model, telemetry, or Python runtime dependency outside the standard library. FFmpeg and ffprobe are required external executables.

**This is a working first release, not a general-purpose forensic recovery product. It has been tested on generated H.264/HEVC recordings that were deliberately separated and scattered. It has not been tested against your camera or your recovery set. Read the support boundary before a large run.**

## What it actually reconstructs

VideoRebuild finds complete surviving `moov` metadata, derives the original sample/chunk layout, searches for compatible encoded media, rewrites chunk offsets, and builds a new MP4. It does not just concatenate filenames or rename clips.

It has two working search stages:

1. **Whole-payload matching:** matches a metadata record to an `mdat` payload, including separate PhotoRec `_ftyp`/`_mdat` pieces. Tests original-relative offsets and relocated payloads. File naming and physical proximity do not determine acceptance.
2. **Scattered-extent matching (`--deep`):** searches recovered files or a raw image for NAL-length patterns matching sample-table sizes. It can reconstruct a recording from multiple out-of-order extents. Each extent consists of one video chunk and its adjacent audio chunks; the bytes within that extent must still be contiguous. Multi-NAL keyframes are supported by bounded NAL-chain searches.

Every published result must satisfy structural checks, a full ffprobe count/decode pass, an independent strict FFmpeg decode, and exact expected video/audio packet and decoded-frame counts. Alternative byte-distinct results remain ambiguous. Search budgets and timeouts are explicit, not silently treated as success.

**Required:** a usable original sample table in a complete `moov`, AVC/H.264 or HEVC/H.265 video, and optional AAC audio. **Not supported:** missing-moov inference, arbitrary sector permutations within an extent, fragmented-MP4 `moof` assembly, proprietary camera containers, or chronological joining of already complete clips. See [the full matrix](docs/SUPPORT.md).

## Run on macOS

The simplest distribution is the bundled `videorebuild.pyz`. It needs Python 3.11 or newer but does not need pip or a virtual environment.

The reference runtime for this release is Python 3.13 and FFmpeg 7.1.5. The following Homebrew major-version formulae avoid silently selecting a different FFmpeg major version. Patch versions can still change; `doctor` records the actual executables and versions.

```bash
brew install python@3.13 ffmpeg@7
export PATH="$(brew --prefix python@3.13)/libexec/bin:$(brew --prefix ffmpeg@7)/bin:$PATH"
python3 videorebuild.pyz doctor
```

From the extracted project folder, use the guided launcher:

```bash
bash ./run-macos.command
```

It prompts for the PhotoRec folder, a case/output parent folder on healthy storage, an optional raw disk image, and either a 50-record batch or all remaining records. It creates/reuses `VideoRebuild-case` beneath the selected output parent. Deep search is enabled. It does not install software or grant permissions. Canceling a picker aborts.

**Do not choose the original recording card as the output destination.** The program refuses a case folder inside the recovery input tree, but it cannot know whether another folder on the same physical device is safe. Make that storage choice yourself. It refuses device nodes such as `/dev/rdisk9`; supply a regular, uncompressed raw image instead.

### CLI with native folder pickers

This is a complete macOS command sequence, without guessed recovery paths:

```bash
RECOVERY="$(osascript -e 'POSIX path of (choose folder with prompt "Select the parent folder containing recup_dir folders")')"
DESTINATION="$(osascript -e 'POSIX path of (choose folder with prompt "Select a folder on healthy output storage, outside the recovery tree")')"
CASE="${DESTINATION%/}/VideoRebuild-case"

python3 videorebuild.pyz run \
  --input "$RECOVERY" \
  --case "$CASE" \
  --deep \
  --limit 50

open "$CASE/reports/index.html"
```

The 50-record limit applies to reconstruction, not source indexing. The whole input set is indexed so that possible matches outside the first output directory are not missed.

Continue all remaining indexed records without rescanning or reprompting:

```bash
python3 videorebuild.pyz recover --case "$CASE" --deep
open "$CASE/reports/index.html"
```

Add a source image to the same case:

```bash
IMAGE="$(osascript -e 'POSIX path of (choose file with prompt "Select the uncompressed raw source disk image")')"
python3 videorebuild.pyz run --case "$CASE" --image "$IMAGE" --deep
```

Previously indexed recovered files remain part of that case. New evidence invalidates prior uniqueness claims and triggers reevaluation; validated candidate bytes are retained.

## Output and interpretation

```text
VideoRebuild-case/
  case.sqlite3          Indexed sources, metadata, attempts, and resume state
  run.log               Progress and errors
  reconstructed/        Uniquely matched, decode-checked MP4s and result sidecars
  candidates/           All successful candidates and per-chunk provenance JSON
  reports/
    index.html          Local report; no web server or external assets
    summary.json        Counts/codecs/reasons; no video or source paths
    results.json        Full inventory and evidence; includes local source paths
    results.csv         One row per metadata record
  work/                 Temporary candidate being validated
```

`reconstructed` entries and byte-identical candidates use **case-internal hard links** where supported to avoid duplicate storage. On filesystems without hard-link support, such as exFAT, the tool logs a warning and retains independent copies instead, requiring additional space. They are never hard links to source material. Copy a result outside the case before editing it. Resume verifies stored candidate hashes and refuses altered candidate bytes.

| Status | Meaning |
|---|---|
| `matched` | One byte-distinct candidate passed within the completed search model. Published under `reconstructed/`. |
| `ambiguous` | More than one different candidate passed. Kept in `candidates/`; none auto-selected. |
| `unresolved` | No candidate passed within the searched model. This does not prove the media is unrecoverable. |
| `search_limited` | A budget or timeout prevents a completed search. Successful trial candidates may exist, but none is published as a unique match. |
| `unsupported` | Metadata or media layout is outside the implemented subset. The reason is recorded. |
| `awaiting_deep` | The whole-payload pass finished, but the deep pass has not finished. Resume continues it. |
| `stale` | Evidence was added after the previous decision. Run recovery again. |
| `pending` | Indexed metadata has not yet been processed. |

**Matched is not a claim of historical identity.** Different recordings can share compatible codec parameters and sample layouts. Decoding cannot establish recording dates, camera identity, authenticity, or the absence of frames the camera never wrote. Do not infer a continuous timeline from file order. The tool preserves existing movie metadata but does not normalize dates or join unrelated clips.

## Resume, budgets, and large collections

Run the same command after interruption. Completed source scans and candidate attempts are reused. A source interrupted mid-scan restarts that source; scanning does not resume at a byte checkpoint. Deep scanning restarts its current target batch after interruption. Completed reconstructions remain cached.

The SQLite catalogue and source/range readers keep file descriptors bounded. Whole-payload queries use an indexed size range rather than testing every metadata/payload pair. The deep pass scans the inputs once per bounded target batch, not once per recording. It is still I/O-intensive; no performance claim has been established for your tens-of-thousands-file collection.

Defaults are 512 `mdat` candidates per record, 1 MiB of permitted trailing payload padding, 200,000 deep extent targets per batch, 32 different hits per extent, 64 complete assemblies per record, 2,000,000 target checks per deep batch, 300 seconds per validation subprocess, and 16 GiB per output candidate. Metadata is capped at 64 MiB and two million samples per track. Deep target batches also bound retained raw metadata at approximately 32 MiB; a single supported movie may exceed this batch threshold.

Inspect a `search_limited` reason before increasing its specific budget. Example with a larger matching/assembly budget:

```bash
python3 videorebuild.pyz recover --case "$CASE" --deep \
  --max-candidates 4096 --max-hits 64 --max-assemblies 512
```

Changing search settings invalidates cached decisions. `--retry` reruns nonmatched decisions under unchanged settings. `--limit 50` processes the next eligible batch; it is not a filter by recording date.

There is no total output-disk quota. Per-candidate free-space checks stop on insufficient storage. Failed candidate media is removed; successful ambiguous alternatives are kept. Detailed attempts and provenance can grow substantially. Do not delete successful candidates from an active case to save space, because resume integrity checks will stop.

## Install as a normal package (optional)

The zipapp requires no package installation. For development or an installed `videorebuild` command:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-build.txt
python -m pip install --no-build-isolation --no-deps .
videorebuild doctor
```

The only Python build dependency is pinned setuptools. There are no third-party Python runtime dependencies. Standard-library parsing is used only for the narrow offset/sample-table operations; mature FFmpeg tools perform the actual media decoding.

## Test and rebuild

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 tools/build_zipapp.py
python3 videorebuild.pyz doctor
```

Tests generate small test-pattern video/AAC recordings locally using `libx264` and `libx265`; no sample-video download is needed. They deliberately separate metadata and media, insert wrong/duplicate payloads, scramble multiple extents and recordings, drop extents, mutate metadata, cross scanner buffer boundaries, and exercise interruption and evidence changes. A GitHub Actions workflow is supplied, but no remote CI execution is claimed.

See [TEST_REPORT.md](TEST_REPORT.md) for the actual executed release checks, [architecture](docs/ARCHITECTURE.md), [support boundaries](docs/SUPPORT.md), and [format references](docs/REFERENCES.md).
