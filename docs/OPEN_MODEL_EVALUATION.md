# Open-weight model evaluation

更新时间：2026-08-11

本文记录 414-hard benchmark 的开源模型完整评测。评测先冻结 inference
artifact，再运行 correctness 和 maintainability；以后 rubric 改变时直接重判，
不重新运行 GPU inference。

## 1. AMLT experiment

| Field | Value |
|---|---|
| AMLT environment | local conda `amlt10` |
| AMLT version | 10.14.0 |
| Project | `reviewbench-maintainability` |
| Active experiments | `reviewbench-qwen-latest-hard414-v4`, `reviewbench-open-infra-retries-20260811-v1`, `reviewbench-coder-context-retry-20260811-v3`, `reviewbench-kimi25-hard414-v12` |
| Service | Singularity |
| Target | `msrresrchbasicvc` |
| Workspace | `Workspace_NLC` |
| Project storage | `conversationhub / amulet` |
| Code snapshots | `projects/reviewbench-maintainability/amlt-code/85ab05f9c45c04ffd8afd982ff3ab522` (original Qwen/cross-family), `projects/reviewbench-maintainability/amlt-code/69e75bceda6918fcd787b1aa5df0c56c` (bounded Coder retry), `projects/reviewbench-maintainability/amlt-code/12a6350df2840e14ee678e219a2d726f` (bounded Kimi remainder) |
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

Kimi K2.5 retry (`reviewbench-kimi25-hard414-v11`):

| Job | Backend ID | Blob result path |
|---|---|---|
| `kimi-k25` | `jolly_stamp_1t6v7bgz36` | `projects/reviewbench-maintainability/amlt-results/7213555570.21499-72941eed-97c2-4128-b1bb-c446f64b0a0b` |

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
| `reviewbench-kimi25-hard414-v7:kimi-k25` | first-request runtime OOM before valid inference | `projects/reviewbench-maintainability/amlt-results/7213601570.50237-cc8dc298-9c65-4d0c-b55c-197520cd42e2` |

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

Five completed open-model archives were downloaded and verified locally under
`/data/v-peiyangliu/reviewbench-openmodels/completed-20260811/`:

| Model | Records | Generated | Failed | Archive SHA-256 |
|---|---:|---:|---:|---|
| Qwen3.6-35B-A3B | 414 | 314 | 100 | `8bbfd0b5349c5cd87a85e236f761268183a135ec9c32e8315b537320964fcacc` |
| Qwen3-Coder-Next-FP8 | 414 | 305 | 109 | `508881cbf65724eeee8a9b22a4534ed91cc7ca89b32ef19ccbfdad5e451ee5a7` |
| Devstral-2-123B | 414 | 380 | 34 | `9b154b175b71c12d7ccf4fe55f1bbca9786d547f925a4761e761ae6c9837da1f` |
| GPT-OSS-120B | 414 | 413 | 1 | `6f1a278830806ad8cbb3f29c0303276d9fea6a680f097b2fc5a8599217fd9472` |
| MiniMax-M2.1 | 414 | 374 | 40 | `76b7f6a01bdf212a6ae0cf8a5d49dd1ac74da11fb7f367ff9007ee3c6b873e12` |

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
cache-object preflight.

Seven updated jobs were accepted by `msrresrchbasicvc` on 2026-08-10:

- Qwen3-Coder-Next and Qwen3.6 27B/35B in
  `reviewbench-qwen-latest-hard414-v4`;
- Devstral-2, GPT-OSS and MiniMax M2.1 in
  `reviewbench-open-latest-hard414-v5`;
- Kimi K2.5 in `reviewbench-kimi25-hard414-v4`.

After exhausting H100 BF16-KV configurations and rejecting unsupported H200
image metadata, Kimi FP8-KV v11 was accepted on 2026-08-11. It produced eight
valid samples before two requests exposed an unbounded-transcript context
failure. Those eight samples are frozen; v11 was stopped without resampling
them, and v12 targets only the remaining 406 IDs with a 60,000-character
transcript bound.

Generation checkpoint at `2026-08-11T17:05Z`:

