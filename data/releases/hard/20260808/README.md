# ReviewBench 414-task Hard Release

This directory freezes 414 strict hard maintainability tasks and their
complete three-model agentic evaluation.

## Composition

- Previously published hard tasks: 40
- `wave-001`: 159 new hard tasks
- `wave-003`: 190 new hard tasks
- `wave-004`: 25 new hard tasks
- Total task-model pairs: 1242

Every task has an exact start revision, a source-only golden revision, three
stable runs on both sides, a non-empty PASS_TO_PASS oracle, and changed-old
production-line coverage.

The 40 previously published hard tasks use the conservative
published difficulty field. The new hard waves require a strict GPT quality
pass and at least one independent hard verdict; this avoids treating
uncalibrated cross-model confidence values as a hard threshold.

## Leaderboard

| Model | Generation | Scope | Correctness | Maintainability | Resolved@1 |
|---|---:|---:|---:|---:|---:|
| claude-opus-4.8 | 392 | 383 | 340 | 206 | **168/414 (40.6%)** |
| gemini-3.5-flash | 340 | 323 | 242 | 179 | **143/414 (34.5%)** |
| gpt-5.5 | 413 | 392 | 341 | 263 | **212/414 (51.2%)** |

Resolved@1 requires generation/apply, scope, executable correctness, and the
human-golden-calibrated maintainability rubric v2.

## Files

| File | Contents |
|---|---|
| `tasks.jsonl` | 414 strict hard tasks |
| `oracles.jsonl` | One strict executable Oracle record per task |
| `results.jsonl` | 1,242 normalized task-model gate results |
| `published-hard-results.jsonl` | The calibrated 40-task published-hard slice |
| `result-overrides.jsonl` | Sanitized runner-derived corrections applied during packaging |
| `report.json` | Aggregate leaderboard, breakdowns, and pairwise outcomes |
| `manifest.json` | Release contract, sources, counts, and hashes |
| `SHA256SUMS` | Integrity hashes |

Machine-specific roots in Oracle metadata are replaced with documented
environment-variable placeholders. Large build workspaces, dependency caches,
container layers, and raw logs are intentionally not included.

Verify the package from this directory:

```bash
sha256sum -c SHA256SUMS
```

## Release notes

- The three waves predate runtime fingerprints. Their legacy-continuity attestations verify current task, patch, correctness, judgment, and summary consistency, but do not claim cryptographic proof of the original invocation inputs.
- Wave-003 Claude generation recovered 94 of 95 aggregate-proxy failures with a fresh direct-endpoint run; one genuine max-round failure remained.
- Wave-001 replay cleared four judge transport errors and repaired six OpenAssetIO correctness infrastructure rows.
- The published Sniffy/Gemini API timeout was regenerated, judged, and replayed; the real candidate failed scope (737 changed lines) and compilation.
- The release uses strongest validated Oracle evidence; two earlier strict passes are retained after later flaky reruns produced weaker failures.
