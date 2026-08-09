# Open-weight model evaluation

更新时间：2026-08-09

本文记录 414-hard benchmark 的开源模型完整评测。评测先冻结 inference
artifact，再运行 correctness 和 maintainability；以后 rubric 改变时直接重判，
不重新运行 GPU inference。

## 1. AMLT experiment

| Field | Value |
|---|---|
| AMLT environment | local conda `amlt10` |
| AMLT version | 10.14.0 |
| Project | `reviewbench-maintainability` |
| Experiment | `reviewbench-qwen-hard414-v1` |
| Service | Singularity |
| Target | `msrresrchbasicvc` |
| Workspace | `Workspace_NLC` |
| Project storage | `conversationhub / amulet` |
| Code snapshot | `projects/reviewbench-maintainability/amlt-code/3be69de32e413569861e2f2fd691e4e7` |
| Task set | `data/releases/hard/20260808/tasks.jsonl` |
| Tasks | 414 |
| Protocol | `agentic-line-edit/v1`, maximum 32 rounds |

`msrresrch` is not used.

## 2. Model and resource matrix

All Hugging Face revisions are immutable commit SHAs.

| Job | Model / revision | SKU | TP | SLA |
|---|---|---|---:|---|
| `qwen25-coder-7b` | `Qwen/Qwen2.5-Coder-7B-Instruct@c03e6d358207e414f1eca0bb1891e29f1db0e242` | `40G1-A100` | 1 | Standard |
| `qwen25-coder-32b` | `Qwen/Qwen2.5-Coder-32B-Instruct@381fc969f78efac66bc87ff7ddeadb7e73c218a7` | `40G2-A100` | 2 | Standard |
| `qwen3-coder-30b-a3b` | `Qwen/Qwen3-Coder-30B-A3B-Instruct@b2cff646eb4bb1d68355c01b18ae02e7cf42d120` | `40G2-A100` | 2 | Standard |
| `qwen3-coder-next-fp8` | `Qwen/Qwen3-Coder-Next-FP8@da6e2ed27304dd39abadd9c82ef50e8de67bdd4c` | `40G4-A100` | 4 | Standard |
| `qwen3-coder-480b-a35b-fp8` | `Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8@003f183a92fbe5b9a8325aaa8b2ae797c91dd90f` | `80G8-A100-IB-NvLink` | 8 | Basic |

GPU selection follows:

```text
A100 40GB → A100 80GB → H100 → H200
```

The 480B FP8 weights do not fit on eight A100-40GB GPUs, so that job uses the
next preferred resource, eight A100-80GB GPUs.

## 3. Durable blob locations

Base URI:

```text
https://conversationhub.blob.core.windows.net/amulet/
```

Minimal exact-start Git cache:

```text
reviewbench-openmodels/repo-cache-minimal
```

The cache contains 340 repositories and only the exact task commit, trees, and
blobs needed by `git show`; parent history and build artifacts are excluded.

Per-job result roots:

| Job | Backend ID | Blob result path |
|---|---|---|
| `qwen25-coder-7b` | `affable_island_mx0ddy7h8k` | `projects/reviewbench-maintainability/amlt-results/7213701554.02094-8117827f-dfdc-45b6-8279-2604b8c3b45c` |
| `qwen25-coder-32b` | `teal_button_928dvnplm3` | `projects/reviewbench-maintainability/amlt-results/7213701554.01877-cea1ee78-505f-4d4f-bc85-4b9a81a3b7a9` |
| `qwen3-coder-30b-a3b` | `kind_island_83kzg8060m` | `projects/reviewbench-maintainability/amlt-results/7213701554.01671-5265586f-27bc-4ce7-9f17-9b219fe047ce` |
| `qwen3-coder-next-fp8` | `upbeat_thread_brps5lyvmh` | `projects/reviewbench-maintainability/amlt-results/7213701554.01472-b7b1ec57-af97-4b8b-9340-71ece19aee34` |
| `qwen3-coder-480b-a35b-fp8` | `mighty_leg_n8hq5y9m20` | `projects/reviewbench-maintainability/amlt-results/7213701554.01273-c08e49e3-e595-4021-ba91-ef711079f6be` |

## 4. Complete inference artifacts

Every job persists both live-synced files and a final
`open-model-generation.tar.gz`. The archive contains:

```text
generation/
  <model>/
    generation_results.jsonl
    generation_summary.json
    generation_complete.json
    patches/<task-id>.patch
    traces/<task-id>.json
vllm-server.log
open_model_manifest.json
```

Each task trace stores all rounds, including:

- prompt SHA-256;
- complete, untruncated raw model response;
- parsed action;
- tool result or tool error;
- token usage;
- response length and object count;
- final patch path and SHA-256;
- task fingerprint and duration.

`open_model_manifest.json` records:

- exact model ID and revision;
- vLLM server command;
- served-model identity response;
- vLLM, PyTorch, Transformers and Hugging Face package versions;
- tensor-parallel size, context length, dtype and GPU visibility;
- generation worker/round/token settings;
- start/end timestamps and terminal status.

