# Documentation migration and removal index

This index records the publication cleanup, not experiment progress. The original
snapshot is retained separately as `BASELINE`; paths below are relative to that
backup. The backup is **not** part of the publication repository. Recover an old
file from the same relative path there; do not reconstruct a run from this index.

- Current entry points are the root README and [documentation index](README.md).
- Selected methods, provenance, protocol descriptions and frozen result reports
  moved to [the historical archive](archive/README.md). Dates, denominators,
  withdrawals and incomplete-result caveats remain historical, not current claims.
- Run diaries, dated status/weeklies, one-task repairs, deployment recipes,
  progress partitions and recovery logs were removed rather than copied into Git.
  Their original bytes and local execution evidence remain in the backup.
- `PITFALLS.md` mixed methodological lessons with a long incident diary; durable
  lessons now live in [the pipeline guide](PIPELINE.md), while the complete original
  remains backup-only. No archived report is evidence that a bug is still open today.
- Archive copies add a historical notice, repair clickable links and replace
  workstation-root identities with `${REVIEWBENCH_WORKSPACE}`. Links to absent
  artifacts are rendered as labeled references, not presented as runnable assets.
- A link-only compatibility page remains at `docs/PROGRESS_20260714.md` because
  the immutable v1 release README points there. Its original diary content is
  removed; the frozen release files need not change to keep that link working.
- This index covers **README/docs only**. It does not rename scripts, rewrite
  release bytes, change a judge roster or claim that a remote repository exists.

## Rewritten entry points

| Original path in backup | Current role |
| --- | --- |
| `README.md` | Installation, ten installed CLI, examples and test overview |
| `docs/CODEBASE_GUIDE.md` | Authoritative source and entry-point map |
| `docs/DATA_PROVENANCE.md` | Published artifacts and integrity checks; original archived |
| `docs/PIPELINE.md` | Current construction methodology; original archived |
| `docs/AGENTIC_EXEC_V1.md` | Current executable protocol and versioned panels; original archived |
| `docs/AGENTIC_EVALUATION_REUSE_PRINCIPLES.md` | Durable reuse rules, without incident updates |

## Historical documents retained

