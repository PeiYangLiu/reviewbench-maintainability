# ReviewBench-Maintainability

Prototype tooling for constructing a maintainability benchmark from real GitHub pull request review signals.

The benchmark idea is to mine merged PRs where reviewers asked for maintainability improvements, localize each task to the review comment's file/region, and use the final PR evolution as evidence that the concern was addressed. This is intentionally closer to SWE-bench than to synthetic "make this code cleaner" prompts: tasks come from real repositories, real reviewer comments, and real accepted changes.

## Documentation

- [`docs/CODEBASE_GUIDE.md`](docs/CODEBASE_GUIDE.md): current code map, authoritative entry points, tests, and legacy boundaries
- [`docs/DATA_PROVENANCE.md`](docs/DATA_PROVENANCE.md): complete data lineage from mining through release and model evaluation
- [`docs/PROGRESS_20260808.md`](docs/PROGRESS_20260808.md): current authoritative status, 414-task hard release, corrected three-model evaluation, and final artifact map
- [`docs/OPEN_MODEL_EVALUATION.md`](docs/OPEN_MODEL_EVALUATION.md): Qwen/open-weight AMLT jobs, pinned model revisions, GPU assignments, complete artifact schema, and blob result paths
- [`docs/LATEST_MODEL_PARTIAL_LEADERBOARD_20260815.md`](docs/LATEST_MODEL_PARTIAL_LEADERBOARD_20260815.md): withdrawn 408-task snapshot retained for audit; do not use for comparison
- [`docs/LATEST_MODEL_PARTIAL_LEADERBOARD_20260813.md`](docs/LATEST_MODEL_PARTIAL_LEADERBOARD_20260813.md): historical 381/378-task partial snapshot before legacy recovery
- [`docs/WEEKLY_REPORT_20260811.md`](docs/WEEKLY_REPORT_20260811.md): current 414-hard latest-model evaluation report, live gate progress, risks, and delivery plan
- [`docs/WEEKLY_REPORT_20260805.md`](docs/WEEKLY_REPORT_20260805.md): historical ICLR 2027-oriented weekly report before the hard-task Oracle campaign completed
- [`docs/PROGRESS_20260728.md`](docs/PROGRESS_20260728.md): historical 475-task cumulative release snapshot
- [`docs/PROGRESS_20260714.md`](docs/PROGRESS_20260714.md): historical v1 and first-reserve progress snapshot
- [`docs/PIPELINE.md`](docs/PIPELINE.md): detailed construction methodology and historical funnel
- [`docs/PITFALLS.md`](docs/PITFALLS.md): failures, root causes, fixes, and methodological lessons

## 414-task hard release

The current hard benchmark is frozen at
[`data/releases/hard/20260808`](data/releases/hard/20260808/):

- **414** strict hard maintainability tasks from **340 repositories** and
  **385 pull requests**;
- 40 hard tasks from the calibrated 475-task release plus 374 new,
  semantically non-overlapping hard tasks;
- 17 language labels and 8 maintainability categories;
- exact start and source-only-golden revisions;
- three stable runs on both sides, non-empty PASS_TO_PASS, and real
  changed-old production coverage;
- 1,242 task-model evaluations under `agentic-line-edit/v1`;
- the human-golden-calibrated maintainability rubric v2.

Together with the 435 medium tasks in the previous cumulative release, the
project now contains **849 unique strict tasks: 414 hard and 435 medium**.

| Model | Generated | Scope | Correctness | Maintainability | Resolved@1 |
|---|---:|---:|---:|---:|---:|
| **GPT-5.5** | 413 | 392 | 341 | 263 | **212/414 (51.2%)** |
| **Claude Opus 4.8** | 411 | 401 | 357 | 216 | **177/414 (42.8%)** |
| **Gemini 3.5 Flash** | 340 | 323 | 242 | 179 | **143/414 (34.5%)** |

Resolved@1 requires generation/apply, single-file scope, executable
correctness, and the calibrated maintainability judgment. Release contents:

