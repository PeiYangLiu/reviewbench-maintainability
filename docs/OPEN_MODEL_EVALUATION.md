# Open-weight model evaluation

更新时间：2026-08-13

本文记录 414-hard benchmark 的开源模型完整评测。评测先冻结 inference
artifact，再运行 correctness 和 maintainability；以后 rubric 改变时直接重判，
不重新运行 GPU inference。

> **最新评分更新（2026-08-15）：** 已恢复 34/40 published-legacy
> contracts，并重放 12 个模型。统一 408-task leaderboard 见
> [`LATEST_MODEL_PARTIAL_LEADERBOARD_20260815.md`](LATEST_MODEL_PARTIAL_LEADERBOARD_20260815.md)。
> 本文后续 381/378 数字保留为恢复前历史 checkpoint。

## 1. AMLT experiment

| Field | Value |
|---|---|
| AMLT environment | local conda `amlt10` |
| AMLT version | 10.14.0 |
| Project | `reviewbench-maintainability` |
| Active experiments | `reviewbench-kimi25-hard414-v14` |
| Service | Singularity |
| Target | `msrresrchbasicvc` |
| Workspace | `Workspace_NLC` |
| Project storage | `conversationhub / amulet` |
| Code snapshots | `projects/reviewbench-maintainability/amlt-code/85ab05f9c45c04ffd8afd982ff3ab522` (original Qwen/cross-family), `projects/reviewbench-maintainability/amlt-code/69e75bceda6918fcd787b1aa5df0c56c` (bounded Coder retry), `projects/reviewbench-maintainability/amlt-code/12a6350df2840e14ee678e219a2d726f` (bounded Kimi remainder), `projects/reviewbench-maintainability/amlt-code/b5d63f4abb99530320a2cfe95b5e735e` (restart-resilient Kimi remainder), `projects/reviewbench-maintainability/amlt-code/65700978cc7e62973c570a5908af83be` (four-way Kimi shards) |
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

Generation checkpoint at `2026-08-13T04:27Z`:

| Model | Generated | Failed | State |
|---|---:|---:|---|
| Qwen3.6-27B | 363 | 51 | canonical; rubric-v2 complete, correctness running |
| Qwen3.6-35B-A3B | 345 | 69 | canonical after targeted retry |
| Qwen3-Coder-Next-FP8 | 327 | 87 | canonical after two targeted retry layers |
| Devstral-2-123B | 387 | 27 | canonical after targeted retry |
| GPT-OSS-120B | 413 | 1 | complete |
| MiniMax-M2.1 | 378 | 36 | canonical after targeted retry |
| Kimi-K2.5 | 34 | 3 failed + 377 pending | v14: four shards queued |

Qwen3.6-27B v5 completed all 235 bounded-remainder IDs with 184 generated and
51 failed records. Its partial v4 input has 179 generated and 55 failed records;
the two inputs overlap only on those 55 failures and their union is exactly 414
IDs. The canonical merger preserves all 179 v4 successes, overlays the complete
v5 remainder, and produces 363 generated / 51 failed. The final v5 archive is:

```text
projects/reviewbench-maintainability/amlt-results/
  7213506183.41486-94bd2fd8-e8fb-4836-9fe4-0c53a4bda124/
  open-model-generation.tar.gz
SHA-256 39405ecdb694b3653a7ed549ebafc65d6a0592f79f3e2dc153f806724b4d8f7e

/data/v-peiyangliu/reviewbench-openmodels/
  canonical-20260811/Qwen-Qwen3.6-27B/generation_results.jsonl
SHA-256 cc6658363311fbccbfb649d19210cd6d815f4c55a5635c3fdfabb0e43de49c68
```

Kimi v7 started successfully but its first request exhausted H100 runtime
workspace. H200 submissions were rejected before job creation by amlt10's
base-image accelerator metadata. Kimi v11 therefore retains 32K on H100 using
a 1200 MiB FP8 KV cache and concurrency two. Its eight preserved v11 successes
plus the preserved v12 checkpoints give 16 generated, one failed, and 397
unprocessed IDs. The restart-resilient v13 job completed a durable 20-row
checkpoint with 18 generated and two failed records. The frozen union is now
34 generated, three failed, and 377 unprocessed IDs.

The Kimi v12 Singularity container restarted and re-executed the command at
`2026-08-13T03:39Z`. Its local `$AMLT_DIRSYNC_DIR` was empty, so the live JSONL
regressed from nine records to five and began repeating already-processed IDs.
Both versions were frozen before terminating v12:

```text
/data/v-peiyangliu/reviewbench-openmodels/
  restart-snapshots/20260813T0414Z/
```

The v13 job targets the exact 397-ID complement and restores
`$AMLT_OUTPUT_DIR/run/generation` from persistent Blob storage before invoking
the existing `--resume` path. Successful patches and traces therefore survive
future container restarts instead of being resampled. The accepted job is:

```text
experiment: reviewbench-kimi25-hard414-v13
job:        kimi-k25-resumable-remainder
job id:     brave_panda_fjqqgfly8v
results:    projects/reviewbench-maintainability/amlt-results/
            7213379617.71709-d7c6bb44-2a39-4e1c-9371-c41034ce2500
IDs SHA-256 53d6a1fb9271c4e7912d334dce942144c3ccdc19ca62853165d784739e8ffdd1
```

The exact 377-ID complement is now split round-robin into four disjoint
95/94/94/94-task jobs in `reviewbench-kimi25-hard414-v14`. All four jobs were
accepted on `msrresrchbasicvc`; v13 was then stopped. Each shard retains the
same Blob-backed restart recovery.

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

#### 7.1.1 Reserve-20260728 replay adapter (17 of the 40)

`scripts/run_legacy_reserve_correctness.py` closes part of this gap for the
17 (of the 40) published-hard tasks whose Oracle was recovered under
`data/releases/reserves/20260728/` (or the older increment5/legacy159
recoveries) and that still have a genuine, previously proven (or fully
recoverable) legacy harness: native 6 (ClickHouse, Desbordante,
IntelPython/dpnp, SciCompMod/memilio, XRPLF/rippled, lanl/singularity-eos),
JVM 6 (Consensys/web3signer, SkriptLang/Skript, apache/solr, hibernate-orm,
reposense/RepoSense, sniffy/sniffy), Rust 1 (FuelLabs/fuel-tx), TS/JS 4
(discourse/discourse, opossum-tool/OpossumUI, microsoft/FluidFramework,
angular/components).

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
- JVM + TS/JS materialize the exact start commit the way the historical
  harness did (`git archive | tar` plus `git init` and one empty commit --
  **not** a `git worktree`, whose `.git` file points at an absolute path
  inside the retained clone that is not mounted into the Oracle container and
  therefore breaks repository-introspecting builds), apply the staged patch
  there, and run the Oracle's own `command`/`test_command` with the historical
  toolchain against **pinned, hashed durable assets** under `--asset-root`.
- Scored JVM/TS-JS replays run with **no network** (`docker run
  --network none`, `pnpm --offline`). Network access is only ever used to
  *warm* the pinned durable assets, never to score. The single documented
  exception is sniffy, whose behavior anchor *is* network behavior (Sniffy
  monitors sockets and NIO channels on loopback); it keeps a network
  namespace but pins Maven into offline mode (`MAVEN_ARGS=-o`), so
  dependency resolution is still cache-only. Every row records
  `offline_replay` and `container_network`.
- `JVM_MANIFEST`/`TSJS_MANIFEST` carry the parts of the historical execution
  recipe that were never published with the Oracle records and had to be
  recovered from the retained harness and run logs: the exact per-task Gradle
  cache partition (`/cachehome/gradle-task-<index>`), the session-authored
  `oracle.*` behavior tests, RepoSense's pre-repeat priming command (its
  published command carries `-x compileTestJava`, which only works in a
  workspace an earlier run already compiled), the Discourse `node_modules`
  symlink into the retained shared Node tools, the proven OpossumUI
  `react-scripts` invocation, and the FluidFramework pnpm/mocha recipe.