| Original path in backup | Publication path |
| --- | --- |
| `docs/AGENTIC_399_RELEASE_CANDIDATE.md` | [archive/AGENTIC_399_RELEASE_CANDIDATE.md](archive/AGENTIC_399_RELEASE_CANDIDATE.md) (report only; candidate data not included) |
| `docs/AGENTIC_402_PROTOCOL_DECISIONS.md` | [archive/AGENTIC_402_PROTOCOL_DECISIONS.md](archive/AGENTIC_402_PROTOCOL_DECISIONS.md) |
| `docs/AGENTIC_EXEC_V1.md` | [archive/AGENTIC_EXEC_V1.md](archive/AGENTIC_EXEC_V1.md) |
| `docs/AGENTIC_GPU_V2.md` | [archive/AGENTIC_GPU_V2.md](archive/AGENTIC_GPU_V2.md) |
| `docs/ARM_NAMING.md` | [archive/ARM_NAMING.md](archive/ARM_NAMING.md) |
| `docs/BUG_AUDIT_20260904.md` | [archive/BUG_AUDIT_20260904.md](archive/BUG_AUDIT_20260904.md) |
| `docs/DATA_PROVENANCE.md` | [archive/DATA_PROVENANCE.md](archive/DATA_PROVENANCE.md) |
| `docs/LATEST_MODEL_LEADERBOARD_20260815.md` | [archive/LATEST_MODEL_LEADERBOARD_20260815.md](archive/LATEST_MODEL_LEADERBOARD_20260815.md) |
| `docs/LATEST_MODEL_LEADERBOARD_V2_20260829.md` | [archive/LATEST_MODEL_LEADERBOARD_V2_20260829.md](archive/LATEST_MODEL_LEADERBOARD_V2_20260829.md) |
| `docs/LATEST_MODEL_LEADERBOARD_V2_8K_20260901.md` | [archive/LATEST_MODEL_LEADERBOARD_V2_8K_20260901.md](archive/LATEST_MODEL_LEADERBOARD_V2_8K_20260901.md) |
| `docs/LATEST_MODEL_PARTIAL_LEADERBOARD_20260813.md` | [archive/LATEST_MODEL_PARTIAL_LEADERBOARD_20260813.md](archive/LATEST_MODEL_PARTIAL_LEADERBOARD_20260813.md) |
| `docs/LATEST_MODEL_PARTIAL_LEADERBOARD_20260815.md` | [archive/LATEST_MODEL_PARTIAL_LEADERBOARD_20260815.md](archive/LATEST_MODEL_PARTIAL_LEADERBOARD_20260815.md) |
| `docs/MAIN_14_MODEL_FOUR_GATE_402.md` | [archive/MAIN_14_MODEL_FOUR_GATE_402.md](archive/MAIN_14_MODEL_FOUR_GATE_402.md) |
| `docs/MT_SFT_FOUR_GATE_20260829.md` | [archive/MT_SFT_FOUR_GATE_20260829.md](archive/MT_SFT_FOUR_GATE_20260829.md) |
| `docs/OPEN_MODEL_EVALUATION.md` | [archive/OPEN_MODEL_EVALUATION.md](archive/OPEN_MODEL_EVALUATION.md) |
| `docs/PIPELINE.md` | [archive/PIPELINE.md](archive/PIPELINE.md) |
| `docs/QWEN27B_THREE_REPLICATE_VARIANCE.md` | [archive/QWEN27B_THREE_REPLICATE_VARIANCE.md](archive/QWEN27B_THREE_REPLICATE_VARIANCE.md) |
| `docs/RL_SIX_ARM_FOUR_GATE_392.md` | [archive/RL_SIX_ARM_FOUR_GATE_392.md](archive/RL_SIX_ARM_FOUR_GATE_392.md) |
| `docs/RL_SIX_ARM_FOUR_GATE_402.md` | [archive/RL_SIX_ARM_FOUR_GATE_402.md](archive/RL_SIX_ARM_FOUR_GATE_402.md) |
| `docs/SFT_EIGHT_ARM_FOUR_GATE_383.md` | [archive/SFT_EIGHT_ARM_FOUR_GATE_383.md](archive/SFT_EIGHT_ARM_FOUR_GATE_383.md) |
| `docs/SFT_EIGHT_ARM_FOUR_GATE_402.md` | [archive/SFT_EIGHT_ARM_FOUR_GATE_402.md](archive/SFT_EIGHT_ARM_FOUR_GATE_402.md) |

## Removed from publication; retained only in backup

