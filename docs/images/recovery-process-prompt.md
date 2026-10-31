# Recovery overview graphic

Generated with the built-in image-generation tool; no source media or private paths supplied. Final image: recovery-process.png. It summarizes conceptual strategy panels, not parallel execution. README Mermaid and prose provide the detailed gates and ownership limitations.

## Initial prompt

Use case: infographic-diagram
Asset type: repository README technical overview, landscape high-resolution raster.
Create a polished, exceptionally readable engineering infographic titled "VideoRebuild: from evidence to reconstructed clips". White/off-white background, dark navy type, restrained blue and teal process boxes, amber risk/performance callouts, thin clear arrows. Large labels, plenty of whitespace, no photography, no decorative charts. Precise text and arrows matter.
Layout: top horizontal intake band, middle two clearly separated strategy panels side by side, bottom shared validation/output band. Note strategy panels explain separate models; actual execution is ordinary first, fragmented second, not parallel.
Top three connected boxes with exact labels:
"Read-only evidence" / small line "Recovered files • images • mapped extents"
"Bounded indexing" / small line "Source hashes • MP4 boxes • surviving metadata"
"Eligible readable ranges" / small line "Identity + acquisition + optional allocation policy"
Middle left panel heading "1 · Ordinary MP4". Vertical flow:
"Surviving moov sample tables"
"Try original offsets + whole mdat translations"
Decision "No accepted candidate + deep enabled?"
Arrow yes to "Find extent anchors → verify all samples"
then "Order by sample tables; reject overlapping source bytes"
No branch goes directly to shared candidate verification; yes branch also goes there. Small caption "Keep feasible alternatives; byte-distinct results may remain ambiguous."
Middle right panel heading "2 · Fragmented MP4". Vertical flow:
"Initialization: ftyp + moov + mvex"
"Try initialization end + indexed moof starts"
"Check IDR starts, sequence +1 and per-track timing"
"Keep prefix or separate later sections"
Small caption "Never bridge unavailable gaps or guess missing timing."
Bottom shared band:
"Copy original encoded bytes" -> "Rebuild container offsets where required" -> "Publish scoped results + evidence"
Below bottomband exact sentence: "Default: strict codec checks. --reconstruct-only: no decoding; decode_checked=false."
Two compact amber annotation cards at bottom:
"Risks" with lines "Missing metadata or bytes • wrong association • unsupported layouts • ambiguity"
"Cost centres" with lines "Candidate count • deep scan • feasible combinations • SQLite + temporary storage"
Footer exact sentence: "A structural match does not prove camera identity, chronology or complete historical footage."
Do not imply AI guesses frames, filename matching, transcodes, brute-force sector permutations, GPU acceleration, guaranteed recovery, archive comparison inside recovery, or automatic gap joining. Avoid extra text or statistics.

## Refinement

Move the strategy branch to the Eligible readable ranges box so policy visibly gates both strategies. Add: Strategy panels show separate models; execution runs ordinary first, then fragmented. Preserve all other labels and layout.

## Tiering and sidecar update

Change the range gate label to "Permitted ranges + candidate tiers" and explain Tier 1 first, with `--include-recovered` enabling Tier 2 fallback. Change the publication label to "Publish scoped clips + ordered JSON". Add that JSON records source ranges, assembly order, output offsets and hashes. Add that recovered references move to Tier 2 while source files remain unchanged, and tiering is a search heuristic. Preserve source-safety, validation-mode, sequence and historical-identity limitations.
