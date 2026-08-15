# 414-hard 最新模型 Audited Leaderboard

更新时间：2026-08-15

> 本榜替代已撤回的 408-task snapshot。计分只覆盖 **402 道可比较题**，
> correctness 必须绑定 canonical patch，且 maintainability 统一由非参赛模型
> `claude-sonnet-4.6` 重判。旧的 GPT-5.5、Claude Opus 4.8 分数不与本榜混排。

## 计分口径

```text
Final Resolved =
generation_apply AND scope_ok AND correctness_ok AND maintainability_pass
```

- 固定分母：402；
- generation、correctness、judgment 各 4,824 条，task/model pair 集合完全一致；
- generated candidate 的 correctness patch SHA-256 必须等于 canonical
  generation；
- scored scope 内不允许 `infra_retry`、`apply_error` 或缺失 correctness；
- judge result 必须绑定同一 canonical generation；
- 12 道排除题不计入任何模型分母：6 道不可恢复 legacy、2 道全模型 metadata
  defect，以及 Greptime、Pasfmt、PiConGPU、dotnet/diagnostics 四道不可执行
  Oracle contract。

## Audited leaderboard

| Rank | Model | Generation | Scope | Correctness | Maintainability | Final Resolved | Rate |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | GPT-5.6 Sol | 400 | 390 | 343 | 201 | **164/402** | 40.8% |
| 2 | Claude Opus 5 | 399 | 389 | 354 | 187 | **154/402** | 38.3% |
| 3 | GPT-5.6 Terra | 402 | 394 | 325 | 186 | **149/402** | 37.1% |
| 4 | Gemini 3.6 Flash | 364 | 342 | 293 | 175 | **132/402** | 32.8% |
| 5 | GPT-5.6 Luna | 402 | 400 | 267 | 137 | **96/402** | 23.9% |
| 6 | Qwen3.6-27B | 353 | 347 | 210 | 146 | **88/402** | 21.9% |
| 7 | Claude Sonnet 5 | 402 | 395 | 327 | 118 | **78/402** | 19.4% |
| 8 | Qwen3.6-35B-A3B | 335 | 333 | 186 | 79 | **45/402** | 11.2% |
| 9 | MiniMax-M2.1 | 366 | 357 | 107 | 70 | **38/402** | 9.5% |
| 10 | GPT-OSS-120B | 401 | 395 | 210 | 39 | **26/402** | 6.5% |
| 11 | Devstral-2-123B | 377 | 368 | 155 | 47 | **20/402** | 5.0% |
| 12 | Qwen3-Coder-Next-FP8 | 316 | 310 | 85 | 26 | **12/402** | 3.0% |

## 为什么旧榜无效

旧榜混合了不一致的 generation/correctness patch、定向重跑、不同的 infra
与 retry 策略，并使用参赛模型 Claude Opus 4.8 作为 judge。审计还发现：

- TSJS candidate build failure 被错误记为 infra；
- JVM mandatory prime failure 没有用 exact-start baseline 区分模型失败；
- correctness overlay 在高优先级 definitive rerun 覆盖前过早拒绝中间
  `infra_retry`；
- C# 最终 pass 会被早期 retry 留下的 `infra_error=true` 覆盖。

这些问题均已 fail-closed 修复；旧 408-task archives 继续保留，仅供审计，
不得用于模型比较。

## 跨代比较

GPT-5.5 vs GPT-5.6 Sol、Claude Opus 4.8 vs Opus 5 必须使用正在执行的
fresh 402-task 同协议 correctness，不能引用旧冻结分数。统一 Sonnet 4.6
重判的 maintainability 单项已恢复合理方向：

| Model | Maintainability pass /402 |
|---|---:|
| GPT-5.6 Sol | **192** |
| GPT-5.5 | **175** |
| Claude Opus 5 | **187** |
| Claude Opus 4.8 | **146** |

该表不是 Final Resolved leaderboard；完整跨代榜必须等 fresh correctness
全部达到 definitive 后发布。

## Artifacts

Audited combined release：

```text
/data/v-peiyangliu/reviewbench-openmodels/evaluation-20260815/
  latest12-corrected402-v1/
```

其中包含冻结 scope、402-task canonical generations、attested judgments、
fail-closed correctness overlay、逐题四门结果、汇总 report 和 checksums。
根 `SHA256SUMS` 的 SHA-256 为
`dd719331f3642b1c6f58b582a5f8a782295442c231eab70116a62f9fe5963d3f`。
