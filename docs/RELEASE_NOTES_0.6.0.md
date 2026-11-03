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
