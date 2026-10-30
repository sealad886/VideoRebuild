# VideoRebuild

VideoRebuild reconstructs supported AVC/HEVC MP4 video from recovered files and optional original disk-image evidence. It runs locally with Python 3.13+, FFmpeg and ffprobe 8+, and no third-party Python runtime dependencies.

Version 0.3.0 uses one case and byte-range model for ordinary and fragmented MP4. Logical file bytes may come from multiple noncontiguous image extents. Original inputs remain read-only; output belongs on healthy storage outside every input tree.

A successful decode does not prove original recording identity or completeness. Reports distinguish acquisition status, association evidence and media validation. Missing bytes remain missing; later recoverable sections are separate clips.

## Run

Check tools, then recover supplied files:

```bash
python3 videorebuild.pyz doctor
python3 videorebuild.pyz run --input /recovery/files --case /healthy/case
```

For PhotoRec headers and the original raw image:

```bash
python3 videorebuild.pyz run \
  --input /recovery/photorec \
  --image /images/disk.img \
  --report /recovery/photorec/recup_dir.1/report.xml \
  --mapfile /images/disk.map \
  --case /healthy/case
```

The PhotoRec report is optional. When supplied, small recovered MP4 headers must match their reported image bytes exactly. Image size must match reported geometry. Initialization locations remain separate jobs even if codec headers are identical. Image indexing is a sequential bounded scan, not a full-image allocation in memory.

For filesystem-guided reconstruction, produce a DFXML report with the installed Sleuth Kit (qualified locally with fiwalk 4.14.0), then import its raw byte runs:

```bash
fiwalk -x -z -g -G0 /images/disk.img > /healthy/filesystem.xml
python3 videorebuild.pyz run \
  --image /images/disk.img --extents /healthy/filesystem.xml \
  --mapfile /images/disk.map --case /healthy/case
```

Fiwalk is optional external tooling, not a Python dependency. Its exit code alone does not establish successful mapping: VideoRebuild validates the complete XML and each object's extent map. This path searches the mapped logical files; it does not separately scan the entire image for unlisted objects. Filesystem support depends on the metadata and external tool; no proprietary filesystem support is claimed.

A mapfile qualifies only mapped image bytes. `+` means reported rescued; other map states and uncovered regions are unavailable. Without a mapfile acquisition status is unknown. If acquisition used different input/output positions, supply `--map-offset DELTA`, where image position = mapfile position + DELTA. Never infer this transform from filenames or file contents.

## Progress and recovery scope

Commands and diagnostic tools report operation phases on stderr, including actual work counters, elapsed time, and throttled updates. Byte operations show throughput and an estimated remaining time when their total is known. Decoder waits report elapsed activity without an invented percentage. Finished, failed, and interrupted phases are distinct; finishing a phase does not mean every recording was recovered or historically identified. Progress is also recorded in a case’s `run.log`.

Use `--no-progress` to suppress progress feedback; warnings and errors remain visible. The option works before or after a CLI subcommand and on the diagnostic/build tools. Stdout result formats and JSON report schemas are unchanged. Progress uses newline records even when redirected, with no terminal escape animation.

For a PhotoRec run explicitly made in **Free / unallocated-space-only** mode, process its exported files as the working set:

```bash
python3 videorebuild.pyz run --input /recovery/photorec-free --case /healthy/free-space-case
```

This invocation does not scan an image and has no automatic image-scan fallback. The free-space provenance comes from the PhotoRec run’s selection, not from VideoRebuild inferring allocation from filenames. For an already indexed image case, constrain reconstruction with an independently established allocation map as described below; the existing index need not be rebuilt.

