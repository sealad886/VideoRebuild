# VideoRebuild 0.5.0

Pending release, assessed from published v0.4.0 at
`ee46785efa8df7adab7fc19bc7e755e785f70ef1`. The new discovery-worker option and
durable structural-index behavior select a minor increment under the project's
pre-1.0 policy. This file does not authorize publication.

- Add reusable, scope-qualified structural postings and selective required-anchor
  matching, preserving canonical source/range validation and ambiguity handling.
- Share cold whole-payload structural checks by known payload origin and required
  relative offset, retaining all-pairs fallback for unsupported or capped models.
- Fix short AVC discovery at a permitted-range boundary and reduce repeated
  mapping-key, range and NAL-header work.
- Add bounded sparse physical-support pruning without dropping alternate
  witnesses or replacing global overlap checks.
- Read fragmented sections incrementally and reuse bounded parsed-pair facts.
- Maintain recovered-reference intervals and normalized rows per changed owner;
  preserve a local backup of the former derived JSON cache before conversion.
- Expose `--search-workers 1..8` in CLI and TUI, default 1, with independent readers
  and deterministic coordinator writes.

Existing source/container indexes and media remain intact. New matching revision
invalidates obsolete search conclusions; structural-index coverage remains tied
to source, mapping, allocation/acquisition and parser identity. Reconstruct-only
outputs continue to report `decode_checked=false`; second-tier fallback remains
opt-in. Existing running processes keep their frozen runtime until explicitly
restarted by the operator.
