# VideoRebuild 0.6.0 (pending)

This pending release adds measured phase progress bars to the read-only TUI
watch screen. This additive feature selects a minor increment under the
project's pre-1.0 release policy. No tag or release is created by preparation.

- Show actual completed/total counts and units for reported phases.
- Keep unknown, empty or inconsistent totals indeterminate.
- Retain observed work and outcomes for interrupted, failed and limited phases.
- Mark stale and historical observations, suppressing their ETA.
- Label fresh byte-phase ETAs as estimates based on the phase-average rate.
- Keep overall recovery completion and ETA unknown; historical job counts
  never provide an overall percentage.
- Bound observation to recent logs and events without reading attempt history
  or media. Closing watch mode leaves recovery running.

Existing case data and log formats require no migration. The viewer can observe
an already running recovery without restarting it. Recovery matching, source
reads, validation and second-tier defaults are unchanged by this TUI update.
Existing logs have no unique phase IDs; the viewer reports only phase evidence
available in its bounded log tail and does not invent missing parent totals.

The separate Rust migration is not included in this change.

Recovery resume also gains targeted restoration of missing generated files:

- Restore absent ordinary candidates/publications and fragmented outputs from
  verified case copies or retained source provenance, requiring the accepted hash.
- Recreate missing fragmented manifests and validation documents from committed
  proof without changing their recorded decode-validation scope.
- Preserve source checks, accepted job decisions and durable search progress;
  changed surviving artifacts and insufficient reconstruction proof remain errors.
- Retain recovery error messages in the case log after report refresh.

These fixes remain part of pending 0.6.0; they do not publish a new release.

Recovered-video comparison also becomes available through `videorebuild compare`:

- Recursively compare a recovered folder against a transferred-video archive,
  keeping both input trees read-only and reports in a separate output folder.
- Recognize identical encoded video in different MP4 containers, including a
  complete recovered clip contained in a longer archived recording.
- Keep unmatched, unreadable and uncertain visual candidates in `missing.csv`.
  Optional decoded-image suggestions require review and never establish absence.
- Reuse a local metadata cache on subsequent runs and report comparison errors
  separately from missing candidates. Video matching does not establish audio
  equivalence, original recording completeness or recovery provenance.

See [comparison usage and evidence limits](COMPARISON.md). This additive command
remains part of the pending 0.6.0 minor release; publication requires separate
authorization and release verification.
