# Data provenance and release artifacts

[Documentation index](README.md) · [Construction methodology](PIPELINE.md)

## Authoritative data

Published data is under [`data/releases/`](../data/releases/). Read each release's
manifest and README before consuming records. Counts describe that frozen release,
not a live execution queue or a current leaderboard.

| Release | Role | Evidence |
| --- | --- | --- |
| [v1](../data/releases/v1/) | First 100-task strict release | [manifest](../data/releases/v1/manifest.json), [provenance JSONL](../data/releases/v1/provenance.jsonl) |
| [reserve 20260721](../data/releases/reserves/20260721/) | Historical strict reserve and its evaluation snapshots | [manifest](../data/releases/reserves/20260721/manifest.json) |
| [reserve 20260728](../data/releases/reserves/20260728/) | Later strict reserve / cumulative construction evidence | [manifest](../data/releases/reserves/20260728/manifest.json) |
| [hard 20260808](../data/releases/hard/20260808/) | Frozen 414-task hard release | [manifest](../data/releases/hard/20260808/manifest.json), [report](../data/releases/hard/20260808/report.json) |

The [audited-402 ID list](../configs/openmodel_scope_ids/audited-402.ids) is a
specific experiment scope, not a renaming of the 414-task release. Other historical
partial scopes must retain their own IDs and denominators.

## Record lineage

1. A merged GitHub PR supplies review-comment identity, repository, timestamps,
   exact `start_sha`/`target_sha`, locality and evidence that the concern was addressed.
2. Candidate/evidence records preserve reviewer text and exact-revision before,
   localized reference patch and full same-file patch. Suggestion leakage and
   incomplete evidence are explicitly handled during selection.
3. Independent task-quality decisions and executable Oracle validation establish
   eligibility. Source-only golden, PASS_TO_PASS, changed-line coverage and runtime
   evidence are construction artifacts, not inferred from a model score.
4. Release files connect those records by task `id`. Where present,
   `provenance.jsonl` traces source batches and record hashes; `manifest.json`,
   `report.json` and `SHA256SUMS` record release rules, counts and integrity.
5. Evaluation records bind task/model/protocol to generation, candidate patch,
   correctness and judgments. Verify patch/runtime/engine identities and declared
   judge policy before aggregating; never join by physical row number alone.

Exact historical cohort and field-level derivations are retained in
[archived DATA_PROVENANCE](archive/DATA_PROVENANCE.md). Original source batches,
raw logs, worktrees, images and dependency caches are not all included here.
Placeholders in records identify external assets; they are not downloadable files.
A checksum-valid metadata release alone does not prove its environments are ready.

## Integrity checks

The checksum files use different path conventions. Run these from the repository
root; the subshell is intentional for the hard release:

```bash
sha256sum -c data/releases/v1/SHA256SUMS
(cd data/releases/hard/20260808 && sha256sum -c SHA256SUMS)
```

Use each reserve/evaluation README and checksum file for its corresponding check.
Do not rewrite frozen release bytes just to align an overview or a new judge panel.

## Scores and corrections

Historical reports include line-edit protocols, three-judge panels, partial
measurements, audit corrections and withdrawn tables. They remain in
[the archive](archive/README.md) with their original limitations. They are not
measurements of the Astra-only policy currently selected by the executable-agent
YAMLs, nor of the distinct no-Gemini policy. A changed panel requires separately
labeled results; this documentation cleanup performs no rejudging or new evaluation.