| File | Purpose |
|---|---|
| `tasks.jsonl` | 414 strict hard tasks and construction evidence |
| `oracles.jsonl` | One strict executable Oracle record per task |
| `results.jsonl` | 1,242 normalized four-gate model results |
| `result-overrides.jsonl` | Sanitized runner-derived infrastructure corrections |
| `report.json` | Leaderboard, language/category breakdowns, and pairwise outcomes |
| `manifest.json` | Release contract, source waves, counts, placeholders, and hashes |
| `SHA256SUMS` | Integrity hashes for all release files |

Rebuild the compact release from the calibrated 475-task results and the three
new hard waves with `scripts/package_hard_release.py`. Machine-specific roots
are replaced by `${REVIEWBENCH_SESSION_FILES}`,
`${REVIEWBENCH_REPOSITORY}`, and `${HOME}`.

The hard-task definition has two historical layers. The original 40 tasks use
the conservative published difficulty field. The 374-task expansion requires
a strict GPT quality pass and at least one independent hard verdict; this
avoids treating uncalibrated cross-model confidence scores as a hard filter.
See [`docs/PROGRESS_20260808.md`](docs/PROGRESS_20260808.md) for the exact
selection and Oracle funnel.

## v1 strict release

The first strict release is complete:

- **100** executable tasks from **86 repositories** and **97 pull requests**
- **11 hard** and **89 medium** tasks under the conservative two-judge rule
- **52 Go**, **36 Python**, **5 TypeScript**, and 7 tasks across PHP, C, C++, Java, and Rust
- two independent quality judgments per task
- stable start/golden PASS_TO_PASS tests
- changed-old-source-line coverage for every task
- semantic reference-patch deduplication

Release files are in [`data/releases/v1`](data/releases/v1/):

| File | Purpose |
|---|---|
| `tasks.jsonl` | Final tasks, evidence, quality judgments, and release validation |
| `oracles.jsonl` | Runnable-oracle and recovery metadata keyed by task ID |
| `report.json` | Aggregate release statistics |
| `manifest.json` | Versioned gates, counts, and hashes |
| `SHA256SUMS` | Integrity checks for generated data |

Re-run the release gate:

```bash
reviewbench-release \
  --tasks data/releases/v1/tasks.jsonl \
  --evaluations data/releases/v1/oracles.jsonl \
  --target 100 \
  --out /tmp/reviewbench-v1-tasks.jsonl \
  --report /tmp/reviewbench-v1-report.json

cmp data/releases/v1/tasks.jsonl /tmp/reviewbench-v1-tasks.jsonl
sha256sum -c data/releases/v1/SHA256SUMS
```

### Non-agentic baseline model evaluation

The complete 100-task evaluation of Claude Opus 4.8, Gemini 3.5 Flash, and
GPT-5.5 is in
[`data/releases/v1/evaluations/three-model-20260714`](data/releases/v1/evaluations/three-model-20260714/).
This frozen baseline uses the original SEARCH/REPLACE text protocol.

| Model | Resolved@1 | Hard |
|---|---:|---:|
| Gemini 3.5 Flash | **66/100** | 6/11 |
| Claude Opus 4.8 | **63/100** | 5/11 |
| GPT-5.5 | **61/100** | **7/11** |

Resolved@1 requires a successfully generated in-scope patch, the verified correctness
oracle, and a strict blind maintainability judgment. The evaluation directory
contains every generated patch and all per-task gate results.

### Post-v1 strict reserve

The first dated post-v1 reserve is frozen at
[`data/releases/reserves/20260721`](data/releases/reserves/20260721/):

- 206 new strict tasks from 178 repositories
- 15 hard and 191 medium
- complete dual-model quality judgments, stable P2P, golden correctness, and
  changed-old-line coverage
- no semantic duplicates with v1
- a cumulative v2 preview of 306 tasks

This is a reserve rather than a final v2 release because continuous mining and
the remaining 13,647 eligible quality candidates are still being processed.

The cumulative 306-task three-model evaluation is available at
[`data/releases/reserves/20260721/evaluations/three-model-20260724`](data/releases/reserves/20260721/evaluations/three-model-20260724/):

| Model | Resolved@1 | Hard |
|---|---:|---:|
| GPT-5.5 | **184/306** | **12/26** |
| Gemini 3.5 Flash | **182/306** | **12/26** |
| Claude Opus 4.8 | **180/306** | 10/26 |

