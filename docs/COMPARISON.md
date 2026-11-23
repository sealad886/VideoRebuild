# Comparing recovered and transferred videos

`videorebuild compare` reports videos from a recovered folder **A** that do not have a confirmed video-content match in another folder **B**. Both folders are searched recursively. Names, dates and container bytes are insufficient evidence by themselves.

```bash
videorebuild compare \
  --recovered /recovery/videos \
  --archive /transferred/videos \
  --output /healthy/comparison
```

Choose separate, non-overlapping recovered and archive directories. A folder cannot serve as its own archive. The output directory must be outside both inputs. The command reads source videos without moving, renaming, deleting or changing them. `.DS_Store` is ignored. Run against stable recovered outputs and an archive that is not being modified.

Discovery includes `.mp4`, `.mov`, `.m4v`, `.mkv`, `.avi`, `.webm`, `.mts`, `.m2ts`, `.ts`, `.mpeg`, `.mpg`, `.3gp` and `.wmv`, regardless of letter case. Files with other extensions are outside the inventory. Symlink files and directories are skipped; input roots and output path components must not be symlinks.

Encoded comparison supports the repository's ordinary and fragmented MP4 layouts, including supported AVC/HEVC sample tables. Other formats can match by complete file bytes or produce optional visual suggestions, but their encoded-content comparison remains unsupported and is reported as an error. Malformed or unsupported MP4 layouts also remain explicit. A fragmented `moof`/`mdat` pair larger than 64 MiB exceeds the comparison buffer limit.

To compare a recovery case's published videos, point at its `reconstructed` directory:

```bash
videorebuild compare \
  --recovered /recovery/case/reconstructed \
  --archive /transferred/videos \
  --output /healthy/comparison
```

Use the `reconstructed` directory for canonical publications. Pointing at an entire recovery case can include unpublished candidate copies and diagnostic videos from `work`; the comparison treats every discovered video as an input. A comparison does not inspect the recovery database or decide which candidate was published. An empty recovered folder produces no video rows; that does not establish recovery success.

From a source checkout, use the project's environment:

```bash
PYTHONPATH=src .venv/bin/python -m videorebuild compare \
  --recovered /recovery/videos \
  --archive /transferred/videos \
  --output /healthy/comparison
```

## Reports and decisions

| File | Purpose |
| --- | --- |
| `missing.csv` | Every recovered video without a confirmed match, including uncertain visual candidates and unreadable files. Start here. |
| `comparison.csv` | All recovered-video results and their match evidence. |
| `comparison.json` | Structured results, counts, errors and comparison scope. |
| `summary.md` | Human-readable counts and limitations. |

The result categories separate the evidence:

| Status | Meaning |
| --- | --- |
| `represented_exact` | The recovered file is byte-identical to a file in B. |
| `represented_encoded` | Every recovered video sample is present in order in B, with compatible codec configuration. Container bytes may differ. |
| `visual_candidate` | Sampled decoded images look similar; manual review is required. This video remains in `missing.csv`. |
| `no_match` | No confirmed match or visual candidate was found by the enabled methods. This remains a missing candidate. |
| `unreadable` | The recovered file could not be compared by the supported methods. It remains unresolved and in `missing.csv`. |

Encoded matching can recognize a complete recovered video inside a longer B video, including a recovered prefix or later segment. A short B clip inside a longer A clip does not establish that all of A is represented. The comparison concerns video samples; audio equivalence and precise playback-timing equivalence are outside its scope.

`no_match` does not prove absence. Re-encoding changes encoded samples; unsupported containers, damaged media or unreadable parts of B can also prevent a match. Check `comparison.json`'s `summary.complete` and `errors` array before treating the report as a complete search. Archive failures are recorded there even when a recovered video has a confirmed match elsewhere. No status is permission to delete recovered files.

## Re-encoded videos

Enable optional visual suggestions when the archive may have been re-encoded:

```bash
videorebuild compare \
  --recovered /recovery/videos \
  --archive /transferred/videos \
  --output /healthy/comparison \
  --visual --timeout 60
```

This mode needs FFmpeg on `PATH` and opens local files in the video formats listed above. It samples the first video track once per second, for up to the first 300 seconds, and compares temporal sequences of small grayscale images. Up to five visual candidates are reported per recovered video, with their scores, offsets and limitations. Recordings with fewer than three sampled frames or insufficient temporal variation do not produce suggestions. A video that exceeds the sampling limit makes visual coverage incomplete.

Suggestions remain unresolved because similar fixed-camera scenes, dark frames and repeated events can resemble each other. Sampling can miss differences or copies with edits, crops, overlays, altered timing or material beyond the sampled interval. It does not establish audio identity or validate that a recovered recording is complete.

Start with encoded matching for a large archive. Visual analysis needs decoding work for archive videos and can take substantially longer. Add `--visual` to the same output directory later when you want that additional evidence.

## Repeated runs and exit status

The output directory contains a reusable comparison cache. Re-run the same command after interruption or when new recovered videos become available. Cached source metadata is checked against the current file identity and timestamps; confirmed matches are verified against current source bytes. Keep the output directory to reuse indexing work. Concurrent comparisons must use separate output directories. Final reports describe the completed invocation, so check them after the command exits.

Each report records its start and finish times and the input snapshot it compared.
`summary.recovered_snapshot` and `summary.archive_snapshot` list videos added,
removed or changed during the run. Detected changes make the comparison
incomplete; a represented result whose source changed returns to the attention
list. Re-run after new recovery exports or transfers arrive.

Progress is written to standard error. Supply `--no-progress` to suppress it. The command prints the summary and report locations on standard output.

| Exit status | Meaning |
| --- | --- |
| `0` | Comparison completed under its stated method and format scope. Missing candidates can still exist. |
| `1` | Reports were produced, but comparison errors left the search incomplete. |
| `2` | Invalid arguments, unsafe output placement or another operation failure. |
| `130` | Interrupted; re-run to reuse completed cache work. |

The comparison neither starts recovery nor changes a recovery case. Recovery manifests retain their original validation and completeness limits regardless of whether a video-content match is found.