| Model | Generated | Failed | State |
|---|---:|---:|---|
| Qwen3.6-27B | 146 | 50 | running; 196/414 records |
| Qwen3.6-35B-A3B | 345 | 69 | canonical after targeted retry |
| Qwen3-Coder-Next-FP8 | 327 | 87 | canonical after two targeted retry layers |
| Devstral-2-123B | 387 | 27 | canonical after targeted retry |
| GPT-OSS-120B | 413 | 1 | complete |
| MiniMax-M2.1 | 374 | 40 | complete |
| Kimi-K2.5 | 8 preserved | 406 pending | v12 running |

The completed non-Kimi models include 47 BlobFuse source-read timeouts that
will be retried without resampling successful or protocol-terminal tasks. Kimi
v7 started successfully but its first request exhausted H100 runtime workspace.
H200 submissions were rejected before job creation by amlt10's base-image
accelerator metadata. Kimi v11 therefore retains 32K on H100 using a 1200 MiB
FP8 KV cache and concurrency two.

### 6.1 Completed-model failure audit

The authoritative report is stored in Blob at:

```text
reviewbench-openmodels/analysis/20260811/failure-analysis-v2.json
```

It is derived from the verified final archives, not potentially incomplete
live-synced JSONL files:

| Model | Failed | Retryable infra/harness | Terminal model behavior |
|---|---:|---:|---:|
| Qwen3.6-35B-A3B | 100 | 36 | 64 |
| Qwen3-Coder-Next-FP8 | 109 | 26 | 83 |
| Devstral-2-123B | 34 | 7 | 27 |
| MiniMax-M2.1 | 40 | 6 | 34 |
| GPT-OSS-120B | 1 | 0 | 1 |
| **Total** | **284** | **75** | **209** |

The 75 retryable records consist of 47 BlobFuse `git show` timeouts, 25 Qwen35
ellipsis serialization failures, and 3 Coder-Next context-boundary failures.
The 209 terminal records consist mainly of 135 no-patch 32-round exhaustions
and 64 invalid actions; the remaining 10 are malformed fields, invalid edit
ranges/paths, or requests for absent files.

Only the 75 retryable task IDs are resubmitted in
`reviewbench-open-infra-retries-20260811-v1`:

| Job | Tasks | Backend ID | Blob result path |
|---|---:|---|---|
| `qwen36-35b-a3b-infra-retry` | 36 | `neat_floor_sfbgyddm3t` | `projects/reviewbench-maintainability/amlt-results/7213533615.38996-1d590363-133c-45f3-9d89-28814db7427e` |
| `qwen3-coder-next-fp8-infra-retry` | 26 | `purple_lamp_9w53plyfkp` | `projects/reviewbench-maintainability/amlt-results/7213533615.38588-188a0adf-efcb-47ac-9fc2-83fe328611e8` |
| `devstral2-123b-fp8-infra-retry` | 7 | `honest_apricot_f52mr9bkh4` | `projects/reviewbench-maintainability/amlt-results/7213533615.38400-562f403e-842a-40cb-9f14-066d504baaa8` |
| `minimax-m21-fp8-infra-retry` | 6 | `serene_floor_kmyhz1z2nx` | `projects/reviewbench-maintainability/amlt-results/7213533615.38206-d44a9ee0-75f2-4994-b06c-83485bd9365c` |

The first Coder-Next delta recovered 21 of 26 tasks. Three remaining tasks
exhausted 32 rounds without a patch. A 1024-token retry did not fix the other
two because the accumulated read transcript still requested exactly 65,537
tokens:

```text
projects/reviewbench-maintainability/amlt-results/
  7213524369.94635-0be2487b-8149-43d0-adb9-d2f97bfdec08
```

The generator now rejects non-JSON ellipsis values before transcript
serialization, allows five minutes for lazy Blob-backed source reads, supports
exact ID manifests and per-job output-token limits, and retains only the newest
complete tool events inside a configured transcript budget. The bounded v3
retry recovered BSMPT; the Clarity-Wasm task then completed 32 valid calls
without editing and is a terminal model failure. Final Coder generation is
327/414.

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

#### 7.1.1 Reserve-20260728 replay adapter (14 of the 40)

`scripts/run_legacy_reserve_correctness.py` closes part of this gap for the
14 (of the 40) published-hard tasks whose Oracle was recovered under
`data/releases/reserves/20260728/` and that still have a genuine, previously
proven (or at least fully self-contained) legacy harness on the retained
session root: native 6 (ClickHouse, Desbordante, IntelPython/dpnp,
SciCompMod/memilio, XRPLF/rippled, lanl/singularity-eos), JVM 5
(Consensys/web3signer, SkriptLang/Skript, apache/solr, hibernate-orm,
reposense/RepoSense), Rust 1 (FuelLabs/fuel-tx), TS/JS 2
(discourse/discourse, opossum-tool/OpossumUI).