- **Every pinned asset is required, hash-validated and fingerprinted.**
  `jvm_required_assets()` is the single source of truth that both
  `jvm_blockers()` (fail closed) and `asset_state()` (replay fingerprint) read,
  so an asset can never be mandatory for one and invisible to the other. Each
  `/oracle-cache` blob the recorded command references, each prefetched
  archive, and the pinned Gradle wrapper jar must exist **and hash to its
  pinned SHA-256** (full content, not a bounded prefix); a token with no pinned
  digest is itself a blocker. The cache directories the reconstructed
  `oracle.init.gradle` declares (`m2-mirror`) are validated the same way, by
  **whole-tree** content: `ORACLE_CACHE_DIR_SHA256` pins a `full_tree_digest()`
  over every file's relative path, mode/symlink target and complete bytes.
  Bounded `directory_identity()` sampling is deliberately *not* used for them --
  it hashes at most 500 files and only each file's first 64 KiB, so a mirror of
  thousands of artifacts could hide a removed, replaced or same-size late-file
  tamper, and resolution silently picking up different bytes would be scored as
  a candidate failure. An unpinned required directory is itself a blocker. On
  the TS/JS side validation binds the **whole resolved runtime payload**, not a
  launcher shim: `tsjs_required_trees()` requires each task's pinned dependency
  tree, pinned Node runtime and (for the pnpm-workspace contract) pinned pnpm
  store to digest to exactly the value recorded in the manifest --
  `full_tree_digest()` reads every regular file in full and binds each entry's
  relative path, size, executable bit and symlink target. On top of that,
  `tsjs_required_executables()` resolves the *concrete entrypoints that
  execute*: `vitest/vitest.mjs`, `vitest/dist/cli.js`, `vitest/dist/config.js`,
  `@vitest/coverage-v8/dist/{index,provider}.js`,
  `react-scripts/bin/react-scripts.js`, `react-scripts/scripts/test.js`,
  `jest-cli/bin/jest.js`, and the corepack modules the bare `pnpm`/`corepack`
  symlinks dispatch to. A tree containing only a shim and a `package.json`
  therefore **blocks**, and so does any tree with a single added, removed or
  edited file anywhere inside it. Only declared *runtime scratch* is excluded
  from a tree digest (Vitest's own `.vite` cache, pnpm's `v10/index` and
  `v10/projects` bookkeeping), because a tool legitimately rewrites those while
  running and including them would make the pin self-invalidating after the
  first replay; the Discourse contract additionally copies the
  retained shared Node tools instead of symlinking them, so a replay can never
  write back into the retained tree at all. `write_manifest.py` recomputes
  every pinned tree digest and fails if it disagrees with the digest the
  adapter manifests enforce, so durable-asset drift is caught at manifest
  generation rather than shipped. All of these are bound by full content digest
  into the replay fingerprint, so removing or tampering with any one
  invalidates previously recorded rows on resume instead of being resumed past.
- **A declared `prime_command` is mandatory.** RepoSense's published command
  carries `-x compileTestJava` and therefore finds no test classes at all in a
  workspace nothing has compiled yet, so the recorded pre-repeat command must
  run first. Its exit code, duration and error are captured into the row's
  `prime_result`; a nonzero exit, a timeout or a spawn error yields
  `infra_retry` with the prime log persisted as the row's evidence and
  `scored_command_executed: false` -- the scored command is never invoked, so
  the resulting "no tests found" build failure can never be scored as a model
  failure.
- **One complete identity function feeds all provenance.**
  `full_tree_digest()` is a full `lstat` traversal that binds every regular
  file by its whole SHA-256 plus size and executable bit, every symlink (to a
  file *or* a directory) by its literal target without following it, every
  directory by its path so an added, removed or emptied directory is visible,
  and any other node by kind; `full_entry_digest()` does the same for a single
  entry. There is no file-count cap and no per-file byte cap. The bounded,
  sampling, symlink-blind `directory_identity()`/`safe_file_digest()` helpers
  are **quarantined out of every replay fingerprint** on every lane -- native,
  rust, jvm and tsjs -- and a static guard test asserts they appear nowhere in
  `asset_state()`, `replay_contract_fingerprint()`, `row_provenance_fields()`
  or any of the required-asset resolvers.
- **Pinned assets are never mounted writable.** `/oracle-cache` -- the
  immutable jars, `oracle.init.gradle`, the `file://` m2-mirror, the pinned
  Gradle wrapper store and the prefetched archives -- is bind-mounted `:ro`,
  and the pinned Gradle/Maven cache home is not mounted at all. Gradle and
  Maven genuinely need a writable cache home, so `seed_jvm_cache_home()` copies
  just the entries the task needs (`.m2`, `.cache`, `.config` and *its own*
  `gradle-task-<index>` partition) into per-replay scratch and only that copy
  is mounted read-write; the only other writable mounts are the disposable
  `/workspace` and `/task`. The rust lane follows the same contract: the
  retained cargo cache is exposed only at `/cargo-cache-pinned:ro` while
  `CARGO_HOME=/cargo-cache` points at a per-replay seeded copy, and a seeding
  failure is an `infra_retry` before anything runs. Dependency trees are provisioned with
  `copy_tree()`, a real or copy-on-write copy -- never `cp -al`, whose shared
  inodes would let an in-place rewrite reach straight back into the pinned
  source. After every run the pinned assets, the required
  `/oracle-cache` directories and the pinned TS/JS trees are all re-digested
  defensively with the same whole-content functions preflight uses; drift is
  impossible by construction, so if it is ever observed the row becomes
  `infra_retry` (`pinned_assets_drifted_during_run` /
  `pinned_trees_drifted_during_run`) instead of a candidate verdict.
- **No live network fallback for a missing pinned asset.** A pinned asset that
  is absent or mismatched stops the replay before the Oracle command runs and
  is recorded as `infra_retry` with a `pinned_asset_faults` list -- never as a
  candidate failure, and never quietly skipped so the build can fetch a
  substitute. This holds for the two Oracles that keep a network namespace as
  well; in addition, every prefetched archive is re-hashed *after* the run, and
  a build that replaced it with a fresh download yields `infra_retry` rather
  than a score.
- Any task whose required Docker image, retained repo checkout, cache home,
  session harness, pinned dependency snapshot, pinned executable or Node
  runtime is missing is reported as a **preflight blocker** and is left out of
  the raw output entirely -- it is never scored as a pass/fail. `finalize
  --allow-incomplete` will show it under `missing_pairs` until the asset is
  recovered.

##### Recovered durable assets

Assets the retained session root no longer carries were reconstructed under a
dedicated, hashed asset root and are resolved through `--asset-root` at the
*same recorded relative path*, so the retained root always wins when it still
has an asset:

```text
/data/v-peiyangliu/reviewbench-openmodels/legacy-recovery-assets/jvm-tsjs
  manifest.json, SHA256SUMS
  strict_screen_20260726/oracle_recovery/java/cache/
    oracle.init.gradle      reconstructed (see below)
    jacocoagent.jar, jacococli.jar
    home/                   pinned Gradle/Maven cache home
    m2-mirror/              file:// mirror of the pinned cache, so the
                            JCenter-only 2021/2022 build-classpath artifacts
                            stay resolvable with no network
    gradle-wrapper/         pinned wrapper jars, keyed and verified by the
                            repository's own gradle-wrapper.jar.sha256
  tsjs/node_modules/opossum-tool__OpossumUI   pinned dependency snapshot
  tsjs/yarn-cache, tsjs/pnpm-store            pinned package-manager stores
  tsjs/_runtimes/node-v22.22.2-linux-x64      pinned Node runtime
  SHA256TREES             whole-payload digests of every pinned dependency
                          tree, Node runtime and package store, cross-checked
                          by write_manifest.py against the digests the adapter
                          manifests enforce (drift fails manifest generation)
```

`oracle.init.gradle` -- required by four of the JVM Oracle commands and lost
with the session -- is a *deterministic reconstruction*, not a guess. Each of
its behaviors is forced by retained evidence: `execute.py` attaches JaCoCo to
Gradle Oracles only through this script, yet the pure-Gradle task directories
still contain a populated `start.exec`; Skript's frozen golden log runs a
`:oracleSessionTest` task that does not exist in its own `build.gradle` (whose
`test` task is `test { exclude '**/*' }`); and the directly retained successor
of the file (`hard_oracle_20260804/jvm/cache/tools/oracle-gradle.init.gradle`)
disables project-configured JaCoCo. It also refuses to attach the agent when
the Oracle command already exports it through `JAVA_TOOL_OPTIONS`, because
double-attaching aborts every test JVM with a `LinkageError`.

##### Contract verification against frozen evidence

`verify-contracts` replays every recovered JVM/TS-JS Oracle at its exact
*unpatched* start, through the same code path scoring uses (an empty candidate
patch), and requires the frozen `pass_to_pass`/signature evidence to be
reproduced before a task is treated as runner-ready:

```bash
python3 scripts/run_legacy_reserve_correctness.py verify-contracts \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --legacy-root <retained-session-root> \
  --asset-root <recovery-asset-root> \
  --output <output>
```

Commands:

