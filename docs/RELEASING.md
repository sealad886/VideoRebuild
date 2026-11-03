# Release ownership and procedure

VideoRebuild has one release unit: the `videorebuild` Python CLI application in
`src/videorebuild`, distributed together as a wheel, source distribution and
standalone `videorebuild.pyz`. The distribution identity is `videorebuild`; the
publication destination is the GitHub Releases page for
`sealad886/VideoRebuild`. PyPI publication is not configured.

## Canonical owner and version source

The authorized agent owns version assessment and writes exactly one version
literal: `src/videorebuild/__init__.py::__version__`. Setuptools 84.1.0 reads that
literal through `[tool.setuptools.dynamic].version`; wheel and sdist metadata,
runtime displays, reports and zipapp code derive from it. Build tools and CI
validate versions but never bump them. Do not introduce Release Please,
Changesets, tag-derived versioning or a second editable version field for this
unit. No separate version command is needed: edit the canonical literal in a
reviewed change, then run native package builds and the checks below.

The supported consumer contract includes CLI arguments and exit behavior,
recovery defaults, case/report/manifest formats and migration behavior,
source immutability, encoded-byte preservation, reconstruction/validation
scope, and supported Python/platform requirements. Assess the cumulative
behavioral diff from the last published baseline; commit labels alone do not
determine compatibility.

The verified current baseline is GitHub release `v0.5.0`, published
2 November 2026 at commit `e257562a0194cec863bb89eb74d76a2c0847f844`.
The next pending version is 0.6.0: measured phase progress bars extend the TUI
under the pre-1.0 feature policy. See `RELEASE_NOTES_0.6.0.md`. This preparation
does not include the separately developed Rust migration. Publication requires
the checks and authorization below.

While below 1.0, features and intentional breaking changes increment the minor
component; compatible fixes increment patch. Compute a pending version from
the immutable released baseline, rather than repeatedly incrementing an
already pending number. Internal changes alone need no release. Declaring 1.0
stability requires an explicit decision.

This workflow supports only stable, normalized `X.Y.Z` versions and `vX.Y.Z`
tags, identical in Python metadata and GitHub. Prerelease, preview, maintenance
and promotion channels require an explicit policy and workflow change first.
Never move a released tag or replace published bytes with different content.

## Review, authority and hosted prerequisites

Prepare a focused PR with the assessed version, behavior/migration notes in
`docs/RELEASE_NOTES_X.Y.Z.md`, package checks and the supported CI matrix.
Resolve review findings and required checks before merging. The reviewed source
SHA on `main` is the release identity; use its full 40-character commit SHA.

Agent ownership grants responsibility for assessment and preparation. It does
not grant publication authority. The setup request authorizes its own commit,
push, PR, remediation, merge and task-branch cleanup; it does not authorize a
new tag, release dispatch, GitHub publication, PyPI publication or repository
settings changes. There is no standing publication grant. Obtain explicit
authorization for the exact version, SHA, stable channel and GitHub release
effects before dispatching. A future standing grant must name repository,
branch, channel, artifacts and allowed effects, and can be revoked by the owner.
Credentials, a merged workflow and an environment name are not a grant.

Before the first authorized release, a repository administrator must configure
and verify:

- `main` protections and required `test-and-package` checks for the Linux/macOS,
  Python 3.13/3.14 matrix, plus the agreed PR review requirement.
- A `release` environment restricted to `main`, with the agreed required
  reviewers. A workflow reference alone does not create those protections.
- GitHub Actions access to the pinned actions, 14-day artifact retention and
  `contents: write` for the publication job. Build/test jobs need read access.
- The approved dispatch identity and publication authority for that invocation.

At setup inspection, the repository listed no rulesets or environments. This
change does not alter hosted settings. Static and local checks cannot establish
that hosted approval, publishing or immutability settings are effective.

## Native verification

Use the project's existing `.venv` (Python 3.13+) and the hash-pinned
`requirements-build.txt`. Installation is confined to that environment.
FFmpeg/ffprobe 8+ must be on PATH for the full tests and `doctor`.