This is the authoritative inference layer. Aggregate scores are derived
artifacts and can be regenerated under a changed rubric.

## 5. Evaluation stages after inference

For every model:

1. download and verify the complete generation archive;
2. validate 414 unique task records and patch hashes;
3. replay the existing seven-lane strict correctness Oracles;
4. preserve normalized correctness records and raw logs;
5. run the current maintainability rubric, while retaining raw judge output;
6. generate normalized four-gate results and leaderboard;
7. package manifests and `SHA256SUMS`.

If the maintainability evaluation changes, steps 5–7 are rerun from the stored
patches/traces. GPU model inference is not repeated.

## 6. Submission status

The five jobs were accepted by `msrresrchbasicvc` on 2026-08-09 and entered
`preparing`. Status, failures, retries, and final artifact hashes will be
appended here as the experiment progresses.

## 7. Correctness orchestration

`scripts/run_open_model_correctness.py` prepares the seven strict correctness
lanes without assuming the closed-model tuple. Model IDs are preserved exactly,
including Hugging Face IDs containing `/`.

Preflight does not require generation artifacts:

```bash
python scripts/run_open_model_correctness.py preflight \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --repo-cache "$REVIEWBENCH_SESSION_FILES/repos" \
  --output "$REVIEWBENCH_HARD_ORACLE/open-model-correctness-preflight" \
  --model-id Qwen/Qwen2.5-Coder-7B-Instruct \
  --model-id Qwen/Qwen2.5-Coder-32B-Instruct \
  --model-id Qwen/Qwen3-Coder-30B-A3B-Instruct \
  --model-id Qwen/Qwen3-Coder-Next-FP8 \
  --model-id Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8
```

After inference, stage one exact generation JSONL per model:

```bash
python scripts/run_open_model_correctness.py prepare \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --repo-cache "$REVIEWBENCH_SESSION_FILES/repos" \
  --output "$CORRECTNESS_OUTPUT" \
  --model 'Qwen/Qwen2.5-Coder-7B-Instruct=/path/generation_results.jsonl' \
  --model 'Qwen/Qwen2.5-Coder-32B-Instruct=/path/generation_results.jsonl'
```

Repeat `--model` for every solver. Preparation rejects duplicate or incomplete
task-model sets, stale task fingerprints, start-SHA mismatches, stale completion
markers, and patch checksum mismatches. Cluster-local patch and trace paths are
rebased into a durable input tree without changing the source generation file.

`run` invokes copied, checksum-recorded variants of the proven lane runners.
Only path, task-count, execution-host, and model-cardinality assumptions are
adapted; frozen wave sources and outputs are not modified:

```bash
python scripts/run_open_model_correctness.py run \
  --output "$CORRECTNESS_OUTPUT"
```

Runs are sequential by lane by default to avoid uncontrolled cross-lane load.
Use `--jobs LANE=N` to tune a lane. Existing terminal records resume, while
infrastructure-shaped records remain retryable.

Finalization validates the exact `(task ID, model)` matrix:

```bash
python scripts/run_open_model_correctness.py finalize \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --output "$CORRECTNESS_OUTPUT"
```

Durable outputs include:

- `correctness-raw/{lane}.jsonl`: complete heterogeneous runner records;
- `correctness-normalized/{lane}.jsonl`: common fields plus the complete raw
  record, raw status, coverage provenance, artifact/log paths, and raw hash;
- `correctness-manifest.json`: per-lane counts, statuses, pair-set hash, input
  hashes, missing pairs, and retryable infrastructure pairs;
- `SHA256SUMS` and `correctness.complete.json`;
- `release/{tasks,oracles}.jsonl`: the exact complete release inputs;
- copied generation records, patches, traces, adapted runner sources,
  provenance, checkpoints, and raw runner logs.

No candidate replay should start until all generation archives have passed
`prepare`.

### 7.1 Published-hard replay gap

The portable 414-task release contains 374 wave tasks with
`lane_results_source` and 40 previously published hard tasks without the modern
lane replay contract. The compact records certify the strict Oracle but usually
omit the exact executable command/environment needed for a new candidate.
The orchestrator assigns all 40 to a lane but fails closed instead of replacing
behavioral replay with source-shape checks:

| Lane | Modern ready | Published contract missing | Total |
|---|---:|---:|---:|
| Go/Python | 73 | 15 | 88 |
| Scripted | 53 | 4 | 57 |
| TS/JS | 42 | 4 | 46 |
| C# | 18 | 0 | 18 |
| JVM | 52 | 6 | 58 |
| Rust | 71 | 2 | 73 |
| Native | 65 | 9 | 74 |

Full 2,070-pair execution remains blocked until those 40 original per-task
replay contracts are recovered into the seven-lane format. For development
only, `prepare` and `run` accept `--allow-partial-modern`; such an artifact is
explicitly marked partial and cannot produce a complete manifest.