```bash
# 1) Stage patches for an arbitrary model across the full 414-task release
#    (allow the 40 published-legacy tasks to stay unresolved for now):
python3 scripts/run_open_model_correctness.py prepare \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --repo-cache <repo-cache> --output <output> \
  --model '<model>=<generation_results.jsonl>' \
  --allow-partial-modern

# 2) Fail-closed preflight (no execution):
python3 scripts/run_legacy_reserve_correctness.py preflight \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --legacy-root <retained-session-root> \
  --asset-root <recovery-asset-root> \
  --output <output> --allow-blocked

# 3) Replay only what preflight reports ready, for the staged model:
python3 scripts/run_legacy_reserve_correctness.py replay \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --legacy-root <retained-session-root> \
  --asset-root <recovery-asset-root> \
  --output <output> --model '<model>'

# 4) Finalize as usual (missing/blocked pairs remain visible, not fabricated):
python3 scripts/run_open_model_correctness.py finalize \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --output <output> --allow-incomplete
```

As originally written (2026-08-11), a read-only `preflight` pass against the
retained session root reported 7 of the 14 tasks ready (ClickHouse, dpnp,
memilio, rippled, singularity-eos, discourse, opossum-tool/OpossumUI) and 7
blocked: Desbordante (harness recipe was never proven historically -- every
historical candidate patch failed to apply through it), the 4 remaining JVM
tasks plus web3signer's `-javaagent`/`-I` flags (missing recorded Gradle
cache home and `/oracle-cache` assets under
`oracle_recovery/java/cache/`), and FuelLabs/fuel-tx (missing recorded
cargo cache under `oracle_recovery/rust/cache/cargo`). These are reported,
not invented; recovering the missing cache directories (or the Desbordante
proof) moves those tasks to "ready" without any code change.

The native/rust half of that list has since been recovered (including the
Desbordante proof and the rust CARGO_HOME) -- see 7.1.2. The historical
native/rust start workspaces have themselves been deleted from the retained
session root since then, so those tasks are now replayed exclusively through
the recovered-asset contracts described there; the JVM/TS-JS tasks are
unaffected by this section's scope. `--task-id` scope also now spans the four
native/rust published-legacy tasks that were never part of the 20260728
reserve (apache/tsfile, grpc/grpc, python/cpython, otter-sec/anchor), which
are blocked unless `--asset-root native-rust=<path>` supplies a verified
contract for them.

Status after the 2026-08-13 JVM/TS-JS recovery: preflight reports 9 of the
10 JVM/TS-JS published-legacy tasks ready (all 6 JVM plus discourse,
OpossumUI and FluidFramework). The one remaining blocker is
angular/components: its increment5 recovery directory
(`increment5-recovery/ts-js/angular-components-24601`), which held the
session-authored `oracles/oracle.ts` its `esbuild session oracle; c8 node
oracle-angular-start.js` command executes, is not present under any retained
root. There is no executable Oracle to replay and one will not be invented, so
it stays a hard preflight blocker rather than a contract that would score every
candidate as a failure.

The historical 11-model replay artifact (7 ready tasks at the time, 79
normalized rows: 33 pass, 38 fail, 8 candidate-missing) is unchanged and
remains stored at:

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-evaluation-20260811/correctness/
  correctness-20260811-legacy-20260728-eleven-v1.tar.gz
SHA-256 8cfa350847d4a86a612b4be4f278ac7c6b4fe64a5f453439a81f4f059598acfd
```

> **Cherry-pick note.** The native/rust legacy paths are also being reworked on
> the sibling `legacy-recover-native-rust` branch. The changes here are kept
> deliberately narrow -- the shared generic digest helpers
> (`full_tree_digest`/`full_entry_digest` and the quarantine of the bounded
> helpers) plus the reserve JVM and Rust replay paths -- but
> `replay_rust()`'s mount list and `asset_state()`'s native/rust branches are
> the most likely cherry-pick conflicts. Resolve them by keeping *both* the
> generic helper swap (bounded -> full digests) and whichever lane-specific
> recipe that branch lands, since the two are orthogonal.

`tests/test_run_legacy_reserve_correctness.py` covers this adapter with
tiny synthetic fixtures only (no real builds/containers): patch
apply/failure handling, exact-start materialization, blocker detection per
lane, fail-closed behavior for tasks without a recovered manifest,
asset-root resolution, the Discourse shared-node-tools symlink, refusal to
pass a green run that executed no tests, drift detection against the
task-ID manifest, resume/skip behavior, and candidate-missing rows that
normalize cleanly through the real `normalized_record()`. It also removes or
tampers with each pinned asset and each required executable in turn --
`/oracle-cache` blobs, the prefetched archive, the pinned wrapper jar, the
`m2-mirror` directory, `.bin/vitest`, `.bin/react-scripts`, their concrete
entrypoints, the corepack dispatch targets and the pinned `pnpm` shim,
including same-length in-place content swaps and a cleared executable bit --
and asserts that preflight blocks, that
`asset_state()`/`replay_contract_fingerprint()` change, that `row_is_stale()`
invalidates already-recorded rows, and that `replay_jvm` refuses to run the
Oracle command at all. A dedicated case builds a dependency tree containing
*only* the launcher shim and its `package.json` and proves it is rejected, and
others prove that a single stray file inside a pinned dependency tree, Node
runtime or pnpm store blocks. Digest cases prove the complete identity function catches every
mutation bounded sampling misses -- a tail edit past 64 KiB, an edit beyond the
500-file cap, a same-size swap, a symlink retargeted between byte-identical
files, a removed empty directory and a retargeted directory symlink -- each
asserting in the same test that `directory_identity()` is unchanged, and a
parametrised case checks every lane's `asset_state()` reports only complete
digests. Whole-tree cases build an `m2-mirror` of more than 500
files, each padded past 64 KiB, and prove that a same-size tamper in the last
file, a same-size tamper past the sampled prefix, and a removed artifact each
block -- asserting in the same test that `directory_identity()` is blind to
them -- and that mid-run drift of a required directory yields `infra_retry`
with no verdict. Immutability cases assert the container argv
mounts `/oracle-cache` as `:ro` and never mounts the pinned cache home at all,
that seeding copies only the task's own Gradle partition into independent
inodes, that an in-place rewrite (and a write through a `.bin` symlink) in a
provisioned copy leaves the pinned source byte-identical, and that simulated
"impossible" drift of a pinned asset or tree during a run yields `infra_retry`
rather than a verdict. The mandatory-prime cases cover a nonzero exit,
a timeout, a spawn error and an error string with a zero exit, each asserting
`infra_retry`, a persisted prime log, `scored_command_executed: false` and
that the scored command was never invoked.

#### 7.1.2 Native/Rust recovered-asset bundle (11 of the 40)

The 20260728 adapter above could only replay a native/rust task while the
historical harness assets still existed. They no longer do: every
`.../native-real/tasks/<case>/start` workspace subtree was deleted, the rust
Oracle's recorded `oracle_recovery/rust/cache/cargo` CARGO_HOME never
survived, and four in-scope tasks (apache/tsfile, grpc/grpc, python/cpython,
otter-sec/anchor) were recovered in a session whose artifacts are not retained
on this machine at all.

`scripts/recover_legacy_native_rust.py` rebuilds that missing half and records
it as a self-describing bundle:

```text
<assets-root>/
  recovery-manifest.json   # one verified replay contract per task
  SHA256SUMS               # every authored/retained small file
  workspaces/<case>/start  # exact-start checkout (git HEAD == task start_sha)
  harness/<case>/          # re-authored harness + derived source-only golden
  caches/{cargo,cargo-anchor,conan}   # reconstructed pinned build caches
  proofs/<case>.json       # recorded start/golden proof runs
  logs/                    # full build/run logs for every proof