### Agentic line-edit protocol

`scripts/generate_agentic_patches.py` provides the protocol-independent
successor to SEARCH/REPLACE. A model operates on the exact `start_sha` through
four host actions:

- `read` and `find` inspect repository files;
- `edit` applies 1-based line-range edits to the single allowed production
  file;
- `finish` asks the host to materialize the candidate;
- Git computes the final patch with `git diff --binary`.

The model never writes a diff and never has to copy old source text into a
SEARCH block. The host rejects path traversal, cross-file edits, overlapping
line ranges, empty changes, and invalid checkpoint traces. If the round budget
expires after valid edits, the host materializes the last worktree state;
`finish` is only an early-stop action. The original
SEARCH/REPLACE results remain frozen as the non-agentic baseline; agentic
results must be generated and evaluated separately rather than filling only
the baseline's protocol failures.

The complete 306-task agentic evaluation is available at
[`data/releases/reserves/20260721/evaluations/agentic-three-model-20260725`](data/releases/reserves/20260721/evaluations/agentic-three-model-20260725/):

| Model | Generated | Correctness | Maintainability | Resolved@1 | Hard |
|---|---:|---:|---:|---:|---:|
| GPT-5.5 | **306/306** | **249/306** | **233/306** | **198/306** | 10/26 |
| Claude Opus 4.8 | 305/306 | 240/306 | 221/306 | **187/306** | **11/26** |
| Gemini 3.5 Flash | 255/306 | 196/306 | 172/306 | **157/306** | 7/26 |

Relative to the frozen SEARCH/REPLACE baseline, GPT-5.5 gains 14 resolved
tasks and Claude gains 7. Gemini loses 25 because it frequently fails to
complete the multi-turn agent loop; this is a generation failure rather than
a Git patch-application failure. Full action traces and protocol-failure
records are included in the evaluation directory.

### Second strict reserve

The next dated reserve is frozen at
[`data/releases/reserves/20260728`](data/releases/reserves/20260728/):

- 169 new strict tasks from 160 repositories and 164 pull requests
- 14 hard and 155 medium
- complete dual-model quality judgments
- start/source-only-golden stability, non-empty P2P, and real changed-old
  production coverage for every released task
- 11 same-solution review-comment duplicates excluded
- no ID or semantic fix overlap with the previous 306 tasks

The cumulative preview now contains **475 tasks**:

- 40 hard
- 435 medium
- 410 repositories

Construction funnel:

`109,619 mined candidates → 70,939 quality-eligible → 3,000 priority evidence
tasks → 2,581 complete evidence → 533 GPT strict passes → 280 dual-model
passes → 268 semantic-unique Oracle candidates → 180 Oracle passes → 169
released after duplicate exclusion`.

The final audit invalidated an initial set of 35 Native oracles because they
tested toy reimplementations and used `#line` probes instead of real production
coverage. All Native tasks were rebuilt from scratch against real repository
sources; 33/35 passed the corrected Oracle. The invalid artifacts are not part
of the release.

## Historical general release

The sections below describe the original v1 and 475-task general release.
Those artifacts remain frozen and are not rewritten by the 414-task hard
release.

## What this prototype does

- Searches or accepts merged GitHub PRs.
- Fetches PR review comments, PR metadata, changed files, commits, and optionally GraphQL review-thread resolution state.
- Classifies review comments into maintainability categories with high-precision keyword heuristics.
- Strips GitHub ```suggestion``` blocks to avoid leaking exact replacement code.
- Emits JSONL candidate tasks with:
  - repository and PR identity
  - task instruction from the reviewer comment
  - `start_sha` from the commented commit
  - `target_sha` from the merged PR head
  - file/line locality
  - addressed evidence: resolved review thread and/or region/file touched after the comment
  - gate type: `auto_static` for measurable categories, `manual_or_llm_calibrated` otherwise
- Provides static metric utilities for local before/after scoring.

## Defensible construction recipe

