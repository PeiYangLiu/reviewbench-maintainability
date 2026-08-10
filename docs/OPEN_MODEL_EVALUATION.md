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
| Active experiments | `reviewbench-qwen-latest-hard414-v4`, `reviewbench-open-latest-hard414-v5`, `reviewbench-kimi25-hard414-v7` |
| Service | Singularity |
| Target | `msrresrchbasicvc` |
| Workspace | `Workspace_NLC` |
| Project storage | `conversationhub / amulet` |
| Code snapshots | `projects/reviewbench-maintainability/amlt-code/85ab05f9c45c04ffd8afd982ff3ab522` (Qwen/cross-family), `projects/reviewbench-maintainability/amlt-code/33bd4b03fb7c4464d9e9d01655ad26cc` (Kimi) |
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

Active latest-Qwen retry result roots
(`reviewbench-qwen-latest-hard414-v4`):

| Job | Backend ID | Blob result path |
|---|---|---|
| `qwen3-coder-next-fp8` | `frosty_celery_1b2rj4p0z2` | `projects/reviewbench-maintainability/amlt-results/7213619421.74239-d211805e-733e-42e0-9347-064abf4142e5` |
| `qwen36-27b` | `loving_garage_zq18vlnm50` | `projects/reviewbench-maintainability/amlt-results/7213619421.74031-104fbb46-9099-4d20-8311-859291c62a66` |
| `qwen36-35b-a3b` | `kind_cat_lc94xdsyxf` | `projects/reviewbench-maintainability/amlt-results/7213619421.73854-a54ab682-f811-46dd-a598-20a20791d77a` |

Active cross-family retry result roots
(`reviewbench-open-latest-hard414-v5`):

| Job | Backend ID | Blob result path |
|---|---|---|
| `devstral2-123b-fp8` | `loving_tooth_y4h1qrkjpk` | `projects/reviewbench-maintainability/amlt-results/7213619390.70356-6fdc8105-67e7-4b85-92bb-49a97b377cf9` |
| `gpt-oss-120b` | `jolly_lime_stp99xkpy5` | `projects/reviewbench-maintainability/amlt-results/7213619390.70144-0660aa2d-7741-4009-8436-e52c5eaf8d03` |
| `minimax-m21-fp8` | `cyan_bulb_wgqbmkp94x` | `projects/reviewbench-maintainability/amlt-results/7213619390.69963-21c60d6b-3514-4c44-be9b-426529b1b719` |

Kimi K2.5 retry (`reviewbench-kimi25-hard414-v7`):

| Job | Backend ID | Blob result path |
|---|---|---|
| `kimi-k25` | `musing_plow_lk7mjb18f0` | `projects/reviewbench-maintainability/amlt-results/7213601570.50237-cc8dc298-9c65-4d0c-b55c-197520cd42e2` |

Failed and canceled latest-model attempts remain immutable failure evidence:

| Experiment / job | Terminal state | Blob result path |
|---|---|---|
| `reviewbench-qwen-latest-hard414-v1:qwen3-coder-next-fp8` | failed before inference | `projects/reviewbench-maintainability/amlt-results/7213695212.05418-8ebe60a6-2ee5-4962-9542-4dc48b51e119` |
| `reviewbench-qwen-latest-hard414-v1:qwen36-27b` | failed before inference | `projects/reviewbench-maintainability/amlt-results/7213695212.05232-29f47ce7-942f-4b03-9d52-3d6a620047db` |
| `reviewbench-qwen-latest-hard414-v1:qwen36-35b-a3b` | failed before inference | `projects/reviewbench-maintainability/amlt-results/7213695212.05046-0d6240a5-1bcc-438b-965a-aa56c6768ce3` |
| `reviewbench-open-latest-hard414-v1:devstral2-123b-fp8` | failed before inference | `projects/reviewbench-maintainability/amlt-results/7213695172.92394-6a9f6a88-7fac-4f66-8725-1c31641410ba` |
| `reviewbench-open-latest-hard414-v1:gpt-oss-120b` | canceled while queued | `projects/reviewbench-maintainability/amlt-results/7213695172.92205-52d66fb9-3ecb-4e45-ab44-f92bc959e3aa` |
| `reviewbench-open-latest-hard414-v1:minimax-m21-fp8` | failed before inference | `projects/reviewbench-maintainability/amlt-results/7213695172.92021-32c2d3f0-8679-4bc8-bfcf-e18b76ad557c` |
| `reviewbench-open-latest-hard414-v2:minimax-m21-fp8` | canceled while queued | `projects/reviewbench-maintainability/amlt-results/7213692649.93167-743e4cf8-fac8-414c-a2af-afa920554df6` |
| `reviewbench-kimi25-hard414-v1:kimi-k25` | failed before inference | `projects/reviewbench-maintainability/amlt-results/7213695135.17390-91af252b-95c0-4b98-88a1-c016e03cae39` |
| `reviewbench-qwen-latest-hard414-v2:qwen3-coder-next-fp8` | 414 source reads failed before inference | `projects/reviewbench-maintainability/amlt-results/7213692609.19921-3d4ecb43-1694-46a3-9a17-7c81468f2148` |
| `reviewbench-qwen-latest-hard414-v2:qwen36-27b` | 414 source reads failed before inference | `projects/reviewbench-maintainability/amlt-results/7213692609.19729-c4908ce4-ce7b-4daf-8d63-8b91805d88c8` |
| `reviewbench-qwen-latest-hard414-v2:qwen36-35b-a3b` | 414 source reads failed before inference | `projects/reviewbench-maintainability/amlt-results/7213692609.19554-34348a7b-dcf4-461a-8701-b840b36599b4` |
| `reviewbench-open-latest-hard414-v3:devstral2-123b-fp8` | 414 source reads failed before inference | `projects/reviewbench-maintainability/amlt-results/7213692429.75640-6bb028b1-99ed-4e5b-88b0-c558f61ed1a2` |
| `reviewbench-open-latest-hard414-v3:gpt-oss-120b` | 414 source reads failed before inference | `projects/reviewbench-maintainability/amlt-results/7213692429.75446-6a3b509f-289f-42dc-bd08-9d84150743a8` |
| `reviewbench-open-latest-hard414-v3:minimax-m21-fp8` | 414 source reads failed before inference | `projects/reviewbench-maintainability/amlt-results/7213692429.75268-d7406a12-413b-414b-8b2b-c35c980690a0` |
| `reviewbench-kimi25-hard414-v2:kimi-k25` | failed before server startup | `projects/reviewbench-maintainability/amlt-results/7213692577.50703-02a66671-a198-437e-b76e-973c6dec125d` |
| `reviewbench-qwen-latest-hard414-v3:qwen3-coder-next-fp8` | failed cache preflight before inference | `projects/reviewbench-maintainability/amlt-results/7213646004.43791-c6690589-573b-4880-9ac5-0cac7405c04a` |
| `reviewbench-qwen-latest-hard414-v3:qwen36-27b` | failed cache preflight before inference | `projects/reviewbench-maintainability/amlt-results/7213646004.43595-e7266fea-8a70-49de-bf32-fcd2ca412285` |
| `reviewbench-qwen-latest-hard414-v3:qwen36-35b-a3b` | failed cache preflight before inference | `projects/reviewbench-maintainability/amlt-results/7213646004.43413-fa6ecfc8-ab1d-4a0a-bc50-1cff4fd81bbc` |
| `reviewbench-open-latest-hard414-v4:devstral2-123b-fp8` | failed cache preflight before inference | `projects/reviewbench-maintainability/amlt-results/7213645968.60049-9982a3d0-57fc-46b0-8027-a1ce108a8128` |
| `reviewbench-open-latest-hard414-v4:gpt-oss-120b` | failed cache preflight before inference | `projects/reviewbench-maintainability/amlt-results/7213645968.59853-ae63b09a-4130-406c-ade5-ec95116f3b86` |
| `reviewbench-open-latest-hard414-v4:minimax-m21-fp8` | failed cache preflight before inference | `projects/reviewbench-maintainability/amlt-results/7213645968.59675-7e76c2e7-9896-4bfa-87c9-4f4cfc39db2d` |
| `reviewbench-kimi25-hard414-v3:kimi-k25` | failed cache preflight before inference | `projects/reviewbench-maintainability/amlt-results/7213645931.71015-6f9dedd5-14f8-4f19-8148-f1f07f86619a` |
| `reviewbench-kimi25-hard414-v4:kimi-k25` | insufficient KV cache before inference | `projects/reviewbench-maintainability/amlt-results/7213619360.83046-b51d699f-4fae-4841-8d19-17e10dd73d61` |
| `reviewbench-kimi25-hard414-v5:kimi-k25` | KV allocation fragmentation before inference | `projects/reviewbench-maintainability/amlt-results/7213614687.03225-df8080c1-869a-4aef-b3d0-8e256d4f7a1b` |
| `reviewbench-kimi25-hard414-v6:kimi-k25` | FlashInfer warmup OOM before inference | `projects/reviewbench-maintainability/amlt-results/7213606929.36377-f080ff1d-bc1d-492a-ada4-7ef16f69b7ca` |