```

Exact-start workspaces are re-materialized from retained full repo objects
(`git clone --shared` + `checkout --detach <start_sha>`), then made
self-contained by packing the HEAD snapshot locally and dropping the
`objects/info/alternates` link -- otherwise a build that shells out to `git`
inside a container (rippled's `GIT_COMMIT_HASH`) cannot read its own object
store. Vendored dependency trees that were part of the historical workspace
are restored from the retained per-case archives (singularity-eos'
`utils/spiner` + `utils/variant`, Desbordante's `lib/easyloggingpp` +
`lib/better-enums` from the recovery image's `/opt/deps`), and dpnp keeps the
retained dpctl-0.19 `dependency-compatibility.patch` that the historical
recovery applied identically to start and golden.

`run_legacy_reserve_correctness.py` consumes this bundle through
`--asset-root native-rust=<assets-root>` (see 7.1.3; the bare
`--asset-root <path>`, the deprecated `--assets-root` spelling and
`REVIEWBENCH_LEGACY_ASSETS` all still work). A recovered contract supersedes
the historical one for the tasks it covers, and **every input the replay
actually mounts** is re-verified against live state on each preflight *and*
again immediately before the Oracle runs:

- the exact-start workspace must still be at the task `start_sha` **and**
  match the manifest's recorded `workspace_state`: same modified tracked paths
  (dpnp legitimately carries the retained dpctl-0.19 compatibility patch),
  same untracked entries, same `git submodule status` lines, **and the same
  full lstat+content digest of the entire copied tree with only `.git`
  excluded**. That last field is what catches build residue git deliberately
  hides -- a `.gitignore`d `build/`, `target/` or `*.o` left in the workspace
  is invisible to `git status` yet is copied into every replay scratch -- so
  ignored residuals, symlinks and empty directories all block. It is not
  free: ClickHouse's 52 GB workspace digests 586,242 entries in ~2 min, which
  is the deliberate price of a fail-closed provenance check;
- every authored harness file, golden patch and harness-copy source must
  still hash to its recorded SHA-256;
- every mounted cache -- both `cache_mounts` (rippled's Conan cache) and the
  rebuilt CARGO_HOMEs -- must be present *and* match its recorded tree digest,
  and a CARGO_HOME missing `bin/cargo` is reported as incomplete;
- **every other replay-mounted input** is bound the same way through
  `mount_input_digests`: single mounted files (ClickHouse's `query.sql`,
  dpnp's `behavior_test.py`), the whole workspace directory a `mount: "."`
  recipe exposes next to `start/` (`oracle.cpp`, the retained source-only
  patches, `checkpoint.json`), read-only harness directories and harness-copy
  sources. The exact-start subtree is the only mounted input not in that map,
  because it is verified through git `workspace_state`; a mount source that
  the manifest never bound is itself a blocker;
- all of those digests are **full-content SHA-256** over every regular file
  (plus path, mode, size and symlink target), never path/size/mode alone, so a
  same-size, same-mtime byte swap inside a cache, a harness file or a mounted
  workspace sibling is detected and blocks. Hashing is parallelized (16
  threads); the 7.8 GB Conan cache and the 166 MB CARGO_HOME together digest
  in ~35 s per verification pass;
- the traversal is an `lstat`-based enumeration of **every path entry**, not an
  `os.walk` over files: regular files (bytes), symlinks to files *and to
  directories* (link target, never followed), and real directories including
  empty ones each contribute a record. `os.walk` reported neither symlinked
  directories nor empty directories, which left them unbound -- and the real
  rippled Conan cache contains both (e.g. `grpc/1.50.1/.../spm-core-include/
  grpc` is a directory symlink, alongside dozens of empty directories), so
  retargeting or deleting one used to be invisible. Both tools share one
  implementation (`_tree_digest_impl`) and produce byte-identical digests;
- the Docker image must still be loaded with the recorded image id;
- the manifest must carry a successful start-tree proof run **and**, for any
  contract that declares a source-only golden patch, a successful golden
  proof run.

Proof runs are themselves bound to a **cryptographic recipe fingerprint**
covering the executed `command`/`golden_command`, shell, user, network, env,
workdir, every mount and harness mount/copy, the Docker image name *and*
loaded image id, the workspace state, every mounted cache's tree digest, the
SHA-256 of every authored input, and the replay adapter's own
`ADAPTER_CONTRACT_VERSION`. `cmd_manifest` and `proof_blockers()` only count
proof runs stamped with the current fingerprint, and the adapter rejects a
manifest entry whose recorded proof runs carry a different one, so editing a
command, rebuilding an image or touching a harness byte turns previously
recorded proofs into *stale evidence* rather than silently reusable ones.

All lanes share that one implementation. The bounded `directory_identity`
(500-file traversal cap, 64 KiB read cap per file) and `safe_file_digest`
helpers that used to fingerprint the *historical* native/rust/JVM/TS-JS trees
and caches are gone: a JVM Gradle cache home, a `/oracle-cache` jar, a TS/JS
`node_modules` tree, a pinned Node runtime, a retained repo checkout and a
recovered cache are now all bound by full lstat+content digests, so a change
to the 900th file, a change past a file's first 64 KiB, a retargeted symlink
or an added/removed empty directory can no longer slip through any lane's
replay-contract fingerprint.

Concurrency is fail-safe as well: `cmd_replay` holds a cross-process
`execution_lock(lane, model, task)` from scratch creation through evidence
persistence and cleanup, each invocation gets a unique scratch directory
(`invocation_scratch`, PID + UUID suffixed), and evidence logs are published
by copy-to-`.new`-then-`os.replace`. Two processes replaying the same pair
therefore cannot delete each other's workspace mid-build, interleave a log, or
leave a half-written artifact behind.

Proofs never run against the canonical workspace: `cmd_smoke` materializes a
disposable copy per variant (start and golden alike), applies the golden patch
and the harness there, runs the container against the copy, purges it, and
then re-reads the canonical `workspace_state` and records a blocker if the
proofs changed it. `cmd_manifest` additionally refuses to promote a contract
whose canonical workspace no longer matches the state its proofs attested.
Mounted caches are likewise never exposed to a run that could write to them:
both `cmd_smoke` and `replay_recovered` copy each cache into their disposable
scratch and mount the copy read-write, and the adapter's cache digests are
recomputed from disk on every call (no memoization) plus explicitly
invalidated before each pre-execution check and after each replay, so a cache
mutated by one replay cannot be waved through by the next check in the same
process.

A manifest that predates these fields cannot be verified and is therefore
itself a blocker. All of it is folded into `recovered_asset_state()` and hence
into each raw row's `replay_contract_fingerprint`, so a drifted workspace,
mutated cache, retagged image or edited command invalidates previously
recorded rows on resume -- and `cmd_replay` retracts any row whose task has
become blocked. If drift is only discovered at replay time, `replay_recovered`
returns an `infra_retry` row carrying the exact `asset_blockers` **before**
touching the workspace, so a broken asset can never be recorded as a candidate
failure. Live verification against the real bundle: injecting one stray file
into a workspace, or one extra file into the mounted CARGO_HOME, flips the
affected task to blocked (`recorded 8356 files ... live 8357 files`) and
retracts its previously recorded row; removing the file restores ready.

Two lane-level bugs were fixed while proving this end to end, both of which
would silently mis-score a *passing* replay:

- the rust lane ran its command through a **login** shell (`sh -lc`), which
  re-sources `/etc/profile`, drops `$CARGO_HOME/bin` from `PATH` and fails
  with `cargo: not found`; the historical evaluator used `/bin/sh -c`;
- rust pass counting used the generic first-match counter, which reads only
  the first test binary's `test result: ok. N passed` line -- 40 of fuel-tx's
  77 recorded pass-to-pass tests -- and would have judged a fully passing run
  as a failure. `rust_test_count()` now sums every target, like the
  historical rust evaluator.

Recovery status (proven by re-running each contract here, not assumed):

| Task | Contract | Proof |
| --- | --- | --- |
| ClickHouse/ClickHouse #74193 | recovered | start: full 13,413-target clang-19 build, oracle query output byte-identical to the historical `start-runs.txt`; golden: retained `source-only.patch` rebuilds and passes (295 s) |
| Desbordante/desbordante-core #154 | recovered | start: stdout SHA-256 `50d9c3cd...` byte-identical to the historical proven start run (previously blocked as "never proven end-to-end"); golden: the recorded object-archive recipe (`golden_command`) passes with identical behavioural output |
| IntelPython/dpnp #2201 | recovered | start + golden: `dpnp.choose public behavior: 5 cases passed` |
| SciCompMod/memilio #690 | recovered | start + golden: oracle binary builds and exits 0 |
| XRPLF/rippled #5143 | recovered | start + golden: `2175 tests total, 0 failures` (matches the recorded 2175) |
| lanl/singularity-eos #75 | recovered | start + golden: oracle binary builds and exits 0 (shared `-include typeinfo`, see below) |
| apache/tsfile #823 | recovered (re-authored harness) | start (repeatably) + golden: `OracleAlignedBatch.DoubleValuesPreserveRowsAndValues` passes |
| python/cpython #143314 | recovered (selected P2P tests) | start (x2) + golden: 2 unchanged `test_os` tests that drive `parse_envlist()` pass |
| FuelLabs/fuel-tx #197 | recovered (rebuilt CARGO_HOME) | start + golden: 77 passed, offline (`--network=none`) after deleting `target/`, matching the recorded `pass_to_pass: 77` |
| otter-sec/anchor #2488 | recovered (re-authored harness) | start + golden: both recorded tests pass, offline |
| grpc/grpc #32970 | **blocked** | no retained replay contract at all (Oracle carries only counters/coverage from `legacy159_oracle_native_normalized.jsonl`), the retained `repos/grpc__grpc` checkout does not contain the task `start_sha`, and no pinned Bazel toolchain/output-base cache for the 2023 tree was retained |

Every recovered contract declares the source-only golden it must satisfy (the
retained per-case patch, or one derived strictly from the Oracle's own
`golden_scope`/`scope_expansion` paths), and **a contract cannot be ready
without a successful golden proof**: `cmd_manifest` requires a passing start
*and* golden run, and `cmd_smoke` records a golden patch that fails to apply
as an explicit `stage: "apply"` failure run (with its log) instead of skipping
the variant, which keeps the contract out of the ready set. Two goldens needed
recorded, documented recipes rather than the start-side command:
Desbordante's golden removes the `Desbordante_lib` target and references
unrelated-branch headers, so its `golden_command` reproduces the historical
"build the real production objects, archive, link with -std=c++20" recipe;
singularity-eos' reference golden introduces `typeid(CRTP)` without including
`<typeinfo>` (upstream's target commit does not include it either), so
`-include typeinfo` is added to the *shared* command -- applying it only to
the golden would unfairly fail any candidate that mirrors the reference fix.

Re-authored harnesses are real behavioral tests, never source-shape checks:
the tsfile oracle writes six aligned fixed-width `DOUBLE` rows through the
public writer API and reads them back through the public reader/result-set
path, and the anchor oracle drives the public async client against an
in-process loopback JSON-RPC server (a compile-time `IntoProgram` trait
absorbs the start-vs-golden `Program` / `Result<Program>` public API
difference without inspecting source text or branching on candidate symbols).
Both pass on start *and* on the golden derived strictly from the Oracle's own
`golden_scope`/`scope_expansion` paths, as a PASS_TO_PASS oracle requires.

Commands:

```bash
# Rebuild/refresh the bundle manifest + checksums (fails closed per task):
python3 scripts/recover_legacy_native_rust.py manifest \
  --assets-root <assets-root> --legacy-root <retained-session-root>

