# Configuration reference

- `agentic-exec-v1.yaml` and `agentic-exec-v1.gateway.yaml` configure the
  installed `reviewbench-agent` CLI. Set the referenced `REVIEWBENCH_*`
  environment variables and supply audited runtime recipes before execution.
  Their explicit judge roster and `score_version` are part of the evaluation
  contract; do not combine them with historical leaderboard results.
- `amlt_*.yaml` are preserved, versioned open-model experiment presets. They
  require the external AMLT CLI, access to the named cloud resources, and the
  model/cache environment variables referenced by each preset. They are not
  required to install or test the package. Model revisions, resource settings
  and retry scopes are retained for reproducibility, not automatically updated.
- `openmodel_scope_ids/` and `openmodel_retry_ids/` contain the exact historical
  task selections consumed by those presets.

Machine-specific Azure Batch submissions and systemd progress services are not
part of the distributable benchmark. The repository cleanup snapshot retains
those original files; no service or evaluation is launched during cleanup.
Put new deployment overrides in ignored `*.local.*` files. Never commit API
keys, storage credentials or `.env` files.
