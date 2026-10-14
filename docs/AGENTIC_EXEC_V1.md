# Agentic Exec v1

[Documentation index](README.md) · [Architecture](ARCHITECTURE.md) · [Reuse rules](AGENTIC_EVALUATION_REUSE_PRINCIPLES.md)

The installed `reviewbench-agent` command implements
`reviewbench-agentic-exec/v1`, independently of historical
`agentic-line-edit/v1` and `agentic-line-edit/v2` experiments. It supports real
multi-turn tool execution, persistent developer workspaces, multi-file patches
and separately recorded evaluation gates.

## Prerequisites and configuration

Use Python 3.12, the optional pinned mini-agent dependencies and Docker. See
[development](DEVELOPMENT.md#environments) for the hash-pinned lock installation.
Loading YAML needs PyYAML; `--help` alone does not check it. Real runs also need
exact source commits, audited images/assets, fixed Oracle metadata, runtime
recipes, explicit model/provider settings and available resources.

Examples:
[`agentic-exec-v1.yaml`](../configs/agentic-exec-v1.yaml) and
[`agentic-exec-v1.gateway.yaml`](../configs/agentic-exec-v1.gateway.yaml).
Paths resolve relative to the configuration's `root`, itself relative to the YAML
file. Environment-variable references must resolve. Configure your own cache,
asset and output locations; do not put credential values in tracked YAML.

The examples select the hard release and audited-402 scope but set
`runtime_recipes: null`. That is an inventory starting point, **not a ready-to-run
402-task environment**. Supply compatible recipes before verification/generation.
Model labels and expected response identities are explicit configuration, not a
promise that those models/endpoints are available on a new machine.

## Lifecycle

These commands are templates for a configuration and assets you have prepared;
`inputs/experiment.yaml` and `inputs/runtime-recipes.jsonl` are user-supplied.

```bash
# Inventory only: no model generation or proof of runtime readiness.
reviewbench-agent prepare --config inputs/experiment.yaml

# Verify public checks and fixed start/golden Oracles before generation.
reviewbench-agent prepare --config inputs/experiment.yaml \
  --runtime-recipes inputs/runtime-recipes.jsonl --verify --workers 2

# Use the same recipe/config identity at every later stage.
reviewbench-agent run --config inputs/experiment.yaml \
  --runtime-recipes inputs/runtime-recipes.jsonl --resume
reviewbench-agent evaluate --config inputs/experiment.yaml \
  --runtime-recipes inputs/runtime-recipes.jsonl
reviewbench-agent report --config inputs/experiment.yaml \
  --runtime-recipes inputs/runtime-recipes.jsonl
```

Prefer setting `runtime_recipes` in your configuration once instead of repeating
an override. `--workers` controls independent jobs for prepare/run/evaluate;
reporting has no worker flag. Inspect JSON output and exit status: the CLI returns
0 for a complete result and 2 for incomplete results or caught contract,
infrastructure, configuration or I/O errors. Inventory success is not solver success.

Readiness binds task, Oracle, runtime and engine fingerprints. `run` rejects stale
verification and incompatible existing run manifests. Outputs include
`preparation/readiness.json`, per-task verification, `run/run-manifest.json`,
episode artifacts, correctness/judgment records and fixed-matrix reports.

## Execution and evidence boundaries

A `reviewbench-agentic-runtime/v1` recipe binds task/repo/start/lane, an audited
image digest, public development checks, resource limits, assets/visibility,
optional sidecars and fixed grading instructions. Historical Oracle metadata does
not itself authorize a safe interactive developer image.

The developer sees the starting source and allowed development assets, not future
commits, golden patches, private grader evidence or host credentials. Grading
reconstructs a fresh starting tree plus the full candidate patch, restoring the
fixed tests declared by the recipe. Added model tests do not replace PASS_TO_PASS.

The mini-agent loop receives actual tool stdout/stderr/exit codes. Ordinary
command/test failures are observations, not automatic infrastructure failures.
Full logs and trajectories are retained separately from bounded model-visible
observations. Host patch collection does not trust the model's Git HEAD/index.

Budgets in the examples allow 100 calls, 128000 cumulative output tokens, 8192
per-call output tokens and 3600 seconds; these are configuration values, not a
promise every run consumes them. Preserve usage, request IDs, termination reasons,
patch hashes and incomplete measurements rather than silently retrying for quality.

## Judge policies

Source of truth: [`judge_policy.py`](../src/reviewbench_maintainability/agentic/judge_policy.py),
[`evaluation.py`](../src/reviewbench_maintainability/agentic/evaluation.py) and
[`reporting.py`](../src/reviewbench_maintainability/agentic/reporting.py).

| `judge.score_version` | Required roster | Aggregation |
| --- | --- | --- |
| `reviewbench-judge-panel/three-judge-v1` | Three distinct configured judges; default if the version is absent | Unanimous |
| `reviewbench-judge-panel/no-gemini-3.6-20261011-v1` | Two distinct configured judges, excluding `gemini-3.6-flash` | Unanimous |
| `reviewbench-judge-panel/astra-only-20261011-v1` | Exactly `[gpt-6-astra]` | Single judge |

**Both shipped YAMLs currently select the Astra-only version.** The no-Gemini
validator does not hard-code the two remaining names: the explicitly configured
roster is part of the policy identity. Do not infer today's YAML roster from an
older operations note. Unknown versions and inconsistent policy metadata fail.

A completed panel needs one valid boolean verdict per required distinct member,
in the declared order for an explicit versioned policy. Missing/malformed replies,
HTTP failures and rejected model identities are not affirmative votes. Valid
negative judgments remain negative; retries are not best-of-N sampling.

`reporting.make_report` rejects mixed score versions **or different rosters**.
Resolved requires generation/apply, scope, correctness and maintainability. A
no-patch result is a measured failure; missing evaluation evidence is incomplete.
A correctness failure can establish an unresolved answer while the four-gate
report remains incomplete because judgment is missing. `resolved_rate` is only
published for complete measurements; otherwise the report exposes a lower bound
and missing/incomplete gate counts.

## Resume and regrade

`run --resume` preserves terminal outcomes and resumes only compatible states;
`--retry-infra` retains prior attempt evidence and is not permission to resample
model failures. Reuse accepted judge replies, including vetoes, and continue only
unfinished work. See [reuse principles](AGENTIC_EVALUATION_REUSE_PRINCIPLES.md).

`evaluate --regrade` and `report --regrade` support grading-engine changes against
frozen generations under identity checks. They do not rerun the solver or provide
an unrestricted panel-migration switch. A changed panel/roster needs a separately
versioned, reviewed integration and compatible evidence; never edit a frozen
manifest to make an incompatible configuration appear resumable.

## Historical protocol references

The [archived agent guide](archive/AGENTIC_EXEC_V1.md) preserves an earlier
three-judge description. [Archived line-edit methodology](archive/PIPELINE.md)
and [frozen reports](archive/README.md) remain historical. This guide does not
claim new benchmark completion or reinterpret their scores.