# Re-prove one contract (start tree, plus golden where recovered):
python3 scripts/recover_legacy_native_rust.py smoke \
  --assets-root <assets-root> --case 22-rippled --golden

# Preflight/replay through the existing adapter, now asset-aware:
python3 scripts/run_legacy_reserve_correctness.py preflight \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --legacy-root <retained-session-root> \
  --asset-root native-rust=<assets-root> \
  --output <output> --allow-blocked

python3 scripts/run_legacy_reserve_correctness.py replay \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --legacy-root <retained-session-root> \
  --asset-root native-rust=<assets-root> \
  --output <output> --model '<model>' --work-root <scratch>
```

The bundle produced on 2026-08-14 lives at
`/data/v-peiyangliu/reviewbench-openmodels/legacy-recovery-assets/native-rust`
(`recovery-manifest.json` SHA-256
`95deeeef08b1aa5e67e37da8f1943283182f4b626978c3142232b07009e8d1e6`,
`SHA256SUMS` SHA-256
`7698777c71fbf62ac21cbc2d3de83eadfe91fea7a36cad8991cef525cbebf477`). The
Conan cache alone now binds 142,680 entries -- 128,802 files, 13,853
directories and 25 symlinks -- where the previous file-only traversal bound
128,802.
Live checks against that bundle: a same-size byte swap in ClickHouse's mounted
`query.sql`; a same-size byte swap inside a `.crate` file in the mounted
CARGO_HOME; retargeting the `grpc/1.50.1/.../spm-core-include/grpc` *directory
symlink* in the Conan cache; and adding a single empty directory to it -- each
blocks the affected task ("has drifted from its recorded content digest"),
and restoring the original state returns each to ready. A `.gitignore`d
`build/residual.o` dropped into the memilio workspace -- which `git status`
reports as nothing at all, because that repo's 278-line `.gitignore` hides it
-- also blocks (10/11 -> 9/11 ready) and clears when removed. Two concurrent
`replay` invocations of the same (lane, model, task) each executed exactly
once, published an intact evidence log with no `.new` residue, and left no
scratch behind.
Every contract's start and golden proof in that bundle was executed on a
disposable copy, and a post-proof replay confirms the canonical CARGO_HOME
(8,343 files) and workspaces are byte-identical afterwards.

A real preflight over the full release with the bundle reports 10 of the 11
native/rust published-legacy tasks ready (grpc alone blocked), and a real
replay of a staged model produced genuine behavioral verdicts -- passes for
singularity-eos and cpython, and *model* failures (broken candidate patches:
a template-arity error in memilio, unbalanced delimiters in fuel-tx and
anchor) that are recorded as `fail`, never as infrastructure noise. Scratch
copies, including root-owned container build state, are removed through a
throwaway cleanup container (`purge_scratch`); shared caches are never
deleted.

#### 7.1.3 Combined JVM/TS-JS + Native/Rust integration

The JVM/TS-JS recovery (7.1.1) and the native/rust recovery (7.1.2) were
developed separately and produced two *different kinds* of durable asset root.
They are consumed together by one adapter, so `--asset-root` is repeatable and
each value is either `LABEL=PATH` or a bare `PATH` classified by the manifest
it actually holds (never by its directory name):

| label | marker | schema | role |
|---|---|---|---|
| `jvm-tsjs` | `manifest.json` | `reviewbench-legacy-recovery-assets/v1` | pinned assets the retained session root no longer carries; recorded Oracle paths resolve there only when the retained root lacks them |
| `native-rust` | `recovery-manifest.json` | `reviewbench-legacy-native-rust-recovery/v1` | whole recovered replay contracts that supersede the historical ones for the tasks they cover |

A bare path that matches neither or both manifests is a hard error rather than
a guess, and the same label may not be given two different roots. Backward
compatibility is preserved: a single bare `--asset-root <path>`, the
deprecated `--assets-root` spelling and `REVIEWBENCH_LEGACY_ASSETS` (which may
itself list both roots, `os.pathsep`-separated) all still work, and passing no
root at all reproduces the original historical-only behavior.

```bash
python3 scripts/run_legacy_reserve_correctness.py preflight \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --legacy-root <retained-session-root> \
  --asset-root jvm-tsjs=<.../legacy-recovery-assets/jvm-tsjs> \
  --asset-root native-rust=<.../legacy-recovery-assets/native-rust> \
  --output <output> --allow-blocked
