# 414-hard 最新模型 Partial Leaderboard

更新时间：2026-08-15

## 口径

```text
Final Resolved =
generation_apply AND scope_ok AND correctness_ok AND maintainability_pass
```

当前 12 个模型统一使用：

- 374 道 modern task；
- 34 道已恢复、通过 start/golden/coverage 与资产 provenance 验证的
  published-legacy task；
- 总分母 **408**。

剩余 6 道 legacy 保持 fail-closed blocked，不进入分母：
Angular、gRPC、Nomad、Yandex，以及两道缺当前运行 coverage 的 Scala.js。
infra/unresolved 不伪装成模型失败，但不计入当前 Final Resolved numerator。

## 最新榜单

| Rank | Model | Generation | Scope | Correctness | Maintainability | Final Resolved | Rate | Unresolved infra |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | GPT-5.6 Sol | 406 | 395 | 322 | 247 | **188/408** | 46.1% | 25 |
| 2 | Claude Opus 5 | 405 | 394 | 332 | 221 | **173/408** | 42.4% | 25 |
| 3 | GPT-5.6 Terra | 408 | 399 | 307 | 221 | **163/408** | 40.0% | 29 |
| 4 | Gemini 3.6 Flash | 369 | 346 | 274 | 220 | **162/408** | 39.7% | 23 |
| 5 | GPT-5.6 Luna | 408 | 405 | 247 | 163 | **118/408** | 28.9% | 30 |
| 6 | Claude Sonnet 5 | 408 | 400 | 308 | 163 | **112/408** | 27.5% | 28 |
| 7 | Qwen3.6-27B | 358 | 352 | 202 | 171 | **105/408** | 25.7% | 19 |
| 8 | Qwen3.6-35B-A3B | 340 | 338 | 163 | 96 | **58/408** | 14.2% | 22 |
| 9 | MiniMax-M2.1 | 372 | 362 | 95 | 74 | **43/408** | 10.5% | 28 |
| 10 | GPT-OSS-120B | 407 | 401 | 195 | 50 | **36/408** | 8.8% | 29 |
| 11 | Devstral-2-123B | 381 | 372 | 142 | 46 | **26/408** | 6.4% | 25 |
| 12 | Qwen3-Coder-Next-FP8 | 321 | 315 | 75 | 32 | **16/408** | 3.9% | 20 |

Kimi K2.5 使用四个独立 H100 shard 继续生成，不阻塞此榜单。

## 冻结 `/414` 历史基线

| Model | Final Resolved /414 |
|---|---:|
| GPT-5.5 | **212/414** |
| Claude Opus 4.8 | **177/414** |
| Gemini 3.5 Flash | **143/414** |

历史基线与当前 partial 分母不同，不混排。

## Portable releases

BlobFuse 根目录：

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-evaluation-20260815/latest12/
```

| Model | Archive | SHA-256 |
|---|---|---|
| GPT-5.6 Sol | `gpt-5.6-sol-partial408-v1.tar.gz` | `27b9e328b6d6dc87b8466b380798efee95a8939e1438d40e5ca857ff6ef84fd8` |
| Claude Opus 5 | `claude-opus-5-partial408-v1.tar.gz` | `124f67bce6a95cc165848b53960e9759b7bacc6e9f5200196aa96ad413081528` |
| GPT-5.6 Terra | `gpt-5.6-terra-partial408-v1.tar.gz` | `3b468c2195f480f4b13728ea8060becc8a782996068aef74eef01305ac123040` |
| Gemini 3.6 Flash | `gemini-3.6-flash-partial408-v1.tar.gz` | `58e47ee4b43d214fdf90d00ad7f3323017c4e84510fdebcbd6cb54eeaf03f4bd` |
| GPT-5.6 Luna | `gpt-5.6-luna-partial408-v1.tar.gz` | `2f197a44f92b2c01fcd71a4dd6cc4d8bd7188b7c343cb1191dda784a42cb81da` |
| Claude Sonnet 5 | `claude-sonnet-5-partial408-v1.tar.gz` | `43bb779beba2f80afc714b4632084061606c7640f4965428dbf888abd4f23c05` |
| Qwen3.6-27B | `Qwen-Qwen3.6-27B-partial408-v1.tar.gz` | `e8fb2760d8d36f841713af74824a6f26e9399140a1b92bda91c1862c38c58605` |
| Qwen3.6-35B-A3B | `Qwen-Qwen3.6-35B-A3B-partial408-v1.tar.gz` | `2d235598de4cc2c0ca51ac92912d4137428a9469027e3ab5e3f6056dd691f838` |
| MiniMax-M2.1 | `MiniMaxAI-MiniMax-M2.1-partial408-v1.tar.gz` | `36447a22a5070059f994fb9d7d8f44aada96760a59a51024a10bfa60e7f1bf43` |
| GPT-OSS-120B | `openai-gpt-oss-120b-partial408-v1.tar.gz` | `165fd7fbac21bf8ada65fe342b4e084719b2a55ba3a151a38a7ef2fb7bd25f81` |
| Devstral-2-123B | `mistralai-Devstral-2-123B-Instruct-2512-partial408-v1.tar.gz` | `ed7e55d69cde1f9d59fcb240dc6454c02156417ace5e3f5629938c499d8c4738` |
| Qwen3-Coder-Next-FP8 | `Qwen-Qwen3-Coder-Next-FP8-partial408-v1.tar.gz` | `0338c44d40b11560b2fea8f5ebf88ad950757e8971c8c75e9af377818db9a08c` |

每个 release 含 408 tasks、canonical generation、patch/trace、normalized
correctness、judgments/raw judge response、逐题四门结果、report、manifest 与
`SHA256SUMS`。
