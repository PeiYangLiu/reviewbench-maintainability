# ReviewBench contributor instructions

## Scope and structure

- Maintain the installable package in `src/reviewbench_maintainability/`.
- `scripts/` contains supported construction, evaluation and reporting tools;
  see its README before adding another standalone entry point.
- Use `docs/README.md` as the documentation index. Historical results belong
  under `docs/archive/`, not in the main quick start.
- Do independent, disjoint work in parallel; do not edit the same files
  concurrently or duplicate completed evaluations.

## Changes and verification

- Keep changes small and behavior-preserving; retain public CLI and JSON contracts.
- Run `python -m pytest` after installing `.[dev,agentic]`.
- Never launch model evaluation, cloud jobs or services as part of unit tests.
- Do not alter frozen releases, Oracle tests, score versions, model identities,
  consumed attempts or recorded outcomes to improve results.
- Runtime migration requires a copy, original environment/command reuse,
  resource admission and a real end-to-end check before any fan-out.
- Report actual command results; unit tests are not completed benchmark runs.
- Keep credentials, machine-local deployment profiles, runtime caches and run
  diaries out of Git. Use environment variables and ignored local overrides.
- Preserve original data and a tested rollback when reorganizing the repository.