```

**Digest surface.** Every bounded/sampling helper (`directory_identity`,
`safe_file_digest`, `_bounded_file_digest`, the 500-file and 64 KiB caps) has
been deleted from the adapter outright, not merely quarantined. Exactly two
complete identity functions remain, and both bind every path entry -- regular
files by their whole content, symlinks by their literal target without ever
following them, and directories including empty ones:

- `content_tree_digest`/`file_input_digest` for the **live, writable** mounts a
  recovered native/rust contract exposes. Never memoized, because the process
  under test writes into them.
- `full_tree_digest`/`full_entry_digest`/`full_file_digest` for the **pinned,
  read-only** jvm/tsjs payloads whose digests are frozen in the manifests.
  These do memoize, so `invalidate_cache_digests()` clears *every* memo in the
  module and is called at the start of each preflight and before each
  post-run drift check -- otherwise a drift check could answer from a pre-run
  memo, since the tree memo key deliberately carries no mtime.

**Preserved semantics.** The merge is a strict superset. From the native/rust
side: ignored-build-residue detection over the full copied-tree identity,
recipe-bound start+golden proofs on disposable copies, per-`(lane, model,
task)` execution `flock`, per-invocation unique scratch, atomic evidence
publication and no memoized live digests. From the JVM/TS-JS side: pinned
file/tree/directory hashes, exact executable and runtime payload validation,
an immutable `/oracle-cache:ro` mount, a per-replay seeded writable JVM cache
home, non-hardlinked TS/JS copies, the mandatory RepoSense prime that reports
`infra_retry` on failure, and post-run drift checks. Both lane groups' replay
paths now go through the shared execution lock and unique scratch, so a
combined run of jvm and native replays proceeds in parallel without sharing
scratch or interleaving evidence.

`ADAPTER_CONTRACT_VERSION` is bumped to a single merged value
(`v6-2026-08-14-integrated-legacy-asset-recovery`). This is deliberately
neither branch's value: rows recorded under either predecessor contract must
be re-run rather than silently reused. Because the native/rust recipe
fingerprint folds in that constant, every recorded proof run became stale, and
the adapter blocks a recovered contract that carries *any* proof from a
superseded fingerprint. All 10 recovered contracts were therefore re-proved
end to end on disposable copies with
`recover_legacy_native_rust.py smoke --golden --reset` -- 20 runs (start and
golden for each), all exit code 0 -- before the bundle manifest was
regenerated. This is the tool's documented recipe-change workflow, not a
rewrite of frozen results: the superseded runs' full build/run logs remain
under `<assets-root>/logs/`.

Both manifest writers were then re-run twice and are zero-drift: the jvm/tsjs
`SHA256SUMS` and `SHA256TREES` are byte-identical across runs and
`manifest.json` differs only in `created_at`, and the same holds for the
native/rust `SHA256SUMS` and `recovery-manifest.json`. The jvm/tsjs writer's
pinned-tree cross-check (12 pinned trees and files) was additionally re-run
against the *merged* adapter's `full_tree_digest`/`full_file_digest` with zero
mismatches.

**Combined status.** A real preflight over the full release with both roots
reports **19 of the 21** unique JVM+TS-JS+native+rust published-legacy tasks
ready -- all 6 jvm, 3 of 4 tsjs, 8 of 9 native and both rust -- including all
14 original reserve-20260728 tasks. The two blockers are unchanged and remain
hard, fail-closed blockers rather than fabricated contracts:

- `angular/components` -- its increment5 recovery directory, which held the
  session-authored `oracles/oracle.ts` its command executes, is not present
  under any retained or recovered root;
- `grpc/grpc` -- the release Oracle carries only normalized results with no
  command, image or harness, the retained checkout does not contain the task
  `start_sha`, and no pinned Bazel toolchain/output-base cache was retained.

**Proof-run readiness is fail-closed on current evidence.** A recovered
contract's recorded proof runs are judged only at the manifest's current
`recipe_fingerprint`; stale runs from a superseded recipe are dropped (and
reported). Among the remaining runs, **any** failure blocks, and the **latest**
start run -- plus the latest golden run where a source-only golden is declared
-- must both have succeeded. An old success followed by a later failure at the
same fingerprint is a contract that stopped working, so it never re-enters the
ready set; `smoke --golden --reset` is the supported way to clear a recorded
failure. `recover_legacy_native_rust.proof_blockers` and the adapter's
`recovered_blockers` implement the identical rule, so the recovery tool and the
replay adapter can never disagree about which contracts are runnable.

**Asset-root provenance is lane-specific.** A combined invocation supplies both
roots, but a native/rust row's replay never reads the `jvm-tsjs` pinned-asset
root and a jvm/tsjs row's replay never reads the `native-rust` bundle.
`lane_asset_root()` therefore resolves exactly one root per row -- the
`native-rust` bundle for recovered contracts and for every `native`/`rust`
task, the `jvm-tsjs` root for every `jvm`/`tsjs` task -- and that value, not
whatever the CLI happened to be given, is what
`replay_contract_fingerprint`/`asset_state` bind and what `cmd_replay` threads
into `row_provenance_fields` and the replay functions. Adding the other lane
group's root to a command line therefore cannot invalidate a row, while moving
a lane's *own* root still does. The normalization also happens *inside* the
fingerprint and asset state, so a caller that passes the raw CLI root cannot
reintroduce the drift.

Real combined runs against both roots, on the merged branch:

| command | scope | result |
|---|---|---|
| `preflight` | full release, both roots | 19 ready / 2 blocked of 21; 6/6 jvm, 3/4 tsjs, 8/9 native, 2/2 rust; 14/14 reserve-20260728 |
| `verify-contracts` | solr, OpossumUI, FluidFramework, angular | solr 215/215, OpossumUI 2/2, FluidFramework 152/152 reproduced at their unpatched starts; angular blocked |
| `replay` (one invocation) | singularity-eos (native), anchor (rust), OpossumUI (tsjs), RepoSense (jvm) | native `pass`, rust `fail` (broken candidate patch, not infra), tsjs `pass` with `pinned_trees_drifted_during_run: []`; RepoSense `infra_retry` because the candidate's own Java does not compile and the mandatory prime therefore fails before the scored command; no scratch left behind |
| `replay` again, same arguments | as above | `skipped_resume: 4`, `stale_reran: 0`, `executed: 0` -- the lane-specific asset-root provenance makes a combined resume a no-op for all four lanes |

#### 7.1.2 Go/Python + scripted legacy replay adapter (19 of the 40)

`scripts/run_legacy_gopy_scripted_correctness.py` closes the gap for the
other large slice: the 19 published-hard tasks whose lane is `gopy` (15) or
`scripted` (4). Their per-task replay recipes are recorded in a reviewable
data file, `data/legacy_contracts/gopy_scripted_20260808.json`
(`reviewbench-legacy-replay-contract/v1`), so the executable contract is
auditable independently of the code that runs it. Every one of the 19 IDs
appears there exactly once, either `recovered` (with a fully pinned recipe)
or `blocked` (with the exact lost assets, the evidence that they are lost,
and what re-engineering would be required).

Like the reserve adapter, it writes only
`output/legacy/correctness/{gopy,scripted}.jsonl` -- the files `finalize()`
already merges and normalizes through the same `normalized_record()` used
for the 374 modern tasks -- and it consumes the already-staged,
checksum-verified `generation_results.normalized.jsonl` from
`prepare --allow-partial-modern`.

Recovered recipe kinds (all replay the historical behavioral Oracle; no
source-shape substitute is ever used):

- `go_docker_p2p` (aws/eks-anywhere#2274, pingcap/tidb#68028): fresh
  `git worktree` of the retained clone at the exact `start_sha`, staged
  candidate patch applied, then the historical container (`golang:1.25-bookworm`,
  the task's own dedicated `rb-wave2-*-mod`/`-build` Go cache volumes,
  recorded `network: host`) runs
  `go test -count=1 -json -run <oracle.test_selector> <test_target>` from the
  recorded `module_dir`. Correctness requires *every* name in the Oracle's own
  `pass_to_pass_tests` list (23 and 162 respectively) to report a `pass`
  action in the JSON event stream; a build failure or a vanished test counts
  as a failure, never as absent.
- `go_docker_session_oracle` (unikraft/kraftkit#2007): same materialization
  plus the retained, SHA-256-pinned session Oracle
  (`initrd/recovery_oracle_test.go`, recovered from the incremental-go-2
  recovery artifacts) copied into the worktree, then the historical
  `go test -tags containers_image_openpgp -run ^TestRecoveryDirectoryCPIO$
  ./initrd` under `golang:1.24.0-bookworm` (the recorded *start*-revision
  toolchain), `CGO_ENABLED=0`, `GOTOOLCHAIN=local`, offline.
- `python_image_pytest` (scikit-learn#18359): the historical image
  `reviewbench-sklearn-18359` bakes the exact start checkout (built in place
  with Python 3.8.20 / numpy 1.19.2 / scipy 1.5.2 / Cython 0.29.21 /
  pytest 6.1.1) plus the original `run-recovery` harness; a disposable
  container applies the candidate patch to that in-image workspace and runs
  the historical `pytest -q sklearn/inspection/_plot/tests/
  test_plot_partial_dependence.py --junitxml=...`, scored from the JUnit
  report against the recorded 55 passing tests.

Fail-closed guarantees: a task is replayed only when its contract entry is
`recovered` **and** every live asset resolves right now (repo checkout
containing the exact `start_sha`; Docker image present and matching the
recorded image ID; Go module/build cache volumes present; session Oracle
asset matching its recorded SHA-256; and, for the in-image workspace, an
anchor file byte-identical both to the recorded digest and to the repository
blob at `start_sha`). Anything missing is a preflight blocker: the task is
never scored, never guessed, and never reported as a model failure. Blocked
entries additionally re-verify their recorded `missing_assets`; if all of
them reappear, preflight reports `blocker_possibly_resolved` and fails, so a
recovered asset cannot be silently ignored. Nothing is fetched from the
network to make a run succeed: containers use the recorded network mode,
Go runs with `GOTOOLCHAIN=local`, and dependency resolution comes only from
the retained, `go.sum`-verified module caches.

Recovered assets live outside the repository (they are large and/or
binary) under a dedicated asset root with a `MANIFEST.json` and
`SHA256SUMS`:

```text
/data/<user>/reviewbench-openmodels/legacy-recovery-assets/gopy-scripted/
  assets/kraftkit-2007/oracle-start_test.go      # session Oracle (pinned)
  assets/sklearn-18359/run-recovery.sh           # historical harness copy
  caches/rb-wave2-eks-anywhere-2274-mod.tar.gz   # go.sum-pinned module cache
  caches/rb-wave2-tidb-68028-mod.tar.gz
  caches/incgo2-mod124kraft.tar.gz
