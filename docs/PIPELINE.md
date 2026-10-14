# Construction pipeline and methodology

[Documentation index](README.md) · [Data provenance](DATA_PROVENANCE.md)

ReviewBench measures whether a model can address a maintainability concern from a
real merged pull request without breaking existing behavior. The accepted human
change supplies construction evidence; it is not the only permissible solution.

## Construction stages

| Stage | Evidence and gate | Main tool |
| --- | --- | --- |
| Mine | Reviewer concern, PR/comment identity, exact revisions, locality and follow-up change | `reviewbench-build` |
| Merge/curate | Stable task IDs; exclude inappropriate anchors/leakage and rank candidates | `reviewbench-merge`, `reviewbench-curate` |
| Extract evidence | Exact before blob, localized reference hunk, full same-file diff, completeness flags | `reviewbench-evidence` |
| Judge task quality | Independent agreement that there is a substantive maintainability improvement | `reviewbench-quality` |
| Validate Oracle | Exact historical start and source-only golden, stable existing tests and changed-old coverage | Retained Oracle construction tools |
| Select release | Quality, correctness, nonempty PASS_TO_PASS, coverage and diversity | `reviewbench-release` |
| Preserve provenance | Task/Oracle IDs, source batches, manifests, hashes and protocol labels | Release packaging tools |

The quality gate `quality-improvement/v1` requires valid, independent judgments:
maintainability as the primary concern, sufficient evidence, an explicit smell,
a substantive attributable golden improvement, no style-only/behavior-change
substitution or clear regression, quality at least 4/5, confidence at least 0.8
and intrinsic difficulty medium/hard. Default aggregation requires at least two
distinct judges and unanimous strict passes. This **task-selection** gate is not
the executable-agent answer panel.

Strict releases additionally require successful golden correctness, at least one
PASS_TO_PASS test and actual changed-old production-line coverage. Published
construction campaigns record repeated start/golden runs. The release selector
consumes these validation records; it does not itself rebuild every historical
repository. It prefers hard tasks and stronger quality evidence, applies semantic
deduplication and limits repository concentration (default at most five per repo).
A shortfall against the target remains a nonzero/incomplete release, not success.

## Correctness is behavioral

- Use tests anchored to the historical starting revision, not tests rewritten to
  match a particular model or human implementation.
- Apply **source-only golden** during Oracle construction. New tests in the
  accepted PR must not silently redefine the task's required behavior.
- Establish nonempty stable PASS_TO_PASS and exercise affected old source lines;
  build success or an unrelated green test suite is insufficient.
- Avoid assertions on private golden helper names, signatures, MRO or call counts
  when the task only requires equivalent public behavior.
- Preserve fixed-test failures and missing assets distinctly. An environment
  error does not demonstrate model failure or authorize a relaxed Oracle.

## Maintainability is more than a metric

Complexity, duplication and dead-code measures provide directional evidence.
Other concerns such as modularity, testability and API design require contextual
review. Keep task-quality judgments, answer-quality judgments and correctness
results distinct. Version the rubric, panel and aggregation; do not turn partial
votes into a final pass. See [current judge policies](AGENTIC_EXEC_V1.md#judge-policies).

## Methodological limitations

Mining keywords miss implicitly phrased concerns. Resolved threads and localized
changes bias the evidence pool. Runtime feasibility favors environments that can
be reconstructed, and quality filtering favors concerns recognized by the rubric.
This is a **selected executable benchmark**, not a random sample of all code
review or maintainability work.

Report task IDs/denominator, repository/language/category composition, exclusions,
protocol, model/serving identity, generation budget, rubric/panel and missing
measurements. Compare only matched scopes and compatible measurement versions;
where repeated arms exist, report sampling variability as well as point estimates.

## Historical evidence

[The archived pipeline](archive/PIPELINE.md) preserves dated funnels and line-edit
protocol descriptions. [Archived provenance](archive/DATA_PROVENANCE.md) preserves
cohort/field-level lineage. Their historical task counts and scoring statements
do not override present source/configuration or authorize rerunning consumed work.