It reuses the existing `run_open_model_correctness.py` extension point
(`source_kind == "published_legacy"`, lane inferred by `legacy_lane()`) and
writes only `output/legacy/correctness/<lane>.jsonl` raw rows -- the exact
file `finalize()` already merges with `output/modern/correctness/<lane>.jsonl`
and normalizes through the same `normalized_record()` used for the 374 modern
tasks. It does not reimplement patch validation/staging: it consumes the
already-staged, checksum-verified `generation_results.normalized.jsonl`
produced by `prepare --allow-partial-modern`.

Design:
- Native + Rust replay a fresh, disposable copy of the historical
  `.../start` checkout per (task, model), applying the staged candidate
  patch there and re-running the exact (for 5 of 6 native tasks: previously
  *proven*, recorded in the historical `native.jsonl`) Docker command; the
  retained checkout itself is never mutated.
- JVM + TS/JS replay a fresh `git worktree` of the retained
  `repos/<org>__<repo>` clone at the Oracle's exact `start_sha`, apply the
  staged patch there, and run the Oracle's own self-contained
  `command`/`test_command` fields with the historical toolchain (pinned
  Docker image or Node runtime).
- Any task whose required Docker image, retained repo checkout, Gradle/cargo
  cache, or Node runtime is missing is reported as a **preflight blocker**
  and is left out of the raw output entirely -- it is never scored as a
  pass/fail. `finalize --allow-incomplete` will show it under
  `missing_pairs` until the asset is recovered.

Commands:

```bash
# 1) Stage patches for an arbitrary model across the full 414-task release
#    (allow the 40 published-legacy tasks, including our 14, to stay
#    unresolved for now):
python3 scripts/run_open_model_correctness.py prepare \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --repo-cache <repo-cache> --output <output> \
  --model '<model>=<generation_results.jsonl>' \
  --allow-partial-modern

# 2) Fail-closed preflight of the 14 reserve tasks (no execution):
python3 scripts/run_legacy_reserve_correctness.py preflight \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --legacy-root <retained-session-root> \
  --output <output> --allow-blocked

# 3) Replay only what preflight reports ready, for the staged model:
python3 scripts/run_legacy_reserve_correctness.py replay \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --legacy-root <retained-session-root> \
  --output <output> --model '<model>'

# 4) Finalize as usual (missing/blocked pairs remain visible, not fabricated):
python3 scripts/run_open_model_correctness.py finalize \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --output <output> --allow-incomplete
```

As of this writing, a read-only `preflight` pass against the retained
session root reports 7 of the 14 tasks ready (ClickHouse, dpnp, memilio,
rippled, singularity-eos, discourse, opossum-tool/OpossumUI) and 7 blocked:
Desbordante (harness recipe was never proven historically -- every
historical candidate patch failed to apply through it), the 4 remaining JVM
tasks plus web3signer's `-javaagent`/`-I` flags (missing recorded Gradle
cache home and `/oracle-cache` assets under
`oracle_recovery/java/cache/`), and FuelLabs/fuel-tx (missing recorded
cargo cache under `oracle_recovery/rust/cache/cargo`). These are reported,
not invented; recovering the missing cache directories (or the Desbordante
proof) would move those tasks to "ready" without any code change.

`tests/test_run_legacy_reserve_correctness.py` covers this adapter with
tiny synthetic fixtures only (no real builds/containers): patch
apply/failure handling, git worktree materialization, blocker detection per
lane, drift detection against the 14-task manifest, resume/skip behavior,
and candidate-missing rows that normalize cleanly through the real
`normalized_record()`.

### 7.2 Current five-model correctness replay

At `2026-08-11T03:40:36Z`, five completed model archives passed generation
marker, task fingerprint, pair-set, patch, and checksum validation. The
374-task modern replay is running at:

```text
/data/v-peiyangliu/reviewbench-openmodels/
  correctness-20260811-five-v2/
```