```

`restore-caches` recreates a lost Go module cache volume from those packaged
archives after verifying them against `SHA256SUMS` -- never from the network,
and never over an existing volume.

Commands:

```bash
# 1) Stage patches as in 7.1.1 (prepare --allow-partial-modern).

# 2) Fail-closed preflight of the 19 gopy/scripted contracts (no execution):
python3 scripts/run_legacy_gopy_scripted_correctness.py preflight \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --legacy-root <retained-session-root> \
  --asset-root  <legacy-recovery-assets>/gopy-scripted \
  --session-files <historical REVIEWBENCH_SESSION_FILES root> \
  --output <output> --allow-blocked

# 3) Replay only what preflight reports ready, for one staged model:
python3 scripts/run_legacy_gopy_scripted_correctness.py replay \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --legacy-root <retained-session-root> \
  --asset-root  <legacy-recovery-assets>/gopy-scripted \
  --output <output> --model '<model>' --allow-blocked

# 4) Finalize as usual (blocked pairs remain visible, not fabricated).

# Optional: rebuild a lost module cache volume from the packaged archive.
python3 scripts/run_legacy_gopy_scripted_correctness.py restore-caches \
  --asset-root <legacy-recovery-assets>/gopy-scripted
```

##### Engineered Oracles for the lost harnesses

A lost historical harness is treated as an engineering task, not a terminal
blocker. For every task whose session Oracle/harness did not survive, a **new**
strict Oracle was derived from the authoritative task instruction and review
comment, the exact-start source, the accepted source-only golden delta, and the
repository's own public tests, and then proven against the current hard-Oracle
standard by `validate-oracle`:

* **exact-start baseline pass** -- the Oracle passes on the untouched start
  revision, three runs in a row;
* **source-only golden pass** -- it still passes after the maintainer's own
  accepted production delta is applied, so it is not coupled to the start
  implementation (a recorded `coupled_test_file_overlay`, `golden_patch_asset`,
  paired `golden_session_tests` or `golden_recipe_overrides` may be part of the
  golden definition, and each is pinned explicitly in the contract);
* **nonempty PASS_TO_PASS** -- every judged test is named in the contract;
* **changed-old production coverage** -- at least one production line the
  accepted change removed or rewrote is executed on the start revision,
  measured with a real tool (`go test -coverprofile`, `coverage.py`,
  `sys.settrace`, or bash xtrace line tracing), never inferred from source
  shape;
* **repeated stability** and a fully offline final command.

Every engineered Oracle drives public API/observable behavior only. Two earlier
drafts were rejected and rebuilt for violating that rule (one tested an
unexported Go helper, one used `unsafe.Pointer` to reach a struct field, and a
third implemented the production class's private extension hooks). One draft
was rejected for reading the historical image's baked source copy instead of
the mounted workspace, which would have made it blind to the code under test.

Three additional recipe kinds carry these Oracles:
`python_docker_session_oracle` (pytest in a version-pinned image),
`shell_docker_session_oracle` (a black-box harness driving a production script
through controlled fake executables, with bash xtrace line coverage), and
`sbt_docker_session_oracle` (the project's own sbt build against a packaged,
hash-verified ivy/coursier closure).

Status as of 2026-08-14: **15 of 19 tasks are replay-ready, and every one of
them is proven by real execution in the validating run itself.**
`validate-oracle` has no attestation-only path and no attested-coverage
fallback: each Oracle is executed now -- exact start x3 and golden x3 -- and
judged on its own output, PASS_TO_PASS set and changed-old production coverage
**measured by that run**. A historical measurement may be carried in the
contract for reference, but it can never satisfy the coverage requirement.

| Task | Oracle | P2P | Golden mode | Coverage measured in-run |
|---|---|---:|---|---|
| aws/eks-anywhere#2274 | repository suite | 23 | source-only (+5 deleted production paths) | go cover |
| pingcap/tidb#68028 | repository suite | 162 | source-only (published 407-path set) | go cover (golden phase, as published) |
| unikraft/kraftkit#2007 | retained session Oracle | 1 | target-commit + paired golden test + golden toolchain | go cover |
| scikit-learn#18359 | in-image harness re-executed | 55 | in-image golden patch | coverage.py (in-image) |
| Agoric/agoric-sdk#8072 | 17 repository tests | 17 | source-only + coupled test overlay | go cover |
| opentelemetry-go-contrib#6172 | engineered session Oracle | 1 | source-only | go cover |
| ozontech/seq-ui#44 | engineered session Oracle | 1 | source-only | go cover |
| postman17/metrics#13 | engineered session Oracle | 1 | source-only | go cover |
| redis/redis-py#3350 | engineered session Oracle | 3 | source-only | coverage.py |
| codecov/shared#90 | engineered session Oracle | 7 | source-only | coverage.py |
| labscript-suite/lyse#119 | engineered session Oracle | 3 | source-only | coverage.py |
| open-formulieren/open-forms#3125 | engineered session Oracle | 3 | source-only | `sys.settrace` |
| OpenFreeEnergy/alchemiscale#421 | engineered session Oracle | 2 | source-only | coverage.py |
| Pyomo/pyomo#3698 | engineered session Oracle | 1 | source-only | coverage.py |
| runfinch/finch-core#780 | engineered shell harness | 7 | source-only | bash xtrace |

The in-image harness (scikit-learn) is judged strictly: the harness process
must exit 0, produce exactly the configured number of start and golden phases
(three each), report every phase status as success, record a successful
`golden_apply`, emit the matching per-phase JUnit report for each, and cover a
changed-old production line. A truncated one-run harness or a non-zero exit can
never be reported as a passing Oracle.

Four tasks are **blocked and non-scoring**:

* **scala-js#4707** and **scala-js#4988** -- both Oracles are engineered and
  behaviourally strict (the project's own sbt build, executed offline against a
  pinned, fully hashed ivy/coursier closure, 3/3 on start and 3/3 on golden with
  a named PASS_TO_PASS set), but changed-old production coverage **cannot be
  measured during the validating run**: the pinned JaCoCo 0.8.12 agent attached
  with `-J-javaagent` does produce an exec and an XML report over 1,469 analysed
  classes, yet the reviewed production sources report zero covered lines (the
  analysed class files are not the class identities the tests executed), and the
  alternative `set every Test/javaOptions += -javaagent:...` injection fails the
  scala-js build. The recipes and their validation records are retained in the
  contract under `engineered_oracle_not_scoring` /
  `engineered_oracle_validation`; re-running `validate-oracle` after attaching
  JaCoCo so the executed and analysed class identities match is all that is
  needed to promote them.
* **hashicorp/nomad#16383** -- behaviourally strict Ember/QUnit Oracle, but no
  instrumentation path exists for changed-old line coverage at that revision.
* **chazari-x/yandex-pr-diplom#1** -- the accepted change is public-API-breaking
  in both directions (see below).

##### Offline execution and cache provenance

Dependency resolution can never touch the network. Every Go recipe runs with
`GOPROXY=off`, `GOSUMDB=off` and `GOFLAGS=-mod=readonly` (readonly so a run can
neither hide a missing dependency nor rewrite the workspace's `go.mod`, which
would corrupt the golden patch), and the module cache is **not** a named Docker
volume: each phase is seeded into a fresh, disposable directory extracted from
an immutable, fully hashed archive under the asset root, verified against the
contract-pinned digest first. The Go build cache is deliberately *not* pinned:
it is content-addressed and self-verifying, so it is a derived accelerator
rather than an execution input, and a metadata-only "pin" would be meaningless.

Both the eks-anywhere and TiDB Oracles now run with `--network none`. Four of
eks-anywhere's 23 PASS_TO_PASS tests read a release manifest over HTTPS; that
response was captured once, is hash-pinned in the contract, and is served by a
loopback TLS server started inside the container from a pinned certificate and
a 20-line stdlib server, with `SSL_CERT_FILE` and `--add-host` pointing the
test at it. Nothing is fetched live.

`restore-caches` restores **both** the start and the golden cache of every task
that declares them, and `--target-dir` restores into plain directories, which
is what a fresh host without the historical Docker volumes needs. An end-to-end
test restores an archive into an empty target and then runs a real `go run` in
the pinned image with `--network none` and `GOPROXY=off`, proving the restored
cache alone is sufficient.

##### Asset integrity is a single source of truth

`asset_inventory()` enumerates *every* execution-relevant asset of a recovered
entry -- each pinned Oracle source (`session_test`, `session_tests`, `harness`,
golden-side variants), the recorded golden patch, every declared `host_mounts`
tree, every packaged cache archive, every Docker image and cache volume, the
attested-coverage evidence file, and the retained repository checkout. Nothing
may be read that does not appear there. From that one list:

* **Preflight** requires a full, symlink-aware content digest for every file
  and tree (a symlink is hashed as its target string and never followed) and an
  exact match against the contract; an asset with *no* recorded digest is a
  blocker in its own right, so nothing can execute unpinned. `--rehash`
  bypasses the per-file digest cache and re-reads every byte.
* **The replay-contract fingerprint** binds the observed digest of all of them,
  so replacing any asset in place invalidates previously-recorded rows.
* **The staged candidate patch** is re-resolved and re-hashed from disk
  immediately before every replay and compared against the `patch_sha256`
  recorded at staging time; its live digest is folded into the row provenance
  and the staleness check, so a rewritten patch reruns the pair. A missing or
  drifted patch is a non-scoring `infra_retry`, never a model `apply_error`.
* **Replay** re-verifies the inventory immediately before executing, copies
  every writable mount into a per-run scratch tree so pinned sources are never
  mutated in place, and re-verifies the immutable sources afterwards. A missing,
  tampered or drifted asset produces a non-scoring `infra_retry` row -- never a
  model pass or fail.

`pin-assets` records the current digest of every declared asset into the
contract as a deliberate, auditable action.

`restore-caches` rebuilds a missing Go module cache volume only from an archive
that is (a) listed in `SHA256SUMS`, (b) byte-identical to that entry, and (c)
byte-identical to the contract-pinned digest. Extraction uses Python's
`tarfile` with path-traversal and link safety; no container image (pinned or
otherwise) and no network is involved, and an existing volume is never
overwritten.

Commands for the validation gate:

```bash
python3 scripts/run_legacy_gopy_scripted_correctness.py validate-oracle \
  --tasks data/releases/hard/20260808/tasks.jsonl \
  --oracles data/releases/hard/20260808/oracles.jsonl \
  --legacy-root <retained-session-root> \
  --asset-root  <legacy-recovery-assets>/gopy-scripted \
  --output <evidence-dir> --runs 3            # add --task-id to scope it