Failed jobs that reached the wrapper contain the complete server log, manifest,
and packaged diagnostic archive. Still earlier retired Qwen jobs preserve the
original NumPy/SciPy incompatibility evidence. Active configs pin
`numpy==2.2.6` and `scipy==1.15.3`.

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

The first latest-model attempts failed before any benchmark inference. Their
artifacts were preserved, diagnosed, and superseded without treating the jobs
as model results:

| Jobs | Root cause | Retry change |
|---|---|---|
| Qwen3-Coder-Next, Qwen3.6 27B/35B, Devstral-2 | vLLM custom all-reduce aborted with `custom_all_reduce.cuh:455 'invalid argument'` on the Singularity A100 topology | `--disable-custom-all-reduce`, falling back to NCCL |
| MiniMax M2.1 | FP8 expert shard width 192 was not divisible by block width 128 under TP8 | official `--enable-expert-parallel`; NCCL fallback also enabled |
| Kimi K2.5 | the 0.90 GPU budget left `-3.75 GiB` for KV cache after loading 71.19 GiB per H100 | text-only serving and `max_num_seqs=4` |

The queued first-attempt GPT-OSS job and an intermediate MiniMax retry were
canceled before model execution once the shared all-reduce failure was known.

The next six non-Kimi jobs successfully started their model servers, but all
414 tasks failed while reading exact-start source from the BlobFuse-mounted Git
cache, before the first model request. Kimi's 0.98 memory threshold was 0.48 GiB
above one H100's startup-free memory. These are infrastructure records, not
model results.

The cache preflight then exposed the underlying portability defect: the local
minimal repositories relied on empty `.git/refs/` directories, which Blob
storage does not preserve. The builder now writes one real
`refs/heads/reviewbench-cache` per repository. All 340 refs were generated,
the 414 exact-start objects were revalidated, and the 340 41-byte files were
incrementally uploaded and counted in Blob. The current jobs retain the
preflight and use `gpu_memory_utilization=0.96` for Kimi.

Seven updated jobs were accepted by `msrresrchbasicvc` on 2026-08-10:

- Qwen3-Coder-Next and Qwen3.6 27B/35B in
  `reviewbench-qwen-latest-hard414-v4`;
- Devstral-2, GPT-OSS and MiniMax M2.1 in
  `reviewbench-open-latest-hard414-v5`;
- Kimi K2.5 in `reviewbench-kimi25-hard414-v4`.

At `2026-08-10T14:47:52Z`, Qwen3.6 27B and 35B-A3B had produced 78 and 157
task records; Qwen3-Coder-Next and all three cross-family A100 jobs were queued.
Kimi v6 allocated enough KV for 32K but FlashInfer autotune required another
256 MiB temporary buffer. Kimi v7 uses a 2200 MiB KV cap and disables that
autotune warmup while retaining the 32K context.

All retry result roots are listed in section 3. No failed startup produced a
benchmark sample, so retries do not overwrite or resample successful inference.

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

Six current hosted models are evaluated through the verified local Copilot
gateway. Claude and Gemini use
`http://127.0.0.1:4141/v1/chat/completions`; GPT-5.6 uses the Copilot Responses
API with credentials issued by the local `http://127.0.0.1:4141/token`
endpoint:

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

The first GPT-5.6 attempt was rejected before inference because the three
models were incorrectly forced through Chat Completions. The corrected
Responses route passed a live tool-use smoke test, and only failed records are
being replaced. Gemini's empty-content failures are likewise retried at one
worker after the original six-process run overloaded the shared gateway.

Status at `2026-08-10T14:47:52Z`:

| Model | Generated | Remaining failed | State |
|---|---:|---:|---|
| `claude-sonnet-5` | 414 | 0 | complete |
| `claude-opus-5` | 411 | 3 | complete; three Metasploit tasks exhausted 32 rounds twice |
| `gemini-3.6-flash` | 116 | 298 | low-concurrency failed-only retry running |
| `gpt-5.6-luna` | 414 | 0 | complete |
| `gpt-5.6-sol` | 412 | 2 | complete; two Speckle duplication tasks exhausted 32 rounds |
| `gpt-5.6-terra` | 414 | 0 | complete |

Artifact root on the `conversationhub` BlobFuse2 mount:

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-copilot-20260809/
```

Outputs are written directly to this blob-backed mount rather than staged on
ephemeral local disk. Each model directory contains:

- full `generation_results.jsonl`;
- every per-round raw response and parsed tool trace;
- all candidate patches;
- completion marker and generation summary;
- run log.

After each model completes, its completion marker, manifest, and checksums are
written beside the full JSONL, traces, and patches. No inference artifact is
deleted after aggregation.