Supplying `--image` without `--extents` indexes available bytes across the image, including allocated space. A PhotoRec `--report` verifies header locations but does not restrict that scan to unallocated ranges. A ddrescue map records acquisition/readability, not filesystem allocation. Filesystem `--extents` follow the supplied logical-file mappings and likewise do not establish a free-space-only policy. Do not add these image paths to a free-space-only workflow without evidence that the accessed ranges are unallocated. PhotoRec documents its [unallocated-space-only selection](https://www.cgsecurity.org/testdisk_doc/photorec.html#carve-the-partition-or-unallocated-space-only).

An existing image index can be reused with an image-bound unallocated-range policy:

```bash
python3 videorebuild.pyz recover --case /healthy/case --deep --retry \
  --allocation-map /healthy/filesystem-unallocated.json
```

The policy is schema 1 JSON with `kind: "filesystem-unallocated"` and an `images` array. Each image entry binds its absolute `path`, `size` and previously indexed `sha256`, plus sorted, nonoverlapping `unallocated_ranges` expressed as `[image_byte_offset, length]`. Every indexed physical image must have an entry. Derive these ranges from the filesystem's allocation metadata, with the correct partition and cluster coordinates; gaps between files and ddrescue status are insufficient. The loader checks bindings and geometry, but cannot establish that a supplied bitmap is truthful. See [allocation scope](docs/ARCHITECTURE.md#filesystem-allocation-scope).

This policy gates image reads during reconstruction, including metadata evidence, cached attempts and fragment windows. It intersects acquisition availability without changing source/index rows. Regular recovered files remain eligible; their free-space provenance still comes from their original recovery selection. Historical publications remain historical and are not retrospectively certified by a new policy. Allocated files can serve as independently extracted controls for reconstruction testing; this does not assume damaged filesystem metadata or authorize recovering allocated footage.

## Cases, limits and reports

`scan` performs intake/indexing only. `recover --case …` resumes an indexed case. `report --case …` refreshes local JSON, CSV and HTML reports. The main report is `CASE/reports/index.html`; `results.json` contains typed jobs and provenance. See `--help` for complete options.

```bash
python3 videorebuild.pyz run --input /recovery/files --case /healthy/case --limit 50
python3 videorebuild.pyz recover --case /healthy/case --limit 50
python3 videorebuild.pyz report --case /healthy/case
```

Ordinary MP4 matching can use `--deep` for scattered-sample searches. Candidate, trailing-padding, hit, assembly, anchor-check and fragmented-start cutoffs are disabled by default. Explicit `--max-candidates`, `--max-padding`, `--max-hits`, `--max-assemblies`, `--max-anchor-checks` and `--max-segments` remain optional search cutoffs; reaching one reports incomplete search rather than proving absence or uniqueness. A whole-payload cutoff does not prevent deep search when no valid candidate has been found. `--max-groups` controls batch capacity without omitting later recordings.

Search hypotheses use private temporary SQLite storage and streaming traversal rather than unbounded in-memory hit combinations. Exhaustive enumeration within the supported model can still take substantial time and temporary disk space. Fragmented recovery searches indexed starts up to the next initialization or source boundary; each section retains a 16-MiB buffer ceiling (`--max-record-bytes`, maximum 256 MiB). Parser/sample bounds, output-size ceilings, disk-space guards and per-process validation timeouts remain enforced. Recovery may write candidates and publications, so allow space beyond the final encoded media size. No total-case retention quota is provided.

Changing source identity, metadata/map binding, coordinate transform or validation settings can require a new case. Completed publications and their evidence are checked before reuse. Old cases require explicit backed-up conversion:

```bash
python3 videorebuild.pyz migrate-case --case /healthy/old-case
```

See [case migration](docs/LEGACY.md), [shared architecture](docs/ARCHITECTURE.md), [fragmented strategy](docs/FRAGMENTED.md), [ordinary format support](docs/SUPPORT.md) and [implementation/release plan](docs/IMPLEMENTATION_PLAN.md).

On macOS, `run-macos.command` selects an existing supported Homebrew/runtime pair and invokes canonical `run`. `recover-interactive.command` asks for input/output paths. Neither installs dependencies. The old split fragmented and legacy launchers were removed.

## Validation and development

Source execution uses the project environment:

```bash
PYTHONPATH=src .venv/bin/python -m videorebuild doctor
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/build_zipapp.py
.venv/bin/python videorebuild.pyz --version
```

Tests generate synthetic media locally; no private recordings are bundled. Recovery checks encoded packet bytes, counts, supported track layout, exact timing where the strategy supplies it, and strict full decoding. Source immutability, ambiguity, interruption/resume, malformed mappings, resource bounds and migration failures are regression requirements. Optional fiwalk integration tests need the installed tool and otherwise skip explicitly.

The zipapp builder overwrites `videorebuild.pyz`. Wheel/sdist builds use the pinned requirements in `requirements-build.txt` in a project-specific virtual environment. See [TEST_REPORT.md](TEST_REPORT.md) for executed evidence and limits; configured CI coverage is separate from checks that actually ran.

No missing-header inference, generated frames, automatic chronology, raw-device writes, source filesystem repairs, or automatic upload occurs. Private paths and diagnostic windows can reveal sensitive media information; review before sharing.
