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
| Active experiments | `reviewbench-qwen-latest-hard414-v1`, `reviewbench-open-latest-hard414-v1`, `reviewbench-kimi25-hard414-v1` |
| Service | Singularity |
| Target | `msrresrchbasicvc` |
| Workspace | `Workspace_NLC` |
| Project storage | `conversationhub / amulet` |
| Code snapshot | `projects/reviewbench-maintainability/amlt-code/9d4941f31edb2412b7dae2e08a13336f` |
| Task set | `data/releases/hard/20260808/tasks.jsonl` |
| Tasks | 414 |
| Protocol | `agentic-line-edit/v1`, maximum 32 rounds |

`msrresrch` is not used.

## 2. Model and resource matrix

All Hugging Face revisions are immutable commit SHAs.

| Job | Model / revision | SKU | TP | SLA |
|---|---|---|---:|---|
| `qwen3-coder-next-fp8` | `Qwen/Qwen3-Coder-Next-FP8@da6e2ed27304dd39abadd9c82ef50e8de67bdd4c` | `40G4-A100` | 4 | Standard |
| `qwen36-27b` | `Qwen/Qwen3.6-27B@6a9e13bd6fc8f0983b9b99948120bc37f49c13e9` | `40G2-A100` | 2 | Standard |
| `qwen36-35b-a3b` | `Qwen/Qwen3.6-35B-A3B@995ad96eacd98c81ed38be0c5b274b04031597b0` | `40G4-A100` | 4 | Standard |
| `devstral2-123b-fp8` | `mistralai/Devstral-2-123B-Instruct-2512@1613bf01adb5e1c6fdc196b46e6b173eae75eb4a` | `40G8-A100-IB-NvLink` | 8 | Standard |
| `gpt-oss-120b` | `openai/gpt-oss-120b@b5c939de8f754692c1647ca79fbf85e8c1e70f8a` | `40G4-A100` | 4 | Standard |
| `minimax-m21-fp8` | `MiniMaxAI/MiniMax-M2.1@cd97f59135f37b2a6bf09356e485d5e4aeb7dc9c` | `40G8-A100-IB-NvLink` | 8 | Standard |
| `kimi-k25` | `moonshotai/Kimi-K2.5@4d01dfe0332d63057c186e0b262165819efb6611` | `80G8-H100-IB-NvLink` | 8 | Standard |

GPU selection follows:

```text
A100 40GB → A100 80GB → H100 → H200
```

Kimi K2.5 uses H100 because its compressed checkpoint and kernels do not fit or
run on the available A100 configuration. GLM-5-FP8 and DeepSeek-V3.2-FP8 are
new enough to include, but their 754B/685B weight footprints require H200; H200
is currently blocked by platform-image compatibility and remains pending.

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

Active latest-Qwen result roots:

| Job | Backend ID | Blob result path |
|---|---|---|
| `qwen3-coder-next-fp8` | `musing_office_r9d8x2ysj9` | `projects/reviewbench-maintainability/amlt-results/7213695212.05418-8ebe60a6-2ee5-4962-9542-4dc48b51e119` |
| `qwen36-27b` | `happy_dog_g72rtj81j0` | `projects/reviewbench-maintainability/amlt-results/7213695212.05232-29f47ce7-942f-4b03-9d52-3d6a620047db` |
| `qwen36-35b-a3b` | `calm_car_5cv9rn3t70` | `projects/reviewbench-maintainability/amlt-results/7213695212.05046-0d6240a5-1bcc-438b-965a-aa56c6768ce3` |

Active cross-family result roots:

| Job | Backend ID | Blob result path |
|---|---|---|
| `devstral2-123b-fp8` | `purple_carnival_j14g3dz3fh` | `projects/reviewbench-maintainability/amlt-results/7213695172.92394-6a9f6a88-7fac-4f66-8725-1c31641410ba` |
| `gpt-oss-120b` | `keen_calypso_50g95dmwzh` | `projects/reviewbench-maintainability/amlt-results/7213695172.92205-52d66fb9-3ecb-4e45-ab44-f92bc959e3aa` |
| `minimax-m21-fp8` | `sharp_bee_8mbf3h0vnb` | `projects/reviewbench-maintainability/amlt-results/7213695172.92021-32c2d3f0-8679-4bc8-bfcf-e18b76ad557c` |

Kimi K2.5:

| Job | Backend ID | Blob result path |
|---|---|---|
| `kimi-k25` | `olden_leg_0rd7vknd5k` | `projects/reviewbench-maintainability/amlt-results/7213695135.17390-91af252b-95c0-4b98-88a1-c016e03cae39` |

Retired Qwen-v1 paths remain in blob as failure evidence. Those jobs loaded
vLLM but failed before inference because vLLM upgraded NumPy while the platform
image retained an incompatible old SciPy. Qwen-v2 and the first cross-family
submission were canceled before inference when the requested model set was
replaced by newer family releases. Active configs pin `numpy==2.2.6` and
`scipy==1.15.3`.

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

Seven latest-model jobs were accepted by `msrresrchbasicvc` on 2026-08-09:

- Qwen3-Coder-Next and Qwen3.6 27B/35B in
  `reviewbench-qwen-latest-hard414-v1`;
- Devstral-2, GPT-OSS and MiniMax M2.1 in
  `reviewbench-open-latest-hard414-v1`;
- Kimi K2.5 in `reviewbench-kimi25-hard414-v1`.

They entered `preparing` while the corrected vLLM environment was built.

GLM-5-FP8 and DeepSeek-V3.2-FP8 remain blocked on H200 image compatibility.
The MSBJ04 fallback account currently has no configured/login-visible targets,
so the pipeline fails closed rather than silently moving them to an unapproved
target.

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
  --model-id Qwen/Qwen3-Coder-Next-FP8 \
  --model-id Qwen/Qwen3.6-27B \
  --model-id Qwen/Qwen3.6-35B-A3B \
  --model-id mistralai/Devstral-2-123B-Instruct-2512 \
  --model-id openai/gpt-oss-120b \
  --model-id MiniMaxAI/MiniMax-M2.1 \
  --model-id moonshotai/Kimi-K2.5
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

## 8. Local Copilot API evaluation

Six current hosted models are evaluated through the verified local direct
endpoint `http://127.0.0.1:4141/v1/chat/completions`:

```text
gpt-5.6-sol
gpt-5.6-terra
gpt-5.6-luna
claude-opus-5
claude-sonnet-5
gemini-3.6-flash
```

All use the same 414-task `agentic-line-edit/v1` protocol, 32-round limit,
exact-start repo cache, and complete raw-response trace format.

Local artifact root:

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-copilot-20260809/
```

Each model directory contains:

- full `generation_results.jsonl`;
- every per-round raw response and parsed tool trace;
- all candidate patches;
- completion marker and generation summary;
- run log.

The `/home` volume currently has sufficient capacity, so these artifacts remain
local while generation runs. If the directory grows beyond comfortable local
capacity, it will be copied with AzCopy to:

```text
https://conversationhub.blob.core.windows.net/amulet/
  reviewbench-openmodels/copilot-api-20260809/
```

Any upload will be followed by a blob listing, manifest hash, and documented
per-model path. Local files are not removed until the blob copy is verified.
