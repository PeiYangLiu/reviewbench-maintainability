> **Historical archive — not current package instructions or a current leaderboard.**
> Preserve the protocol, roster, denominator, incomplete/withdrawn flags and audit
> caveats stated below; do not reinterpret these results using today's judge policy.
> See the [archive index](README.md) and [current documentation](../README.md).
> Historical commands and local artifact references may require assets not shipped
> in this repository. Publication changes are limited to this notice, portable
> path placeholders and repaired links; original bytes remain in the backup.

# 414-hard 最新模型 Partial Leaderboard

更新时间：2026-08-13

## 口径

Final Resolved 固定为：

```text
generation_apply AND scope_ok AND correctness_ok AND maintainability_pass
```

除 Qwen3.6-27B 外，当前模型使用 374 道 modern task 加 7 道已冻结可重放
published-legacy task，分母为 381。Qwen3.6-27B 在管理员清理历史资产后只能重放
4 道 legacy，分母为 378。

以下均为 **fail-closed partial**：infra/unresolved 不会伪装成模型 fail 的
provenance，但在当前 Final Resolved numerator 中不计为 resolved。缺失的 legacy
task 不进入分母。完整 `/414` 榜单仍需恢复历史资产。

## 最新模型榜单

| Rank | Model | Tasks | Generation | Scope | Correctness | Maintainability | Final Resolved | Unresolved infra |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | GPT-5.6 Sol | 381 | 379 | 370 | 299 | 227 | **170/381** | 25 |
| 2 | Claude Opus 5 | 381 | 378 | 369 | 310 | 206 | **161/381** | 25 |
| 3 | GPT-5.6 Terra | 381 | 381 | 374 | 284 | 206 | **151/381** | 28 |
| 4 | Gemini 3.6 Flash | 381 | 342 | 323 | 249 | 203 | **146/381** | 23 |
| 5 | GPT-5.6 Luna | 381 | 381 | 379 | 228 | 151 | **108/381** | 28 |
| 6 | Claude Sonnet 5 | 381 | 381 | 374 | 287 | 154 | **104/381** | 26 |
| 7 | Qwen3.6-27B | 378 | 332 | 327 | 179 | 160 | **93/378** | 21 |
| 8 | Qwen3.6-35B-A3B | 381 | 319 | 317 | 156 | 93 | **56/381** | 20 |
| 9 | MiniMax-M2.1 | 381 | 346 | 337 | 84 | 68 | **37/381** | 27 |
| 10 | GPT-OSS-120B | 381 | 380 | 374 | 182 | 47 | **34/381** | 27 |
| 11 | Devstral-2-123B | 381 | 355 | 346 | 135 | 45 | **25/381** | 23 |
| 12 | Qwen3-Coder-Next-FP8 | 381 | 300 | 294 | 69 | 30 | **15/381** | 19 |

Kimi K2.5 独立运行中，当前冻结并集为 34 generated / 3 failed / 377
unprocessed。剩余任务已拆成 4 个 restart-resilient H100 shard，不影响以上模型
评分或发布。

## 冻结 `/414` 历史基线

历史三模型基线保持不变，不与 partial 分母混排：

| Model | Final Resolved /414 |
|---|---:|
| GPT-5.5 | **212/414** |
| Claude Opus 4.8 | **177/414** |
| Gemini 3.5 Flash | **143/414** |

## Portable releases

BlobFuse 根目录：

```text
${REVIEWBENCH_WORKSPACE}/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-evaluation-20260813/
```

| Model | Archive | SHA-256 |
|---|---|---|
| GPT-5.6 Sol | `hosted-five/gpt-5.6-sol-partial381-v1.tar.gz` | `9a1a8d29f1dd98c26234adde2ee333b3f3acc0689e1f3b2ba07948a7777c6bf3` |
| Claude Opus 5 | `hosted-five/claude-opus-5-partial381-v1.tar.gz` | `07b24274fe22d0cb44dc4f835207ab736ee3d77910a784bc014dcdad3554310d` |
| GPT-5.6 Terra | `hosted-five/gpt-5.6-terra-partial381-v1.tar.gz` | `497f951c6f057635e76146494d4482386ba78062351f9d874876eefeb558548f` |
| Gemini 3.6 Flash | `gemini36/gemini-3.6-flash-partial381-v1.tar.gz` | `7d2aa59de071f36375b3b82058634f3c9e193d12d82806cfd2a9d7be3df00edb` |
| GPT-5.6 Luna | `hosted-five/gpt-5.6-luna-partial381-v1.tar.gz` | `a39d024955a037a5eaca81e5af28dddebf71ec06710085dafc4a044a6b0b67e1` |
| Claude Sonnet 5 | `hosted-five/claude-sonnet-5-partial381-v1.tar.gz` | `174b6d8e3ad5561e0bb4dac9a1e72ba4c8d7a53bd579147c1eb9d0a0823baa0e` |
| Qwen3.6-27B | `qwen27/qwen27-partial378-v1.tar.gz` | `e5b58bd948ced81803f8e40667f30446fc7ce0e6891896c54d7bcb911e727eb1` |
| Qwen3.6-35B-A3B | `open-five/Qwen-Qwen3.6-35B-A3B-partial381-v1.tar.gz` | `f6c2fd93f9e6669bc6f219cab1b70b866376e89ea97c2417cb77c74bcdecf3e4` |
| MiniMax-M2.1 | `open-five/MiniMaxAI-MiniMax-M2.1-partial381-v1.tar.gz` | `fa1497a8629403bc40b0023ac0c8a516403312e377dbbca4356187e131aa9686` |
| GPT-OSS-120B | `open-five/openai-gpt-oss-120b-partial381-v1.tar.gz` | `9d8982b2b26641a347b07545e3b3facdb1c1e2258f7d1230e4c946ea02e6fdca` |
| Devstral-2-123B | `open-five/mistralai-Devstral-2-123B-Instruct-2512-partial381-v1.tar.gz` | `c83441f04672656a1ccd170ae6812a77125f7bba45d15fb021a24902d77386da` |
| Qwen3-Coder-Next-FP8 | `open-five/Qwen-Qwen3-Coder-Next-FP8-partial381-v1.tar.gz` | `53bdfbb7b1a7fc3fc6bfca4cc8324e2d5d5fe581adf81fc459ea31954e9f570a` |

每个 release 含 tasks、canonical generation、patch/trace、normalized
correctness、judgments/raw judge response、四门逐题结果、report、manifest 和
`SHA256SUMS`。
