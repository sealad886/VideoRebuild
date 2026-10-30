# Explicit case migration

Version 0.3.0 replaces the split ordinary/fragmented case lifecycles with one `case.sqlite3` catalogue and typed jobs. `recover-fragmented`, its `--root` contract and split macOS launchers are removed. Use canonical `run --input RECOVERED --image IMAGE --report REPORT --case CASE` for PhotoRec workflows; files-only `run --input RECOVERED --case CASE` requires no image/report.

Run `migrate-case --case PATH` on an old case before resuming it. Conversion acquires the applicable case lock, backs up the original SQLite database through SQLite's backup API (including committed WAL contents), validates source/output evidence, and installs a converted database only after validation. Ambiguous mixed case layouts are rejected. Original outputs and validation evidence are preserved.

Migration preserves historical claims under their original validation version. It does not relabel old bytes as rescued, infer missing source mappings or upgrade old validation to new guarantees. Follow the migration result and create a fresh case when new acquisition metadata/settings cannot be bound safely.

Keep the reported backup until conversion and the intended recovery workflow have been independently checked. A failed conversion must leave the original case usable. To use prior code, use the original/backup case separately; do not open a converted schema with old code. No automatic deletion of historical evidence or backups occurs.

Earlier audit documents and TEST_REPORT.md entries describe their dated code/tool snapshots. Their old commands and paths are historical evidence, not current usage instructions.

Migration makes historical reports available immediately. For fresh fragmented recovery, run canonical intake with the image and any recovered/report evidence after conversion; an old image registration is deliberately not marked as newly scanned. Forced process termination during the short final database-installation interval can require restoration from the complete retained migration backup. Ordinary exceptions restore the original database filenames automatically. No private case was used to qualify migration.
