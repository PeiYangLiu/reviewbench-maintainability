# ReviewBench-Maintainability

Prototype tooling for constructing a maintainability benchmark from real GitHub pull request review signals.

The benchmark idea is to mine merged PRs where reviewers asked for maintainability improvements, localize each task to the review comment's file/region, and use the final PR evolution as evidence that the concern was addressed. This is intentionally closer to SWE-bench than to synthetic "make this code cleaner" prompts: tasks come from real repositories, real reviewer comments, and real accepted changes.

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

## Install for local development

```bash
cd /home/v-peiyangliu/reviewbench-maintainability
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