```

It writes `<evidence-dir>/oracle-validation/<task>/validation.json` (per-run
results, the changed-old anchor set, the measured coverage intersection) plus a
`validation-summary.json`, and exits non-zero unless every scoped Oracle meets
the standard. There is no mode that reports success without executing: an
Oracle whose golden side cannot be run is a failure, not an attestation.

Two further subcommands support the asset contract:

```bash
# record the current digest of every declared asset (deliberate re-pinning)
python3 scripts/run_legacy_gopy_scripted_correctness.py pin-assets \
  --tasks ... --oracles ... --legacy-root ... --asset-root ...

# rebuild a lost module-cache volume from its SHA256SUMS-listed archive
python3 scripts/run_legacy_gopy_scripted_correctness.py restore-caches \
  --asset-root <legacy-recovery-assets>/gopy-scripted [--dry-run]
```

`tests/test_run_legacy_gopy_scripted_correctness.py` covers this adapter with
synthetic fixtures only (no real builds or containers): contract schema and
scope-drift validation against the published release, PASS_TO_PASS judging
over `go test -json` streams (including build failures and vanished tests),
JUnit/pytest node-ID matching, per-recipe blocker detection (missing image,
retagged image, missing cache volume, start SHA absent from the checkout,
tampered session Oracle, drifted in-image workspace), reappeared-asset
detection for blocked entries, provenance/staleness and row retraction,
lock-safe raw-row merging, CLI fail-closed behavior, and the engineered-Oracle
machinery: session-asset installation, workspace/workdir resolution, declared
host mounts, coverage parsing for `go_cover`/`py_cover`/`py_settrace`/
`bash_xtrace`, digest-verified `attested` coverage, changed-old anchor
computation from a real git history, and the recorded coupled-test overlay.

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
Qwen3.6-27B now runs independently at:

```text
/data/v-peiyangliu/reviewbench-openmodels/
  correctness-20260812-qwen27-v1/
```

Its Go/Python, Scripted, TS/JS, C#, JVM, and Rust lanes are complete. Native is
queued behind the two existing heavy Native runs. Kimi will be added after
valid generation completes.

The local evaluation host rebooted at `2026-08-12T17:14Z`. Atomic JSONL
checkpoints remained intact at 233/325 open-model Native pairs and 207/325
hosted-model Native pairs. Completed Qwen lane caches were removed, releasing
about 61 GiB without deleting evidence, and both Native runners resumed in
skip-terminal/retry-infra/fill-missing mode. The Qwen Native supervisor was
recreated to start after either existing Native run terminates.

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

Qwen3.6-27B rubric-v2 judgment completed independently with 414 records, 363
judged candidates, 51 generation failures, no judge errors, and 174
maintainability passes:

```text
/data/v-peiyangliu/reviewbench-openmodels/
  judging-20260812/qwen27/

/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-evaluation-20260812/judging/
  qwen27-rubric-v2.tar.gz
SHA-256 c9979833098d6d73748571fbc065e63d5064726a6955b98bd068d1547db199ed
```

Qwen3.6-27B has also completed all seven modern correctness lanes. Four
currently runnable published-legacy tasks were replayed independently; 36
legacy tasks remain blocked by missing historical assets. Its current
378-task partial four-gate result is:

| Gate | Pass |
|---|---:|
| Generation/apply | 332/378 |
| Scope | 327/378 |
| Correctness | 179/378 |
| Maintainability | 160/378 |
| **Final Resolved** | **93/378** |

This is a partial score, not a replacement for the frozen `/414` leaderboard.
Nineteen correctness pairs remain explicitly unresolved infrastructure, so
93 is also a fail-closed lower bound over the current 378-task evidence set.
The portable release is stored at:

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-evaluation-20260813/qwen27/
  qwen27-partial378-v1.tar.gz
SHA-256 e5b58bd948ced81803f8e40667f30446fc7ce0e6891896c54d7bcb911e727eb1
```

The original five open models have now completed all seven modern correctness
lanes as well. Their independent 381-task partial releases use the 374 modern
tasks plus the seven previously frozen runnable legacy tasks:

| Model | Generation | Scope | Correctness | Maintainability | Final Resolved |
|---|---:|---:|---:|---:|---:|
| Qwen3.6-35B-A3B | 319 | 317 | 156 | 93 | **56/381** |
| MiniMax-M2.1 | 346 | 337 | 84 | 68 | **37/381** |
| GPT-OSS-120B | 380 | 374 | 182 | 47 | **34/381** |
| Devstral-2-123B | 355 | 346 | 135 | 45 | **25/381** |
| Qwen3-Coder-Next-FP8 | 300 | 294 | 69 | 30 | **15/381** |

These remain fail-closed partial scores: unresolved infrastructure is not
converted into model failure provenance, and 33 published-legacy tasks remain
outside the denominator. Portable releases and checksums are stored under:

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-evaluation-20260813/open-five/

Qwen-Qwen3.6-35B-A3B          f6c2fd93f9e6669bc6f219cab1b70b866376e89ea97c2417cb77c74bcdecf3e4
Qwen-Qwen3-Coder-Next-FP8     53bdfbb7b1a7fc3fc6bfca4cc8324e2d5d5fe581adf81fc459ea31954e9f570a
mistralai-Devstral-2-123B     c83441f04672656a1ccd170ae6812a77125f7bba45d15fb021a24902d77386da
openai-gpt-oss-120b           9d8982b2b26641a347b07545e3b3facdb1c1e2258f7d1230e4c946ea02e6fdca
MiniMaxAI-MiniMax-M2.1        fa1497a8629403bc40b0023ac0c8a516403312e377dbbca4356187e131aa9686
```

Hosted-five and Gemini have now completed the same independent 381-task
partial flow:

| Model | Generation | Scope | Correctness | Maintainability | Final Resolved |
|---|---:|---:|---:|---:|---:|
| GPT-5.6 Sol | 379 | 370 | 299 | 227 | **170/381** |
| Claude Opus 5 | 378 | 369 | 310 | 206 | **161/381** |
| GPT-5.6 Terra | 381 | 374 | 284 | 206 | **151/381** |
| Gemini 3.6 Flash | 342 | 323 | 249 | 203 | **146/381** |
| GPT-5.6 Luna | 381 | 379 | 228 | 151 | **108/381** |
| Claude Sonnet 5 | 381 | 374 | 287 | 154 | **104/381** |

The combined 12-model table and every portable archive checksum are recorded
in [`LATEST_MODEL_PARTIAL_LEADERBOARD_20260813.md`](LATEST_MODEL_PARTIAL_LEADERBOARD_20260813.md).

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
