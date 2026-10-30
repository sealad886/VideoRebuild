# Changelog

## 0.3.0 — 2026-10-30

Unified ordinary and fragmented MP4 recovery under one case, source catalogue, byte reader, job ledger and report. Added explicit logical extent mappings from PhotoRec/fiwalk DFXML, GNU ddrescue acquisition-map enforcement with an explicit coordinate transform, and separately validated later fragmented sections after gaps. Files-only recovery uses the same workflow as image-backed recovery. Encoded media remains unchanged; acquisition, association and validation evidence are reported separately.

Breaking changes: `run`/`scan` accept `--report`, `--extents`, `--mapfile` and `--map-offset`; `recover-fragmented`, `--root`, automatic private diagnostic-window exports, and the split fragmented/legacy launchers are removed. Use `run --input … --image … --report …` for PhotoRec recovery. Old cases require explicit `migrate-case`; migration creates a verified local backup and preserves historical evidence without upgrading its guarantees. See docs/LEGACY.md.

Raw DFXML extents support filesystem-guided ordering; compressed and resident mappings are unsupported and sparse ranges remain unavailable. Later sections require valid independent video starts and explicit per-track restart timing. Missing-initialization inference, guessed chronology, proprietary filesystem parsing and reconstruction of absent encoded bytes remain unsupported. Test and release evidence is recorded in TEST_REPORT.md and docs/IMPLEMENTATION_PLAN.md.

## 0.2.0 — 2026-10-02

Added a separate report-anchored image-first fragmented-MP4 backend. Supports empty initialization sample tables, moof/traf/tfhd/trun, optional tfdt, track-specific cumulative timing, exact sample-byte validation, and independent per-offset recording jobs despite identical headers. Added bounded blocked-source windows, explicit decoded-prefix results, durable per-location resume, a diagnostic ZIP inspector, and a Terminal launcher.

The generic `run-macos.command` now forwards arguments to the new fragmented backend; the previous GUI is retained as `run-legacy-macos.command`. Existing `run`/`recover` commands keep their sample-table semantics.

No generic filesystem-fragment reordering, camera-identity inference, complete-image success claim, or macOS execution claim is introduced. See TEST_REPORT.md for executed evidence.



## 0.1.0 — September 29, 2026

Initial local reconstruction release: read-only source/image scanning, bounded MP4 sample-table parsing, whole-payload matching, scattered-extent search, 64-bit chunk-offset rewriting, strict full-media validation, ambiguity and budget reporting, SQLite resume state, byte-range provenance, local reports, macOS launcher, standalone zipapp, conventional wheel packaging, and a generated-media regression suite.

Support is intentionally restricted to the layouts in `docs/SUPPORT.md`. This is an initial experimentally validated release, not a claim of arbitrary-camera or production forensic coverage.