| Original path in backup | Original bytes | Reason |
| --- | ---: | --- |
| `docs/AGENTIC_399_CURRENT_PROGRESS.md` | 22,368 | Local progress / admission diary |
| `docs/AGENTIC_402_PROGRESS.md` | 15,415,078 | Local progress / admission diary |
| `docs/AGENTIC_402_REMAINING_14.md` | 5,249 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_402_REMAINING_3.md` | 4,249 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_402_REMAINING_4.md` | 4,290 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_402_REMAINING_5.md` | 4,080 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_402_REMAINING_6.md` | 5,666 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_402_REMAINING_7.md` | 2,610 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_402_REMAINING_8.md` | 2,920 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_402_REMAINING_9.md` | 3,606 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_CACHE_SEED_PERMISSIONS.md` | 1,507 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_CLOSED_LOOP_REPAIR_20261013.md` | 7,675 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_CONTEXT_ADMISSION_RECOVERY.md` | 11,171 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_CSHARP_COMPATIBILITY.md` | 8,233 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_CSHARP_GRAPHQL3270.md` | 5,865 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_CSHARP_GRAPHQL3270_SDK6.md` | 5,629 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_CSHARP_ORIGINAL_CLI.md` | 1,923 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_CSHARP_ORIGINAL_SDK6.md` | 2,422 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_CSHARP_SQLCLIENT_ORIGINAL.md` | 2,625 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_FRAMEWORK_REPAIR_20261012.md` | 5,948 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_FRAMEWORK_REPAIR_20261013.md` | 4,882 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_GO_BIGTABLE_FIRST_EPISODE.md` | 1,822 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_GO_BIGTABLE_MODFILE.md` | 4,742 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_GO_METADATA_ISOLATION.md` | 1,124 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_GO_MODFILE_RECOVERY.md` | 7,347 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_GO_PUBLIC_COMPILE.md` | 1,995 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_GO_RELIC.md` | 4,325 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_INFLIGHT_CACHE_ADMISSION.md` | 1,819 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_JAVA_TESTCONTAINERS.md` | 4,724 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_JUDGMENT_RECOVERY.md` | 5,446 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_JVM_HOST_PATHS.md` | 774 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_JVM_ORIGINAL_CLI.md` | 1,778 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_JVM_TIMEFOLD.md` | 1,252 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_LEGACY_JS_SESSION_RUNTIME.md` | 1,751 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_LEGACY_YARN_DUAL_GRAPH.md` | 1,984 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_MATERIALIZE_ORIGINAL_APIS.md` | 3,913 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_MATERIALIZE_STATIC_BUILD_INPUTS.md` | 1,894 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_MEASUREMENT_RECOVERY.md` | 4,973 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NATIVE_API_COMPAT.md` | 5,414 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NATIVE_CONAN.md` | 3,556 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NATIVE_KOKKOS.md` | 4,732 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NATIVE_MQT.md` | 1,789 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NATIVE_MQT94.md` | 1,075 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NATIVE_RIPPLED.md` | 10,748 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NATIVE_RIPSIM.md` | 1,280 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NATIVE_SOLIDITY.md` | 1,022 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NATIVE_SOURCE_REUSE.md` | 1,398 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NATIVE_SUBMODULES.md` | 5,823 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NODE_ORIGINAL_FIXED_INSTALL.md` | 2,417 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NODE_PNPM.md` | 4,842 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_NODE_PNPM_HOVERKRAFT.md` | 6,819 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_ORIGINAL_LOCKED_WORKSPACES.md` | 1,838 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_ORIGINAL_SESSION_OVERLAYS.md` | 1,787 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_ORIGINAL_STANDALONE_SERVICES.md` | 2,323 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_PINNED_REGISTRY_TEST_TOOLS.md` | 1,485 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_PNPM_FIXED_WORKSPACES.md` | 2,350 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_PROGRESSIVE_ADMISSION_RETRY.md` | 4,111 | Local progress / admission diary |
| `docs/AGENTIC_PYTHON_LEGACY_CLOSURE.md` | 4,122 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_REJECTED_JUDGE_MEMBER_PROFILES.md` | 6,292 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RELAY_HTTP_RESOURCE.md` | 1,424 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUNTIME_MIGRATION.md` | 52,036 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUNTIME_SCOPE_REPAIR_20261013.md` | 2,895 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_ANCHOR.md` | 2,290 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_API_SELECTION.md` | 4,171 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_BITCOIN71.md` | 2,424 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_COMPOSABLE.md` | 3,533 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_FIRST_NEW_RESOLUTION.md` | 2,978 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_FUEL.md` | 1,623 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_GEAR.md` | 2,486 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_GRPC.md` | 2,961 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_KAMU.md` | 2,077 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_LIBP2P.md` | 1,836 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_MANIFEST_SERVICE.md` | 3,191 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_RECS.md` | 2,225 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_TEST_COMPAT.md` | 6,260 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_TIKV.md` | 2,244 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_VCX.md` | 2,708 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_VENDOR_TRANSPORT.md` | 1,829 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_VFS61.md` | 5,555 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_VFS61_FIRST_RESOLUTION.md` | 8,475 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_WASH_TEST_COMPAT.md` | 6,388 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_WASH_TEST_COMPAT_V2.md` | 3,443 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_RUST_ZCASH.md` | 2,813 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_SQLCLIENT_WIDE_CONTEXT.md` | 1,814 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_STORAGE_AND_CONCURRENCY.md` | 13,147 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_TESTCONTAINERS_DOCKER.md` | 4,920 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_TEST_FIXTURE_RETENTION.md` | 4,024 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_THROUGHPUT.md` | 14,594 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_V2_BATCH_RESCUE_20260925.md` | 24,475 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_VECTOR_ROOT_MANIFESTS.md` | 2,079 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_VERIFICATION_ADMISSION_RETRY.md` | 1,582 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_VUE_ORIGINAL_RUNTIME.md` | 2,534 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_WORKER_IDENTITY_REPAIR_20261013.md` | 3,085 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_YARN1_PREPARATION.md` | 2,054 | Local deployment, runtime repair or recovery record |
| `docs/AGENTIC_YARN1_RUNTIME.md` | 2,500 | Local deployment, runtime repair or recovery record |
| `docs/AZURE_BATCH_BULK_SUBMISSION.md` | 81,597 | Local deployment, runtime repair or recovery record |
| `docs/AZURE_BATCH_DOCKER_SUBMISSION.md` | 76,447 | Local deployment, runtime repair or recovery record |
| `docs/AZURE_BATCH_PIPELINE_REPAIR_20261013.md` | 6,191 | Local deployment, runtime repair or recovery record |
| `docs/AZURE_BATCH_SCALE_BOTTLENECK_ROOT_CAUSE_ANALYSIS.md` | 21,223 | Local deployment, runtime repair or recovery record |
| `docs/BRIEFING_20260902.md` | 5,944 | Dated status or weekly snapshot |
| `docs/NATIVE_PACKAGE_RELEASE.md` | 4,535 | Local deployment, runtime repair or recovery record |
| `docs/PITFALLS.md` | 238,179 | Mixed incident diary; durable lessons extracted |
| `docs/PROGRESS_20260714.md` | 11,257 | Original diary removed; publication path is a link-only compatibility page |
| `docs/PROGRESS_20260728.md` | 17,208 | Dated status or weekly snapshot |
| `docs/PROGRESS_20260808.md` | 14,164 | Dated status or weekly snapshot |
| `docs/PROJECT_HEALTH_REMEDIATION_20261009.md` | 654,693 | One-off code-health audit / remediation diary |
| `docs/PROJECT_HEALTH_REVIEW_20261009.md` | 18,576 | One-off code-health audit / remediation diary |
| `docs/PROJECT_STATUS_20260826.md` | 22,576 | Dated status or weekly snapshot |
| `docs/WEEKLY_20260902.md` | 25,446 | Dated status or weekly snapshot |
| `docs/WEEKLY_REPORT_20260714.md` | 5,422 | Dated status or weekly snapshot |
| `docs/WEEKLY_REPORT_20260805.md` | 17,285 | Dated status or weekly snapshot |
| `docs/WEEKLY_REPORT_20260811.md` | 21,885 | Dated status or weekly snapshot |
| `docs/WEEKLY_REPORT_20260818.md` | 11,989 | Dated status or weekly snapshot |
| `docs/WEEKLY_REPORT_20260825.md` | 13,653 | Dated status or weekly snapshot |
| `docs/progress-archive/AGENTIC_402_PROGRESS.part01-20260920.md` | 16,884,772 | Local progress / admission diary |

Inventory: **21 historical documents retained**, **115 original documents retained only in backup** (one replaced by a link-only compatibility page).
