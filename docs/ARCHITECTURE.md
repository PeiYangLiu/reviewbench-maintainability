# Architecture

[Documentation index](README.md) · [Codebase map](CODEBASE_GUIDE.md)

## Two related pipelines

```text
GitHub review snapshots → candidates → exact-revision evidence
  → independent task-quality gate → executable Oracle validation → frozen release

Frozen task + candidate patch → apply/scope + correctness + maintainability
  → identity-bound result → protocol/score-version-specific report
```

Task construction quality is distinct from candidate-solution maintainability.
`quality-improvement/v1` judges whether a task belongs in the benchmark;
`agentic/judge_policy.py` selects the panel evaluating an executable-agent answer.
Neither is a license to reinterpret historical result files.

## Candidate and release records

[`models.py`](../src/reviewbench_maintainability/models.py) defines the candidate
schema. Important fields are `id`, `repo`, `pr_number`, `task_instruction`,
`category`, `start_sha`, `target_sha`, `locality`, `addressed_evidence`, `comment`
and `oracles`. Quality evidence adds a numbered before window, a localized patch
and a complete same-file patch with explicit completeness/truncation metadata.
Exact two-point blob differences avoid GitHub merge-base/rebase ambiguity.

Release records enrich candidates with quality and correctness evidence. Join
JSONL records by task ID, never row position. Treat raw mined candidates,
quality-passed candidates, runtime-ready tasks and released tasks as different
states. The release selector can fail to reach its requested target and returns
nonzero rather than inventing additional eligible tasks.

## Generic supplied-patch evaluator

`reviewbench-evaluate` accepts one task JSON or task JSONL plus supplied patch(es).
A JSON Oracle configuration merges `defaults`, per-`repos` and per-`tasks` rules:

```json
{
  "defaults": {"timeout_seconds": 600, "require_locality_touched": true},
  "repos": {
    "owner/repo": {
      "setup_commands": ["python -m pip install -e ."],
      "test_commands": [{"name": "unit", "command": "python -m unittest discover -s tests"}]
    }
  },
  "tasks": {
    "TASK_ID": {
      "allowed_files": ["src/example.py"],
      "maintainability_paths": ["src/example.py"]
    }
  }
}
```

Supply commands appropriate for the actual target repository. The evaluator
checks out `start_sha`, applies the patch, records scope, runs setup/correctness,
and computes maintainability evidence only after correctness passes. Static
categories are `complexity`, `duplication` and `dead_code`; other concerns require
a separate calibrated/manual quality gate. Missing correctness commands fail
closed. This evaluator does not infer a runnable environment from arbitrary
historical Oracle JSONL.

**Trust boundary:** generic Oracle commands execute as local shell commands.
Review inputs and run untrusted repository code inside an appropriate isolated
environment. `--work-dir` is disposable: an existing task run directory can be
removed/rebuilt. The generic evaluator is not the agent's Docker isolation layer.

## Executable-agent lifecycle

`reviewbench-agent` is a separate, multi-file execution protocol:

1. **Prepare** inventories selected tasks, source commits, Oracle metadata and
   runtime recipes; `--verify` executes public checks and repeated start/golden
   correctness to produce identity-bound readiness evidence.
2. **Run** checks readiness, fixes the task×model/config/runtime identity, starts
   an isolated developer workspace and records model/tool calls and budgets.
3. **Evaluate** reconstructs the candidate from the trusted start plus patch,
   protects fixed tests, checks scope/correctness and records judge evidence.
4. **Report** validates IDs, patch hashes, protocol, score version and roster,
   then aggregates the fixed task×model matrix.

Developer-visible source/assets and grader-only tests/golden evidence are kept
separate. Host-side patch capture does not trust the model's Git index. Resource
admission, output locking and content-addressed storage make concurrency explicit;
a running worker is not a completed measurement.

## State and scoring invariants

- No-patch generation is a measured model failure. Missing infrastructure or
  judge evidence is an incomplete measurement, not a synthetic passing/failed vote.
- `resolved` requires generation/apply, scope, correctness and maintainability.
  Incomplete gate rows keep `resolved_rate` unset; a labeled lower bound and gap
  counts remain available.
- Unanimity applies to the required roster for the declared policy, not whatever
  subset happened to respond. A single-judge result requires its own version.
- Resume preserves attempts, traces, consumed budget and accepted judgments.
  A changed identity cannot silently adopt a completed run directory.
- Changing a judge panel changes a measurement. Publish separate versioned
  results rather than mixing old three-judge, no-Gemini and Astra-only records.

See [agent operation and judge policy details](AGENTIC_EXEC_V1.md),
[reuse rules](AGENTIC_EVALUATION_REUSE_PRINCIPLES.md) and
[frozen-data provenance](DATA_PROVENANCE.md).
