# Evaluation reuse principles

[Documentation index](README.md) · [Executable-agent guide](AGENTIC_EXEC_V1.md)

These are durable evaluation rules, not a deployment status log. Preserve the
original task/model identities, fixed tests, Oracle semantics, generation budget,
consumed attempts and evidence when moving or resuming a run.

1. **Reuse verified execution, not an approximation.** Recover the complete
   successful environment: source/image identities, dependencies/toolchains,
   provider/broker adapters, service images, mounts, configuration, environment
   variable names, command arrays and calibration evidence. Do not assume an image
   tag alone identifies a reproducible environment.
2. **Resume the first unfinished operation.** Keep completed generation,
   correctness and accepted judge replies (including vetoes), hashes, traces and
   attempt records. Never reset consumed budget, resample a failed answer or
   replay a completed gate to manufacture progress.
3. **Prove one end-to-end runtime before scaling it.** Inventory, image loading,
   a running worker or an offline unit test does not demonstrate generation,
   fixed correctness, judgment and artifact recovery on a new execution backend.
4. **Classify failures accurately.** Separate scheduling/quota, resources,
   images/assets, network/provider adapters, agent behavior, correctness, judging
   and archive/transport. Missing infrastructure is not a model zero; a real
   negative correctness/judge outcome is not an infrastructure retry opportunity.
5. **Keep admission explicit.** Check actual local and remote CPU, memory, disk,
   scratch and concurrency requirements independently. Reuse validated caches;
   don't weaken resource or test gates merely to fit a smaller worker. Parallelize
   independent work with distinct outputs and adequate capacity, not duplicate calls.
6. **Limit migrations to reviewed adapters.** Transport, controlled paths/network
   access and resource admission may need adjustment; retain source, tests, model
   identity, scoring and consumed state. Validate on a copy and retain rollback
   evidence rather than hot-editing frozen or running artifacts.
7. **Version measurement changes.** A new judge roster, rubric, input-coverage
   protocol or generation budget is not the old measurement. Keep explicit
   versions and separate reports. Current supported judge policies are documented
   [here](AGENTIC_EXEC_V1.md#judge-policies); old prose cannot override the config.
8. **Verify recovery before cleanup.** Preserve content hashes and durable
   receipts, reopen restored archives and verify that the restored inputs match
   before releasing local assets. Submission is not retrieval or full recovery.
9. **Report observed outcomes.** Distinguish submitted, prepared, generated,
   measured, imported and archived states, with literal errors/exit statuses when
   relevant. Do not turn intentions, service liveness or task counts into completed
   evaluation claims.

Local incident narratives and one-task recovery commands are retained in the
original backup, not appended here. See [the migration index](ARCHIVE_MIGRATION.md).
