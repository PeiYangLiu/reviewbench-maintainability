# Fixed modern Go Oracle adapters

The Go entry point remains `go_lane.migrate(ctx, task, oracle)`. No main CLI,
engine, Python lane, native lane, model configuration, or scope list is changed.
All recipes request **2 CPUs / 4096 MiB** and retain whole-candidate package
builds. Configuration inspection is not an executed runtime verification.

## Contracts implemented

| Family | Original contract | Retained semantics |
| --- | --- | --- |
| Direct `go test` | Existing session command and injected test | Same argv, including paired/equals build flags and test selectors |
| Inline module shell | Optimism `bash -lc 'cd op-chain-ops && go test …'` | Original shell command; module-specific compiler/proxy/public compile target |
| Module pin + test | Bigtable `run_oracle.sh` | Exact genproto require, `go mod download all`, original policy assertions |
| CLI process boundary | go-azure-sdk | Original `fake-bin/az`, PATH seen by `az`, token assertions, exit status |
| Generated package | Poktroll | Original conditional Ignite/mockgen commands against **current** proto/interface inputs |
| Local replace boundary | weave-gitops-enterprise | Original credentials boundary and `go mod edit -replace`, unchanged help assertions |
| Covered public CLI | yaprmtrc | Whole `./cmd/agent` build, rejected argument, nonzero exit, diagnostic and covdata |
| Automatic public package | Named original automatic run evidence | Original full-package command and six-run stable intersection; no count-derived identities |

The five retained shell files are SHA256-selected profiles in `go_contracts.py`.
An unknown wrapper or changed positional-argument contract stays pending.
`tests/fixtures/go_wrappers` contains the original small wrappers, session test
sources, specs and boundary fixtures only—not repository production snapshots,
compiler distributions, weights, or generated protobufs.

### Runtime transport

* Grader files are copied from immutable assets into a task-local writable
  directory before a wrapper can `chmod` its fake process boundary.
* The Bash entry exports a Go function for wrappers which reset PATH. `exec go`
  still finds the same compiler through the prepended pinned `bin` directory.
* The CLI's absolute Go 1.24.10 pathname is relocated, not upgraded. A runtime
  bootstrap checks the exact observed Go version, including shared archives.
* Poktroll's original `git checkout -- go.mod go.sum` would erase uncommitted
  candidate manifest edits. Only that cleanup operation is transported:
  restore the candidate's pre-generation manifest bytes instead of Git HEAD.
  Original tests/selectors/assertions are untouched.
* Optimism's original 73-byte `genesis_compat.go` boundary is needed by the
  public compile check as well as grading. Its exact hash and destination are
  allowlisted. Existing candidate production files are never overwritten.
* Developer assets include only compilers, dependency data, original compile
  boundaries and generation tools. Behavioral tests and signatures stay grader
  assets. Candidate production source is never staged from an old checkout.

## Dependency inputs

`go_dependencies.py` reads `go.mod`/`go.sum` and active `go.work`/`go.work.sum`
from the frozen start commit and
applies **only manifest hunks** from the already pinned source-only golden patch
to a metadata-only temporary directory. It traverses candidate-relative local
replacements and all active workspace `use` modules, and preserves start/golden
lock versions and checksum conflicts. Optimism's retained workspace contains
19 modules; it is not silently reduced to `op-chain-ops` or disabled with
`GOWORK=off`. An explicitly retained `GOWORK=off` setting is still honored.
No production `.go` file is extracted into that directory or dependency assets.

The file proxy includes wrapper-explicit version pins and their exact-version
`.mod` graph. It does not resolve `@latest`. Known `.mod`/ZIP content is checked
against the original `h1` checksums (Go `dirhash.Hash1`, not ZIP byte SHA256).
`go-proxy-inputs.json` records original manifest hashes, version origins,
original checksum when present, observed content checksum and asset byte hash.
A wrapper-only pin absent from the old go.sum is explicitly recorded as such;
its newly staged immutable bytes are not described as an old checksum.

Offline dependency closure, tool availability, generated-code compatibility,
and start/golden runtime success still require actual preparation/verification.
A missing exact compiler or incompatible current public API stays a real gap;
no dependency or assertion is replaced to manufacture a passing replay.

## Automatic contracts with no session-spec

The five current automatic rows (ibc-go, mongodb-atlas-kubernetes, Percona,
Sourcegraph, Lattigo) retain counts but not named per-run results in their
automatic shard JSONL. Counts and the recovery controller's descriptive
“N tests; node IDs were not retained” string are not selectors.

The adapter accepts a separately pinned **actual** original-run evidence file
through configuration, without writing `session-spec.json`:

```yaml
migration:
  # Existing experiment fields, especially verification_engine_fingerprint,
  # remain unchanged.
  go_original_runs:
    TASK_ID:
      path: /absolute/path/to/actual-original-go-runs.json
      sha256: SHA256_OF_THAT_FILE
  go_toolchains:
    1.24.10:
      path: /absolute/path/to/pinned-toolchain-archive-descriptor.json
      sha256: SHA256_OF_THAT_DESCRIPTOR
```

`reviewbench-go-original-runs/v1` requires:

* `id`, `repo`, `start_sha`, `golden_patch_sha256` matching the published inputs;
* `evidence_kind`: `retained-original-runs` or `original-harness-replay`;
* nonempty `harness`: existing original harness `{path, sha256}` references;
* the original uncached verbose single-package `command`, candidate-relative
  `cwd` (`{repo}` or `{repo}/module`), `go_version`, actual `go_version_output`;
* `runs.start` and `runs.golden`, each with runs numbered 1, 2, 3, the same
  original `command` and `cwd`, literal `exit_code`, and `log: {path, sha256}`;
* for a **new** original-harness replay, `engine_fingerprint` equal to the
  experiment's frozen fingerprint. Such evidence is not a historical session.

The adapter checks hashes, task/golden binding, all six logs, named stable
intersection, and exact published P2P count. It neither invents test names nor
recovers identities from a candidate being graded. Records lacking these inputs
stay `go_automatic_original_runs_missing`.

`go_public_test_report.go` adds only Go's JSON event transport to the original
package command. It requires all fixed named tests and package completion.
Unselected sibling failures retain the original `trackA_go`/`evaluate_e2e`
named-result semantics. Missing/skipped/failed selected tests, build failures,
incomplete output, and stale reports do not pass.

## Inspection and regression tests

`scripts/inspect_go_runtime_contracts.py` reads the complete remaining-gap file
and emits Go configuration rows while preserving `scope`, `remaining_input`,
and **all** original gap IDs. It performs no network fetch, container launch,
model invocation, or live registration. A supported contract is reported
separately from compiler availability and `runtime_verified: false`.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:scripts "$PY" -m pytest -q \
  -p no:cacheprovider tests/test_agentic_go_wrappers.py \
  tests/test_agentic_go_dependencies.py tests/test_agentic_go_public_inputs.py \
  tests/test_agentic_lane_migration.py
```

Subprocess coverage includes all six real shell families, PATH-reset and `exec`
dispatch, original CLI negative assertions, generator cleanup of candidate
locks, a real Go whole-package neighboring-source mutation, real offline Go
checksum agreement, exact new dependency closure, and named-result reporting.

For actual runtime verification, use the experiment's shared gate with one
worker and `REVIEWBENCH_ENGINE_SRC` set from its `engine-src.txt`. Existing
`migrate_agentic_runtimes.py` accepts `--migration-ids`, `--languages Go`,
`--workers 1`, and `--verify` without reducing the experiment's declared scope.
Never substitute the private worktree's current `src` for the frozen engine.
