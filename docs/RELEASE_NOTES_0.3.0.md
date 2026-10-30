# VideoRebuild 0.3.0

Reconstruct supported MP4 media through one case and shared byte-range model. Recovered files work independently; optional image, PhotoRec/fiwalk DFXML and GNU ddrescue maps provide additional provenance and logical ordering. Separate validated later sections can survive gaps without silently joining missing media.

## Changes

- One source catalogue, logical/physical reader, typed job ledger, resume and streamed report.
- Raw filesystem extent imports, including noncontiguous files and explicit unavailable gaps.
- Rescue-map enforcement and explicit image/map coordinate transformation.
- Exact encoded-sample, timing and strict decoding gates retained.
- Location-specific fragmented jobs and separate later IDR/tfdt clips.
- Explicit backup-first migration of old cases, preserving historical validation scope.
- Bounded scanner setup, indexed availability, streamed migration and charged fragment attempt/read budgets after independent simplification/performance review.

## Migration

Requires Python 3.13+ and FFmpeg/ffprobe 8+. Replace recover-fragmented/--root with canonical run --input/--image/--report. Old split launchers and automatic private diagnostic exports are removed. Existing cases require migrate-case; retain its verified backup. New acquisition settings can require a fresh case. See docs/LEGACY.md.

## Validation and limits

189 tests passed in source, standalone zipapp and isolated installed-wheel execution. Real fiwalk 4.14.0 on a generated partitioned FAT16 image preserved exact A/V payload and input hashes. Independent requested reviews closed confirmed findings. All four hosted Linux/macOS Python 3.13/3.14 test-and-package jobs passed: https://github.com/sealad886/VideoRebuild/actions/runs/41041942049. PR #5 merged into main at 7adb2a1602d21772fe76a29eb36ad72c4e750825; main is the repository default branch.

Missing-initialization inference, proprietary filesystem parsing, guessed chronology, generated missing frames, complete original recordings and original-image recovery yield are not claimed. Later sections need independent IDR starts and explicit per-track restart timing. No total-case disk quota. Forced termination at final migration installation can require restoration from its retained complete backup.

## Artifacts

Release assets: deterministic videorebuild.pyz, videorebuild-0.3.0.tar.gz, videorebuild-0.3.0-py3-none-any.whl and SHA256SUMS.txt. Assets were built from the reviewed source; SHA256SUMS.txt verifies all three archives. No PyPI publication or deployment was performed.

Published: https://github.com/sealad886/VideoRebuild/releases/tag/v0.3.0. Downloaded assets passed checksum parity and zipapp version/doctor checks. Merged task/audit branches were removed locally and remotely; main remains the default branch.
