# ReviewBench-Maintainability

Tools for building and evaluating a maintainability benchmark from real GitHub
pull-request reviews. Tasks pair a reviewer's concern with an exact starting
revision and evidence of an accepted human change. Functional correctness and
maintainability are separate gates; static metrics alone are not a quality oracle.

## Install

Python **3.10+** and Git are required. The core package has no mandatory Python
runtime dependencies. Mining live GitHub data also requires the authenticated
[GitHub CLI](https://cli.github.com/).

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
reviewbench-build --help
```

For development and the full test suite, use `.[dev,agentic]` (see below).
Executable-agent runs use Python **3.12**, Docker, and the optional agent dependencies:

```bash
python -m pip install -e '.[agentic]'
reviewbench-agent --help
```

The extra pins `mini-swe-agent==2.4.6` and `tomli-w==1.2.0`; YAML configuration
loading requires **PyYAML**, explicitly included in the agent extra. For
reproducible agent environments use the hash-pinned
[`requirements-agentic-py312.lock`](requirements-agentic-py312.lock), as described
in [development](docs/DEVELOPMENT.md).

## Installed commands

All ten entry points are declared in [`pyproject.toml`](pyproject.toml); run any
command with `--help` for its current arguments.

| Command | Purpose |
| --- | --- |
| `reviewbench-build` | Mine review signals or rebuild from saved API snapshots |
| `reviewbench-merge` | Merge and deduplicate candidate JSONL |
| `reviewbench-curate` | Filter and rank a candidate pool |
| `reviewbench-evidence` | Extract exact-revision before/patch evidence |
| `reviewbench-quality` | Apply or import independent task-quality judgments |
| `reviewbench-recovery` | Classify unsuccessful Oracle evaluations for recovery |
| `reviewbench-release` | Apply strict task-release gates and diversity selection |
| `reviewbench-score` | Compare local before/after static metrics |
| `reviewbench-evaluate` | Evaluate supplied patches with a configured Oracle |
| `reviewbench-agent` | Prepare, run, evaluate and report executable-agent episodes |

Scripts are separate from these ten installed commands. The retained set includes
established tools and required newer runtime helpers; see the
[codebase guide](docs/CODEBASE_GUIDE.md#retained-script-families).

## Examples

Run from the repository root. Output directories below are local working data,
not additional published releases.

```bash
# Mine a PR; gh auth status must succeed first.
reviewbench-build --pr 'psf/requests#6500' \
  --out outputs/candidates.jsonl --report outputs/mining-report.json \
  --snapshot-dir outputs/snapshots

# Rebuild saved snapshots without querying GitHub.
reviewbench-build --from-snapshot outputs/snapshots \
  --out outputs/rebuilt.jsonl

# Compare existing source trees (directional metrics, not a full Oracle).
reviewbench-score --before-dir examples/before --after-dir examples/after \
  --paths src/example.py --category complexity

# Evaluate your task, patch and JSON Oracle configuration.
reviewbench-evaluate --task inputs/task.json --patch outputs/model.patch \
  --oracle-config inputs/oracles.json --work-dir outputs/work \
  --out outputs/evaluation.json
```

The last two examples require your own input files. The generic patch evaluator
requires real correctness commands and does not directly replay every historical
release Oracle. See [architecture](docs/ARCHITECTURE.md) for its input contract and
[the agent guide](docs/AGENTIC_EXEC_V1.md) for isolated multi-file execution.

## Protocols and scoring

The installed agent implements `reviewbench-agentic-exec/v1`. Frozen
`agentic-line-edit/v1` and `agentic-line-edit/v2` experiments are separate protocols.

**Both shipped agent YAML configurations currently select
`reviewbench-judge-panel/astra-only-20261011-v1` with `gpt-6-astra`.** The package
also supports the explicitly versioned two-member
`reviewbench-judge-panel/no-gemini-3.6-20261011-v1` and the legacy three-judge policy.
These are distinct measurements, not interchangeable fallbacks. Reports reject
mixed score versions or rosters; missing judgments are not passing votes.
See [judge policies](docs/AGENTIC_EXEC_V1.md#judge-policies) for exact rules.

## Data and documentation

Frozen releases live under [`data/releases/`](data/releases/), including the
100-task [`v1`](data/releases/v1/) and 414-task
[`hard/20260808`](data/releases/hard/20260808/) artifacts. Their manifests and
checksums describe historical releases, **not a current Astra-only leaderboard**.
Runtime images, source caches, credentials and private run artifacts are not
bundled with the Python package.

- [Documentation index](docs/README.md) — authoritative current reading order
- [Codebase guide](docs/CODEBASE_GUIDE.md) — package, scripts and tests
- [Construction methodology](docs/PIPELINE.md) and [data provenance](docs/DATA_PROVENANCE.md)
- [Development and tests](docs/DEVELOPMENT.md)
- [Historical reports/protocols](docs/archive/README.md) — dated, sometimes partial or withdrawn
- [Documentation migration/removal index](docs/ARCHIVE_MIGRATION.md)

## Tests

```bash
python -m pip install -e '.[dev,agentic]'
PYTHONPATH=src python -m pytest -q
```

The full suite includes retained agentic tests and therefore installs the mini-agent
stack as well as pytest/PyYAML/build. `.[dev]` alone is for the focused core tests
listed in [development](docs/DEVELOPMENT.md#tests), not the full suite. Tests do
not establish live provider availability or benchmark completion. Real Docker
integration is opt-in via `REVIEWBENCH_DOCKER_TESTS=1`; see
[development](docs/DEVELOPMENT.md#tests) for prerequisites and focused commands.

## Limits

Mining and environment selection introduce sampling bias. Judge/model versions,
protocols, budgets, task IDs and denominators must accompany comparisons. A
historical golden patch is evidence, not the only valid implementation; fixed
behavioral tests must remain independent of its private structure.