1. **Human label source**: use reviewer comments, not benchmark-author opinions, as the maintainability signal.
2. **Task locality**: each benchmark item is anchored to one review comment and one file/region. The whole PR diff is not treated as the answer to a single comment.
3. **Addressed filter**: keep a candidate only if its review thread is resolved or the commented region/file changed after the comment, depending on `--addressed-policy`.
4. **Correctness gate**: generated patches should pass the repository's original tests. This prototype records the candidate; production benchmark curation should add per-repo test commands and FAIL_TO_PASS/PASS_TO_PASS-style selection.
5. **Maintainability gate**: use automatic static gates only for categories with measurable signals, currently complexity, duplication, and dead code. Other categories are retained as `manual_or_llm_calibrated` candidates and should be separately calibrated by human labels.
6. **Reproducibility**: save API snapshots with `--snapshot-dir` and rebuild datasets from those snapshots with `--from-snapshot`.

## Strict quality-improvement release gate

The legacy `CLEAN` criterion only established that the human patch passed tests
and addressed a reviewer comment. It did not prove that every task was a
substantive maintainability improvement. A strict release now requires all of
the following:

1. complete before/reference evidence from the exact task commits;
2. unanimous judgments from at least two distinct judge models;
3. a concrete code smell and a substantive, attributable quality improvement;
4. maintainability as the primary goal, not a bug fix, feature, or style-only edit;
5. independently judged intrinsic difficulty of at least medium;
6. stable PASS_TO_PASS tests at both start and golden revisions;
7. execution coverage of at least one changed source line; and
8. semantic deduplication of tasks backed by the same local reference fix.

`partial` judgments, low-confidence judgments, truncated evidence, judge errors,
and uncovered changes fail closed. The release command exits non-zero until the
requested target count is reached. The checked-in v1 release reaches the target
of 100 and returns zero.

Quality tasks that fail setup, build, baseline, PASS_TO_PASS, or coverage are
not discarded. `reviewbench-recovery` writes them to a persistent recovery
queue for per-repository Docker/toolchain work; only structural failures such as
test anchors are marked ineligible.

```bash
reviewbench-curate \
  --in data/difficulty/combined_v3.jsonl data/difficulty/expanded.jsonl \
  --out /tmp/reviewbench/curation_pool.jsonl \
  --report /tmp/reviewbench/curation_pool_report.json

reviewbench-evidence \
  --in /tmp/reviewbench/curation_pool.jsonl \
  --out /tmp/reviewbench/evidence.jsonl \
  --repo-cache /tmp/reviewbench/repos

reviewbench-quality \
  --in /tmp/reviewbench/evidence.jsonl \
  --judgments judge_a.jsonl judge_b.jsonl \
  --out /tmp/reviewbench/quality_gated.jsonl

python scripts/validate_oracles.py \
  --in /tmp/reviewbench/quality_gated.jsonl \
  --out /tmp/reviewbench/oracles.jsonl \
  --repos-dir /tmp/reviewbench/repos

reviewbench-release \
  --tasks /tmp/reviewbench/quality_gated.jsonl \
  --evaluations /tmp/reviewbench/oracles.jsonl \
  --target 100 \
  --out data/releases/v1/tasks.jsonl \
  --report data/releases/v1/report.json

reviewbench-recovery \
  --tasks /tmp/reviewbench/quality_gated.jsonl \
  --evaluations /tmp/reviewbench/oracles.jsonl \
  --out /tmp/reviewbench/recovery_queue.jsonl \
  --report /tmp/reviewbench/recovery_report.json
```

## Install for local development

```bash
python -m pip install -e .
```

The miner uses the GitHub CLI:

```bash
gh auth status
```

## Build candidate tasks

Mine specific PRs:

```bash
reviewbench-build \
  --pr psf/requests#6500 \
  --out data/candidates.jsonl \
  --report data/report.json \
  --snapshot-dir data/snapshots
```

Search a scoped repository:

```bash
reviewbench-build \
  --repo psf/requests \
  --query 'is:pr is:merged refactor' \
  --limit 25 \
  --out data/candidates.jsonl \
  --report data/report.json \
  --snapshot-dir data/snapshots
```

Rebuild from saved snapshots without hitting GitHub:

```bash
reviewbench-build \
  --from-snapshot data/snapshots \
  --out data/candidates.rebuilt.jsonl \
  --report data/report.rebuilt.json
```

