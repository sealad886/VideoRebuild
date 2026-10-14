# Changelog

## 0.2.0 — 2026-10-02

Added a separate report-anchored image-first fragmented-MP4 backend. Supports empty initialization sample tables, moof/traf/tfhd/trun, optional tfdt, track-specific cumulative timing, exact sample-byte validation, and independent per-offset recording jobs despite identical headers. Added bounded blocked-source windows, explicit decoded-prefix results, durable per-location resume, a diagnostic ZIP inspector, and a Terminal launcher.

The generic `run-macos.command` now forwards arguments to the new fragmented backend; the previous GUI is retained as `run-legacy-macos.command`. Existing `run`/`recover` commands keep their sample-table semantics.

No generic filesystem-fragment reordering, camera-identity inference, complete-image success claim, or macOS execution claim is introduced. See TEST_REPORT.md for executed evidence.



## 0.1.0 — September 29, 2026

Initial local reconstruction release: read-only source/image scanning, bounded MP4 sample-table parsing, whole-payload matching, scattered-extent search, 64-bit chunk-offset rewriting, strict full-media validation, ambiguity and budget reporting, SQLite resume state, byte-range provenance, local reports, macOS launcher, standalone zipapp, conventional wheel packaging, and a generated-media regression suite.

Support is intentionally restricted to the layouts in `docs/SUPPORT.md`. This is an initial experimentally validated release, not a claim of arbitrary-camera or production forensic coverage.
