# Shared byte-range reconstruction architecture

## Canonical data path

Recovered files and optional image/map/DFXML → physical source catalogue → logical extent maps → streaming atom discovery → ordinary or fragmented reconstruction plan → packet/timing/full-decode validation → publication and typed job evidence.

`storage.Case` owns one SQLite database (`case.sqlite3`), lock, source catalogue, settings bindings, jobs, attempts and events. `ReaderPool` is the canonical recovery read path. It resolves logical offsets through `ranges.Extent` to checked regular physical sources, overlays acquisition maps and rejects unavailable ranges. Descriptor count is bounded. No missing range is zero-filled or removed to shift subsequent offsets.

`workflow.py` imports inputs, verifies PhotoRec headers, stages DFXML imports transactionally, and invokes the separate format strategies. `reports.py` streams case reports without collecting every job in memory. `cli.py` owns command dispatch only. The former independent fragmented database, lock and resume/report runner are removed.

## Extents and acquisition

An extent records logical offset, physical source ID, physical offset and length. Mappings are sorted by logical position and validated for overlap, source-range reuse and bounds. Lookup is indexed with binary search; work is proportional to the intersecting extents. Gaps remain unavailable.

GNU ddrescue maps use explicit image = map position + offset translation. Only `+` is consumable with a map supplied; failed/untried/intermediate/uncovered ranges are unavailable. Files without mapped acquisition evidence are unknown. Mapfiles are evidence supplied by the operator, not cryptographic proof of original acquisition. Zero-valued bytes never establish rescue status.

`ranges.iter_dfxml` streams PhotoRec/fiwalk XML with limits on bytes, depth, fileobjects, runs and captured text. DTD/entities are forbidden. Raw `file_offset`/`offset`, `img_offset` and `len` values are checked against logical and image bounds. Fiwalk image offsets already include partition offsets. Sector padding is clipped to logical size. Sparse fills remain holes; compressed/resident data is unsupported. Filenames are labels, never export paths. A malformed trailing XML document rolls back staged mappings.

## Format specialists

`bmff.py` retains ordinary MP4 sample-table parsing and offset rewriting. `engine.py` retains whole-payload and bounded scattered-extent hypotheses through the shared reader. Complete usable sample tables remain required; explicit filesystem maps can make physically split metadata/chunks logically readable. All encoded samples are preserved; reconstructed container metadata can differ from the original.

`fragmented.py` retains narrow initialization, moof/traf/tfhd/trun and NAL validation. `image_recovery.py` discovers initialization locations in the shared catalogue and creates location-specific jobs. Equal headers remain separate locations. It validates prefixes and separately assesses later independent starts within a bounded logical window. See FRAGMENTED.md.

FFmpeg remains the codec implementation. Independent packet hashes, exact timing and strict decoding validate candidates; they do not prove recording association. Acquisition status, association evidence and validation are separate manifest fields. Ordinary `matched`, fragmented `decoded_prefix`/segment outcomes, blocked/unsupported states and search limits retain their scope.

## Safety, state and migration

Inputs are regular read-only files. Case roots cannot contain inputs or overlap recovered-file trees. Metadata and source identities are bound to a case; consumed byte ranges carry hashes and provenance. Identity checks detect ordinary changes, not an adversarial administrator preserving inode/time metadata. The tool is not a hardware write blocker.

One case lock prevents concurrent writers. Outputs and evidence are verified before reuse. Interrupted uncommitted jobs are retried; conflicting existing publications stop recovery. Reports use private atomic writers; no external upload or source linking is performed. Case storage has no total quota; output/temp capacity remains an operator requirement.

Older schema/case layouts require explicit migration with a verified local SQLite backup (including WAL state). Historical outcomes preserve their original validation scope and do not acquire mapfile/extent guarantees. See LEGACY.md. Rollback uses the prior code with its original or backed-up case, never a new schema with old code.

## Evidence and limitations

Tests use generated media and disposable disk images, including noncontiguous mappings and unavailable ranges. Performance checks must distinguish interval lookup, mapping import, scan, validation subprocess and report costs. Passing synthetic fixtures does not establish original-image yield, all filesystem support, historical identity or wall-clock chronology. TEST_REPORT.md and the implementation plan record actual checks and unresolved gates.
