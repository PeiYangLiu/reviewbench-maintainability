# Codebase guide

[Documentation index](README.md) · [Architecture](ARCHITECTURE.md) · [Development](DEVELOPMENT.md)

## Repository boundaries

| Path | Responsibility |
| --- | --- |
| [`src/reviewbench_maintainability/`](../src/reviewbench_maintainability/) | Installed library and ten CLI entry points |
| [`src/reviewbench_maintainability/agentic/`](../src/reviewbench_maintainability/agentic/) | Executable-agent protocol, isolation, resources and versioned evaluation |
| [`scripts/`](../scripts/) | Established experiment/data tooling and its retained dependency closure; not additional installed CLI |
| [`tests/`](../tests/) | Core, agentic and retained script regression tests |
| [`configs/`](../configs/) | Agent examples and retained experiment configurations, not proof a deployment is running |
| [`data/releases/`](../data/releases/) | Frozen task/Oracle/result metadata, manifests and checksums |
| [`docs/archive/`](archive/) | Historical evidence, not active instructions |

Use the package for reusable behavior. Keep script names stable for reproducible
historical invocations; a helper's presence does not make it a supported public
CLI. Do not reintroduce one-task recovery drivers or local deployment state into
the reusable library. Large caches, model outputs and temporary worktrees belong
outside published source/data.

## Core modules and entry points

Each CLI targets `main` in the linked module; exact flags come from `--help`.

| Entry point | Module | Inputs → outputs |
| --- | --- | --- |
| `reviewbench-build` | [build_dataset.py](../src/reviewbench_maintainability/build_dataset.py) | PR references/search or snapshots → candidate JSONL, optional report/snapshots |
| `reviewbench-merge` | [merge_candidates.py](../src/reviewbench_maintainability/merge_candidates.py) | Candidate JSONL files → deduplicated JSONL |
| `reviewbench-curate` | [curate_pool.py](../src/reviewbench_maintainability/curate_pool.py) | Candidates and filters → ranked pool/report |
| `reviewbench-evidence` | [evidence.py](../src/reviewbench_maintainability/evidence.py) | Tasks and exact-commit cache → enriched evidence JSONL |
| `reviewbench-quality` | [quality_gate.py](../src/reviewbench_maintainability/quality_gate.py) | Evidence plus online/imported judgments → task-quality decisions |
| `reviewbench-recovery` | [recovery.py](../src/reviewbench_maintainability/recovery.py) | Tasks and evaluations → recovery queue/report |
| `reviewbench-release` | [release.py](../src/reviewbench_maintainability/release.py) | Quality-gated tasks and Oracle evidence → selected release/report |
| `reviewbench-score` | [score_patch.py](../src/reviewbench_maintainability/score_patch.py) | Before/after directories and paths → static-metric comparison |
| `reviewbench-evaluate` | [evaluate.py](../src/reviewbench_maintainability/evaluate.py) | Task(s), patch(es), JSON Oracle configuration → gate results |
| `reviewbench-agent` | [agentic/cli.py](../src/reviewbench_maintainability/agentic/cli.py) | YAML + task/Oracle/recipe assets → prepare/run/evaluate/report artifacts |

Shared [jsonl.py](../src/reviewbench_maintainability/jsonl.py) centralizes the
non-agentic pipeline's UTF-8 JSONL readers/writers, preserving legacy aliases
and atomic replacement behavior. Agentic identity-validated JSONL contracts
remain separate.

Supporting modules: [models.py](../src/reviewbench_maintainability/models.py)
defines candidate records; [github_client.py](../src/reviewbench_maintainability/github_client.py)
accesses GitHub/snapshots; [heuristics.py](../src/reviewbench_maintainability/heuristics.py)
classifies signals; [static_metrics.py](../src/reviewbench_maintainability/static_metrics.py)
computes directional evidence. Candidate schema version is
`reviewbench-maintainability/v0.1`; it is not an agent scoring version.

## Agentic module map

All paths below are relative to `src/reviewbench_maintainability/agentic/`.

| Modules | Responsibility |
| --- | --- |
| `contracts.py`, `__init__.py` | Versioned task/runtime contracts, canonical identities, atomic serialization |
| `cli.py`, `preparation.py` | YAML/overrides, input selection, readiness and start/golden verification |
| `runner.py`, `models.py`, `environment.py` | Optional mini-agent loop, model adapters/budgets, isolated tool execution |
| `snapshots.py`, `sources.py`, `assets.py` | Exact starting trees, trusted asset materialization and patch collection |
| `execution.py`, `resources.py`, `scheduling.py` | Execution services, resource admission, concurrency and output locks |
| `storage.py`, `archives.py`, `retention.py` | Content-addressed storage, archive validation and retention |
| `evaluation.py`, `judge_policy.py`, `reporting.py` | Fixed correctness, explicit judge policies and fixed-denominator reports |

Importing the package does not require the optional agent/model stack. Loading an
agent YAML needs PyYAML; actual agent execution needs the pinned mini agent and
runtime assets. Configured model names, output directories and engine identities
must not be silently substituted during resume.

## Retained script families

Retention follows dependency closure, not file age or naming. All established
tracked tools remain, along with newer runtime helpers required by retained
callers/tests. The [retirement manifest](../scripts/retired-tools.json) records
dependency and removal decisions; it is the inventory reference, not a claim that
only the originally tracked scripts exist.

Examples (names preserved, dependencies may include other retained helpers):

- Mining/annotation: `mine_scale.py`, `prefilter_candidates.py`, `enrich_cases.py`,
  `annotate_with_copilot.py`, `apply_annotations.py`.
- Oracle construction: `validate_oracles.py`, `validate_hard_wave.py`,
  `collect_hard_oracle_passes.py`, `freeze_hard_oracle_pool.py`.
- Historical generation/evaluation: `generate_release_patches.py`,
  `generate_agentic_patches.py`, `run_open_model_agentic.py`,
  `run_open_model_correctness.py`, `run_legacy_reserve_correctness.py`.
- Audit/report/package: `audit_judge_panel.py`, `compare_arms_paired.py`,
  `summarize_four_gate.py`, `package_agentic_evaluation.py`, `package_hard_release.py`.

These scripts span multiple historical protocols. In particular,
`generate_agentic_patches.py` is not the installed `reviewbench-agent` executor.
Read each script's arguments and its experiment manifest before reproduction.
The [archive](archive/README.md) preserves relevant historical caveats.

## Test map

Core tests include `test_heuristics.py`, `test_curate_pool.py`,
`test_merge_candidates.py`, `test_evidence.py`, `test_quality_gate.py`,
`test_release.py`, `test_recovery.py`, `test_static_metrics.py` and `test_evaluate.py`.
Agentic protocol coverage starts in `test_agentic_exec.py`; retained source,
storage, runtime and script tests extend it. Tests that need Docker/model-adapter
integration are opt-in, not a claim of live benchmark completion. Use
[development commands](DEVELOPMENT.md#tests), not counts copied from old run logs.