It contains 2,070 task-model inputs, explicitly marks candidate-missing
generations, and has preflight SHA-256
`f0b6513a053182557d74cc147433f176aa202fa278ccf551c37b08a4cb593a0e`.
Qwen3.6-27B and Kimi will be added after valid generation completes.

The initial Rust pass stopped after 40 records because per-attempt Cargo build
state filled `/data`. Completed lane caches and interrupted worker scratch were
removed without deleting JSONL, logs, patches, or evidence artifacts. A first
cleanup used `shutil.rmtree`, which silently left root-owned Docker files.
The current adapter keeps Cargo state inside each attempt and verifies deletion
through a cleanup container. Rust resumed at one worker without accumulating
completed build trees.

The replay also found two evaluation-infrastructure defects rather than model
failures:

- six Go/Python Oracles lost their pinned Go/Python runtime through path
  remapping, shared `PYTHONPATH`, or an overlong AF_UNIX temporary path;
- four CRLF source files were read through universal-newline conversion, so
  otherwise valid patches were emitted against LF-normalized blobs.

Both are fixed and isolated task subsets are being replayed across all completed
models. Canonical generation overlays reconstruct CRLF patches from the saved
tool traces; they never call the model again.

### 7.3 Parallel rubric-v2 judgment

Completed models are judged without waiting for the remaining inference jobs.
Two rubric-v2 blind-judge runs use `claude-opus-4.8` through the verified 4141
direct endpoint:

```text
/data/v-peiyangliu/reviewbench-openmodels/judging-20260811/
  hosted-five/
    judgments.jsonl
    raw/
    run.log
  open-five/
    judgments.jsonl
    raw/
    run.log
```

The hosted group contains Claude Opus/Sonnet 5 and GPT-5.6 Luna/Sol/Terra.
The open group contains Qwen35B, Coder-Next, Devstral-2, GPT-OSS, and MiniMax.
Every raw judge response is retained. Completion markers, checksums, normalized
four-gate results, and Blob paths are produced after each group finishes.

Both base groups are complete: open-five has 2,070 records with 298
maintainability passes, and hosted-five has 2,070 records with 1,029 passes.
Schema-invalid judge replies were retried with explicit field feedback; no
`judge_error` remains. The base judgment bundle, including 4,229 raw responses,
is stored at:

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-evaluation-20260811/judging/
  rubric-v2-judgments-20260811.tar.gz
SHA-256 d1c032a1566678701f308779676a5e53247120f3660360b30fc8e8605bd0f123
```

Recovered-generation and line-ending deltas are judged separately and overlay
only the affected task-model pairs.

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
Responses route passed a live tool-use smoke test, and only failed records were
replaced.

The first Gemini 3.6 run is retained but excluded from scoring. Of its 283
terminal failures, 149 had empty content on all 32 calls and 77 had only one
non-empty call. Controlled probes showed:

- `default`, `medium`, and `low` reasoning all returned
  `finish_reason="error"` with empty content;
- changing source-window and output-token budgets did not fix the error;
- the same real task returned a valid `find` call when the four actions were
  provided through the OpenAI `tools` field instead of text-embedded JSON.

The native-tools adapter preserves the same logical read/find/edit/finish
protocol, converts exactly one function call per turn into the canonical action,
and saves the complete raw API response envelope. A first native run showed that
text-serialized tool history caused repeated read/find loops: all 27 failures
made 32 valid function calls but zero edits. The corrected adapter uses standard
assistant/tool messages and reserves the final eight calls for an edit if the
model has only inspected files.

A full 414-task fresh run completed at:

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-copilot-20260811/
  gemini-3.6-flash-native-tools-v2/
```

The valid native-tools result is **375 generated / 39 failed**. Of the failures,
37 explored through round 23 and then repeatedly returned an empty `edits`
array when required to make a concrete edit; two edited but ended with no
effective patch. There are no empty gateway responses in this run. Its
rubric-v2 judgment is complete at 223/414 before correctness and delta overlay.

Hosted-model status at `2026-08-10T20:10:41Z`:

| Model | Generated | Remaining failed | State |
|---|---:|---:|---|
| `claude-sonnet-5` | 414 | 0 | complete |
| `claude-opus-5` | 411 | 3 | complete; three Metasploit tasks exhausted 32 rounds twice |
| `gemini-3.6-flash` | 375 | 39 | native-tools v2 complete; old 131/414 run excluded |
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