```bash
.venv/bin/python -m pip install --require-hashes -r requirements-build.txt
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -c 'from setuptools.build_meta import build_sdist; build_sdist("dist")'
.venv/bin/python -m pip wheel --no-build-isolation --no-deps dist/*.tar.gz --wheel-dir dist
.venv/bin/python tools/build_zipapp.py --no-progress
cp videorebuild.pyz dist/
VERSION="$(PYTHONPATH=src .venv/bin/python -c 'import videorebuild; print(videorebuild.__version__)')"
.venv/bin/python tools/check_release.py --dist dist --version "$VERSION"
```

Run builds in a disposable source copy with an empty `dist` so stale artifacts
cannot be mixed into the candidate. The checker rejects extra distributions,
version/name mismatches and differences between the checked-out package code
and wheel, sdist or zipapp contents. Install the wheel built from the sdist into
a disposable environment, change outside the checkout, and require
`importlib.metadata.version("videorebuild") == videorebuild.__version__` before
CLI/TUI-help and `doctor` checks. Run the zipapp's `--version` and `doctor` too.
The ordinary CI matrix performs these checks; the release workflow repeats
tests and consumer checks on its exact selected revision.

`tools/build_zipapp.py` overwrites the ignored local `videorebuild.pyz`.
Setuptools and pip execute the repository's declared build backend; they do not
commit, tag or publish. `tools/check_release.py` reads package archives without
importing their contents. Its `--write-evidence --revision FULL_SHA` option
writes `SHA256SUMS.txt` and `source-revision.txt` only in the selected artifact
directory; the caller must first verify that SHA is the checked-out revision.

## Authorized GitHub release

After explicit publication approval and hosted prerequisites are verified:

```bash
gh workflow run release.yml --ref main \
  -f revision=APPROVED_FULL_COMMIT_SHA -f version=APPROVED_X.Y.Z
```

The workflow accepts dispatches from `main` only. It validates input syntax,
checks out the exact SHA, requires that it is an ancestor of `main`, requires
release notes, and builds/checks artifacts against the canonical version.
Stable releases are serialized without canceling an active publication.
Verified bytes, checksums and source revision are uploaded before publication
as `release-SHA-RUN_ID-RUN_ATTEMPT`, retained for 14 days subject to repository
retention policy. Failed retention blocks publication.

The publication job downloads those bytes, verifies checksums and revision,
and fails on any existing exact tag or release, including a partial draft.
It creates a new tag at the verified SHA and a draft GitHub release, uploads
all five assets, checks GitHub asset digests and downloaded bytes, then
publishes the completed draft. Draft publication supports immutable GitHub
releases without replacing assets after publication. No package registry
credentials or PyPI publisher are used.

## Failure and recovery

Record version, full SHA, tag, workflow run/attempt, asset names and digests
before taking recovery action. API/authentication failures are failures, never
evidence that an identity is absent. A rerun after tag creation or partial
publication deliberately stops; do not bump a version merely to bypass a
failed job, and do not delete/recreate or force-update its tag automatically.

For a partial release, inspect the tag target and any draft/published assets.
Compare retained source revision and checksums with downloaded bytes. Obtain
approval for only the missing operations and use a reviewed recovery procedure
against that same identity. Never overwrite an existing asset or finalize a
draft whose bytes have not all been verified. If a published identity contains
wrong content, preserve it and prepare a corrective release with a new version.

Retention is temporary. Preserve approved recovery evidence before it expires.
A rebuild from the same commit is not proof of byte-identical archives; compare
digests or disclose the difference. Successful CI is not proof of hosted
publication or consumer acceptance. After publication, read back the tag/SHA,
release state and asset digests, then independently consume the published wheel
and zipapp in a disposable environment under the granted authority.

## Version-source migration and restoration

The setup consolidated two equal 0.3.0 literals by replacing static
`project.version` with setuptools dynamic metadata; the runtime source literal
was preserved. Before this one-off conversion, original affected files and
SHA-256 digests were backed up locally under the ignored
`test-results/release-setup/original/` and `backup.json`. That directory is not
included in the sdist or committed.

Before committing, restoration means reviewing intervening edits, then copying
only the affected original files from that backup. Do not overwrite later
user/agent work. Review references before removing newly added files. After
merge or publication, restore through a reviewed corrective commit rather than
rewriting history. Rebuild wheel/sdist/zipapp and recheck native metadata after
any restoration.
