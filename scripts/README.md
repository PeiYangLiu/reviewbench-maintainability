# Script inventory

The publication copy retains **296 original script files**, including all **57
originally tracked commands**, their transitive local dependencies, and reusable
preparation, migration, evaluation, recovery, and reporting helpers.

- `agentic_runtime_migration/` and `agentic_templates/` are retained with their
  runtime assets. Retained scripts are byte-for-byte unchanged. The retained CLI scheduling
  unit test now uses a private resource ledger and simulated host capacity,
  without changing production resource limits or test assertions.
- **281 untracked script files** are retired: historical cloud/campaign
  orchestration and versioned continuation tools outside the retained dependency
  closure. A versioned filename alone is not a deletion rule.
- **292 original test/fixture files** remain; **192 orchestration-only test/fixture
  files** are retired with their targets. Local package coverage and tests of
  retained commands remain. Retirement is not based on test failures.

[`retired-tools.json`](retired-tools.json) inventories every original file in the
cleanup scope, with status, rationale, SHA-256, dependency paths, and the AST/string
audit summary. Detailed reference evidence is held with the external transaction. Counts exclude these two new inventory files and the
separately scoped `tests/test_jsonl.py`.

All retired originals and cleanup verification/rollback records are retained in
the external publication transaction, **not duplicated in this repository**.
Static analysis cannot prove arbitrary computed imports, so uncertain helper
bundles are retained. Preserved tools still require their usual optional runtime
dependencies and, where applicable, external historical inputs.