## Score local before/after code

```bash
reviewbench-score \
  --before-dir /tmp/repo-before \
  --after-dir /tmp/repo-after \
  --paths src/foo.py src/bar.py \
  --category complexity
```

The scorer reports raw before/after metrics and directional evidence. It is not a complete oracle for every maintainability concept; use it as one component of the benchmark evaluator.

## Evaluate a model patch

`reviewbench-evaluate` is the first runnable benchmark harness. It follows a SWE-bench-style gate order:

1. clone or copy the target repo;
2. checkout the task `start_sha`;
3. apply the model patch with `git apply`;
4. run configured setup and correctness commands;
5. check scope constraints;
6. only if correctness passed, score maintainability evidence.

For auto-static categories (`complexity`, `duplication`, `dead_code`), the maintainability gate uses the static metrics in `reviewbench-score`. Other categories are marked as requiring a manual or LLM-calibrated quality gate.

Single-task example:

```bash
reviewbench-evaluate \
  --task data/task.json \
  --patch outputs/model.patch \
  --oracle-config data/oracles.json \
  --work-dir /tmp/reviewbench-eval \
  --out data/eval_result.json
```

Batch example, where patches are named `<task_id>.patch` or `<task_id>.diff`:

```bash
reviewbench-evaluate \
  --tasks data/verified_strict.jsonl \
  --patch-dir outputs/patches \
  --oracle-config data/oracles.json \
  --work-dir /tmp/reviewbench-eval \
  --out data/eval_results.jsonl
```

Oracle config example:

```json
{
  "defaults": {
    "timeout_seconds": 600,
    "require_locality_touched": true,
    "max_changed_files": 5,
    "max_changed_lines": 300
  },
  "repos": {
    "owner/repo": {
      "setup_commands": [
        "python -m pip install -e ."
      ],
      "test_commands": [
        {
          "name": "unit_tests",
          "command": "python -m pytest",
          "timeout_seconds": 900
        }
      ]
    }
  },
  "tasks": {
    "github__owner__repo__pull_123__comment_456__complexity": {
      "allowed_files": [
        "src/foo.py",
        "tests/test_foo.py"
      ],
      "maintainability_paths": [
        "src/foo.py"
      ]
    }
  }
}
```

The final result has `passed: true` only when patch application, setup, correctness, scope, and maintainability gates all pass. If correctness commands are missing, the task fails with `correctness:missing_correctness_commands`; this is intentional because maintainability improvements should only be scored after functional correctness is established.

## Candidate JSONL schema

Each line is a JSON object:

```json
{
  "schema_version": "reviewbench-maintainability/v0.1",
  "id": "github__owner__repo__pull_123__comment_456__complexity",
  "repo": "owner/repo",
  "pr_number": 123,
  "pr_url": "https://github.com/owner/repo/pull/123",
  "task_instruction": "Could we split this function into smaller helpers?",
  "category": "complexity",
  "gate_type": "auto_static",
  "confidence": 0.82,
  "start_sha": "commented commit sha",
  "target_sha": "merged PR head sha",
  "merge_commit_sha": "merge commit sha if available",
  "locality": {
    "path": "src/foo.py",
    "line": 80,
    "start_line": 60,
    "diff_hunk": "@@ ..."
  },
  "addressed_evidence": {
    "thread_resolved": true,
    "file_touched_after_comment": true,
    "region_touched_after_comment": true
  },
  "oracles": {
    "correctness": "run original project tests",
    "maintainability": "auto_static"
  }
}
```

## Known limitations

- GitHub search results drift over time; use snapshots for reproducible datasets.
- Keyword classification must be calibrated with a hand-labeled sample before publication.
- Review comments can include exact suggested code; this prototype strips suggestion blocks but also flags whether one was present.
- Static metrics are directional evidence, not universal definitions of maintainability.
- Multi-language support is intentionally conservative; Python has richer AST metrics, other languages fall back to text metrics.
- Repository environments remain the main scaling bottleneck; a candidate does
  not count toward a release until its exact historical environment and covered
  PASS_TO_PASS oracle run successfully.
