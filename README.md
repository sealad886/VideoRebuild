# VideoRebuild 0.2.0

Local, MIT-licensed video recovery for macOS/Linux. Python 3.11+ and both FFmpeg and ffprobe 8+ are required. The included `videorebuild.pyz` needs no Python package installation. There are no network calls in the recovery code.

## The new path: fragmented MP4 from original-image bytes

`recover-fragmented` addresses PhotoRec output consisting of repeated `ftyp`/`moov` initialization headers and separate `mdat` pieces. It imports `report.xml`, verifies header bytes against the original image, and follows an uninterrupted sequence of `moof`/`mdat` pairs. Headers with identical content remain separate source-location jobs. This is not arbitrary filesystem-fragment reordering.

Accepted prefixes are copied without re-encoding, checked against exact encoded-packet SHA-256 hashes, counted, and fully decoded. Published files are labeled **decoded prefixes**, never guaranteed complete original recordings. Gaps, truncation, missing reference pictures, unsupported layouts, and missing tracks are not concealed.

Read [the fragmented-backend contract](docs/FRAGMENTED.md) before a full run. It includes support, failure states, resource bounds, and resume semantics.

## Start

```bash
python3 videorebuild.pyz doctor
python3 videorebuild.pyz recover-fragmented --help
```

On a Mac, install the runtime yourself with `brew install python@3.13 ffmpeg`. `run-fragmented-macos.command` locates installed Homebrew `python@3.13` and unversioned `ffmpeg` tools when present, resolving Homebrew aliases through `brew --prefix ffmpeg`, then runs the fragmented backend with your arguments. It retains the first complete FFmpeg/ffprobe pair accepted by `doctor`, checking `/opt/homebrew` before `/usr/local`; a later installation cannot override a supported pair. Unsupported or incomplete Homebrew pairs leave the original PATH fallback intact. Both `ffmpeg` and `ffprobe` must be version 8 or newer; `doctor` checks each executable. The launcher never installs or updates dependencies silently.

```bash
bash ./run-fragmented-macos.command --help
```

For an interactive Terminal prompt without hardcoded paths:

```bash
bash ./recover-interactive.command
```

The interactive script runs a 50-location pilot. A case directory must be new/empty and outside the recovered-source tree. The image must be the regular, uncompressed raw image represented by the PhotoRec report; a device node, cropped substitute, different image size, or guessed offset rebasing is rejected.

Use the same arguments with `--all` rather than `--limit 50` to process remaining locations. Do not delete or rename PhotoRec inputs. Re-running a pilot processes up to 50 additional pending locations.

## Outputs

- `recovered/`: exact contiguous MP4 byte prefixes passing every validation gate.
- `attempts/`: candidate MP4s, precise source ranges, hashes, and detailed validation logs. These include failed candidates; do not treat them as recovered videos.
- `reports/index.html`: local summary and latest 100 outcomes.
- `reports/summary.json`: aggregate results, reasons, pending counts, and tool versions.
- `reports/results.jsonl`: streaming index of every processed location.
- `blocked-windows/`: by default, up to four 1 MiB original-image windows around blocked locations, plus offsets and hashes. These can contain private video/audio; inspect before sharing. Disable with `--diagnostic-windows 0`.
- `fragmented.sqlite3`, `fragmented-case.json`, `fragmented.log`: resume catalogue, source binding, and progress log.

Exit codes: 0 means the case contains at least one decoded prefix; 3 means no decoded prefix was found; 2 means a safety/configuration/input error; 130 means interrupted. Code 0 does not mean every location succeeded. The report is authoritative.

## Other recovery models

The original ordinary-MP4 sample-table and scattered-extent backend remains available under `scan`, `run`, `recover`, and `report`. Its old guided launcher is `run-legacy-macos.command`. It is not the appropriate path for empty fragmented-MP4 initialization tables. See [legacy documentation](docs/LEGACY.md) and [support boundaries](docs/SUPPORT.md).

## Reproduce analysis of an Intake ZIP

```bash
python3 tools/inspect_diagnostic.py --help
```

This tool verifies sample hashes, parses initialization data, inventories embedded fragments independently of enclosing carve lengths, and reports PhotoRec source discontinuities. It does not extract or upload binary media. An existing output report is not overwritten.

## Tests and packaging

Run from the project root with both FFmpeg and ffprobe 8+ available:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 tools/build_zipapp.py
python3 videorebuild.pyz --version
```

[`TEST_REPORT.md`](TEST_REPORT.md) records executed validation. The October 16 accuracy audit passed all 118 tests on macOS/Apple Silicon with Python 3.11 and 3.13 source checkouts and the Python 3.13 standalone archive. Installed-wheel execution covered an earlier 102-test snapshot. Nine sampled real recovered prefixes passed fresh original-image byte comparisons, packet hashes, timing checks, and full decoding; this does not validate the entire recovery image or establish complete historical recordings. Synthetic fixtures are generated locally, not downloaded, and no private surveillance-media fixture is bundled. See the [accuracy audit](docs/AUDIT_ACCURACY.md) for scope and limits.

The standalone archive has no Python dependencies. The optional wheel build uses the pinned build requirement in `pyproject.toml`; it is not needed to run the zipapp. FFmpeg performs codec decoding; the Python code implements the narrow recovery/addressing rules.

To reproduce source and wheel packaging in an isolated project environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-build.txt
.venv/bin/python tools/build_zipapp.py
.venv/bin/python -c 'from setuptools.build_meta import build_sdist; build_sdist("dist")'
.venv/bin/python -m pip wheel --no-build-isolation --no-deps dist/*.tar.gz --wheel-dir dist
```

Use Python 3.11+ for these commands. The source archive includes all four Mac launchers; CI checks their syntax, starts the extracted fragmented launcher, and installs a wheel built from that archive. Local artifacts retain version 0.2.0; validation does not imply a new published release.
