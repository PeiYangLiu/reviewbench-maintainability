# Development

[Documentation index](README.md) · [Codebase guide](CODEBASE_GUIDE.md)

Run commands from the repository root. Keep generated data, worktrees, provider
credentials, deployment receipts and dependency caches out of source control.
Independent tasks/tests may run in parallel when their outputs and resource
budgets do not conflict; do not duplicate completed model requests for parallelism.

## Environments

The core package supports Python 3.10+ and requires Git. For the full retained
test suite, install **both** extras: `dev` supplies pytest/PyYAML/build, and
`agentic` supplies the mini-agent stack required by agentic tests. This matches
the CI installation; a pure `.[dev]` environment is not the full-suite default.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,agentic]'
```

Python 3.12 is the reference environment for executable-agent runs. Docker and
audited runtime assets are additionally required for actual episodes; installing
both extras does not provide those assets. A smaller `.[dev]` installation is
sufficient for the focused core tests below, but not the full retained suite.

For experiments requiring a reproducible agent dependency stack, create a clean
Python 3.12 environment and use the checked-in lock rather than resolving newer
transitive dependencies:

```bash
python3.12 -m venv .venv-agentic
. .venv-agentic/bin/activate
python -m pip install --require-hashes -r requirements-agentic-py312.lock
python -m pip install --no-deps -e .
reviewbench-agent --help
```

The lock targets Python 3.12; it is not a promise of identical resolution on all
Python versions/platforms. `mini-swe-agent==2.4.6` is checked at runtime. YAML
loading needs PyYAML even for configuration-driven preparation/reporting that
never calls a model. A help-only smoke test does not test that import path.

## Tests

The full-suite command assumes the `.[dev,agentic]` environment above. The focused
core command can run with just `.[dev]`; it does not exercise the mini-agent loop.

```bash
# Full retained offline suite; Docker integrations remain opt-in.
PYTHONPATH=src python -m pytest -q

# Fast core checks while editing construction/evaluation logic.
PYTHONPATH=src python -m pytest -q \
  tests/test_heuristics.py tests/test_curate_pool.py \
  tests/test_merge_candidates.py tests/test_evidence.py \
  tests/test_quality_gate.py tests/test_release.py tests/test_recovery.py \
  tests/test_static_metrics.py tests/test_evaluate.py

# Executable-agent protocol unit tests.
PYTHONPATH=src python -m pytest -q tests/test_agentic_exec.py
```

Use `PYTHONPATH=src` to ensure imports resolve to this checkout, not an unrelated
installed copy. Pytest is required: unittest discovery alone omits pytest-style
agentic tests. Retained script tests may use fixture subprocesses/local HTTP
servers and installed Go/Rust/Node/C/C++ toolchains; these are not live model
evaluations. Some compiler-backed fixtures can take several minutes. The CLI
scheduling unit test uses a private ledger and injected host capacity so it
cannot wait on or reserve production resources.

Historical Gradle profile tests additionally require
`LEGACY_GRADLE_PROFILE_ROOT` pointing to the original archived `profiles/` and
their hash-bound inputs. Only those fixture-dependent cases skip when the variable
is unset; configured but missing/corrupt inputs still fail. Their local unit
checks continue to run without the archive.

For the real Docker integration fixtures, install the agent extra, have a working
Docker daemon, and make the `python:3.11` image available first:

```bash
docker pull python:3.11
REVIEWBENCH_DOCKER_TESTS=1 PYTHONPATH=src \
  python -m pytest -q tests/test_agentic_exec.py
```

These opt-in tests use a deterministic local provider but real mini-agent/Docker
execution. Passing them does not establish remote API access, production runtime
readiness or a completed benchmark. Report skipped tests and failed checks rather
than quoting an old all-tests-passed count.

## Build and smoke test

```bash
python -m build
reviewbench-build --help
reviewbench-agent --help
```

Wheel/sdist creation validates packaging, not bundled benchmark environments.
For release data, run the checksum commands in [data provenance](DATA_PROVENANCE.md).
For actual episodes, use the [agent guide](AGENTIC_EXEC_V1.md) and verify each
runtime before generation.

## Change boundaries

- Reusable behavior belongs in `src/`; preserve public entry points and the
  established script names used by frozen manifests and regression tests.
- A change to execution, scoring, source identity or runtime assets needs focused
  tests and a compatible identity/version policy. Do not hot-edit frozen runs.
- Tests should cover missing/duplicate evidence, foreign IDs, mismatched hashes,
  incomplete panels and configuration errors, not just the happy path.
- Use environment-variable names/placeholders in examples, never credentials or
  personal absolute paths. Treat provider IDs as explicit configuration.
- Keep ongoing documentation concise. Store publishable protocol/data evidence
  under an explicitly historical archive when superseded, not in a growing
  progress/debug diary. Update [the index](README.md) when moving a document.

## GitHub CI template

The [test/build workflow template](../.github/tests.yml.example) covers Python
3.10 and 3.12. It is not active because the publication credential lacks GitHub's
`workflow` scope. See [activation instructions](../.github/README.md). Local
verification results must not be presented as an observed GitHub Actions run.
