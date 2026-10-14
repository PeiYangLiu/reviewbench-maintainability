# Historical archive

**These are frozen historical references, not current package documentation.**
Words such as “current”, “latest”, “complete” and proposed next steps inside an
archived document refer to its own date. Read corrections and incompleteness
notices before using a table. No new model evaluation was performed by moving it.

Start with [current docs](../README.md), [current judge policies](../AGENTIC_EXEC_V1.md#judge-policies)
or the [migration/removal index](../ARCHIVE_MIGRATION.md).

## Methodology, provenance and protocols

- [DATA_PROVENANCE.md](DATA_PROVENANCE.md)
- [PIPELINE.md](PIPELINE.md)
- [AGENTIC_EXEC_V1.md](AGENTIC_EXEC_V1.md)
- [AGENTIC_402_PROTOCOL_DECISIONS.md](AGENTIC_402_PROTOCOL_DECISIONS.md)
- [AGENTIC_GPU_V2.md](AGENTIC_GPU_V2.md)
- [OPEN_MODEL_EVALUATION.md](OPEN_MODEL_EVALUATION.md)
- [ARM_NAMING.md](ARM_NAMING.md)
- [BUG_AUDIT_20260904.md](BUG_AUDIT_20260904.md)

`DATA_PROVENANCE.md` and `PIPELINE.md` preserve the construction lineage and
historical selection funnels. `AGENTIC_EXEC_V1.md` describes an earlier snapshot
of the executable protocol (including its then-three-judge rule); it does not
override the current versioned policy. GPU/open-model documents preserve run and
protocol provenance, not a promise that their old orchestration scripts ship.
`AGENTIC_402_PROTOCOL_DECISIONS.md` is a **proposal**, not proof it was executed.
`BUG_AUDIT_20260904.md` preserves qualifications needed to interpret SFT/RL tables.

## Frozen result reports

- [AGENTIC_399_RELEASE_CANDIDATE.md](AGENTIC_399_RELEASE_CANDIDATE.md) — historical report only; its ignored candidate package is **not included**.
- [LATEST_MODEL_LEADERBOARD_20260815.md](LATEST_MODEL_LEADERBOARD_20260815.md)
- [LATEST_MODEL_LEADERBOARD_V2_20260829.md](LATEST_MODEL_LEADERBOARD_V2_20260829.md)
- [LATEST_MODEL_LEADERBOARD_V2_8K_20260901.md](LATEST_MODEL_LEADERBOARD_V2_8K_20260901.md)
- [LATEST_MODEL_PARTIAL_LEADERBOARD_20260813.md](LATEST_MODEL_PARTIAL_LEADERBOARD_20260813.md)
- [LATEST_MODEL_PARTIAL_LEADERBOARD_20260815.md](LATEST_MODEL_PARTIAL_LEADERBOARD_20260815.md)
- [MAIN_14_MODEL_FOUR_GATE_402.md](MAIN_14_MODEL_FOUR_GATE_402.md)
- [MT_SFT_FOUR_GATE_20260829.md](MT_SFT_FOUR_GATE_20260829.md)
- [QWEN27B_THREE_REPLICATE_VARIANCE.md](QWEN27B_THREE_REPLICATE_VARIANCE.md)
- [RL_SIX_ARM_FOUR_GATE_392.md](RL_SIX_ARM_FOUR_GATE_392.md)
- [RL_SIX_ARM_FOUR_GATE_402.md](RL_SIX_ARM_FOUR_GATE_402.md)
- [SFT_EIGHT_ARM_FOUR_GATE_383.md](SFT_EIGHT_ARM_FOUR_GATE_383.md)
- [SFT_EIGHT_ARM_FOUR_GATE_402.md](SFT_EIGHT_ARM_FOUR_GATE_402.md)

## Comparison rules

- Historical line-edit `v1` and `v2` results are not executable-agent `v1` results.
- Three-judge historical tables are not no-Gemini or Astra-only measurements.
  The variance experiment also declares its own distinct panel.
- A report saying “402 attempted” can still have `complete=false`; do not convert
  missing infrastructure or judge measurements into model failures.
- Partial/withdrawn tables stay labeled as such. The 408-task snapshot was
  withdrawn; 368-, 383-, 392-, 398-, 402- and 414-task figures are not interchangeable.
- Numerical evidence and recorded caveats are preserved. Original unmodified
  document bytes and removed incident logs are available only in `BASELINE`.
