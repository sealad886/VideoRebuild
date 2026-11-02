# VideoRebuild 0.4.0

This release adds tiered recovery, a terminal interface and case-local search
storage while retaining a dependency-free Python runtime.

## Recovery behavior and migration

Recovered physical ranges move out of the first candidate tier. Second-tier
fallback is disabled by default; opt in with `--include-recovered` when no
first-tier match is found. A first-tier cutoff cannot establish absence or
uniqueness and does not trigger fallback until that search is complete.
Existing indexed sources and published media remain preserved. Changed matching
and validation scopes can cause existing jobs to be reconsidered without
rebuilding the source index. Historical manifests retain their stated limits.

Ordered reconstruction manifests record source extents and output order.
Fragmented retries reconsider uncovered failed or limited starts and changed
validation modes; existing fragmented sidecars are preserved when new evidence
requires another manifest. Reconstruction-only exports retain
`decode_checked=false` and make no playback or decoder-quality claim.

Saved source validation tolerates remounted device numbers while enforcing saved
size, modification time and inode. Open readers pin the current descriptor and
path identity. Finder `.DS_Store` metadata is excluded from discovery and
recovery. Image reconstruction remains constrained by any applied allocation
and acquisition maps.

## Case-local temporary storage

CLI and TUI recovery now create private search databases under `CASE/work`,
independent of system temporary-directory settings. Search cardinality and the
persistent index remain on disk. Auxiliary SQLite temporary sorts use
connection-local memory rather than spilling to the system temporary volume.
These sorts have no separate total-memory quota. Search directories are cleaned
on normal and handled-error exits; forced termination can leave case-local
scratch directories. An unavailable or unsafe work directory causes an error
rather than a fallback to the system disk.

## Interface, packaging and performance

The installed `videorebuild` command includes a PhotoRec-style filesystem browser,
foreground recovery and a read-only case watcher. Pip, pipx and uv installation
are supported; a standalone zipapp remains available.

Candidate screening, extent lookup, bounded manifest backfill, scan-window
reuse and report serialization reduce redundant work. No workload-independent
throughput claim is made.

Wheel, source distribution and zipapp versions derive from one canonical source.
Release artifacts are verified and checksummed before GitHub publication. The
MIT license grant is unchanged; its copyright attribution names Andrew M. Cox.
