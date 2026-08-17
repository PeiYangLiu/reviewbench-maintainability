# 414-hard 最新模型 Audited Leaderboard

更新时间：2026-08-15

> ⚠️ **本页所有 Maintainability 与 Final Resolved 数值正在按新的三判合议规则
> 重新计算。** 现有表格是旧的**单裁判** `claude-sonnet-4.6` 结果，在重判完成前
> 不得用于比较。Generation / Scope / Correctness 三门不受影响（correctness 为
> 真实执行，与裁判无关）。

> 本榜替代已撤回的 408-task snapshot。计分只覆盖 **402 道可比较题**，
> correctness 必须绑定 canonical patch，且 maintainability 由 **Opus 5 /
> GPT-5.6 Sol / Gemini 3.6 Flash 三判合议、全票通过**才算通过。
> GPT/Opus 跨代模型只使用 fresh 402-task 结果，不混入旧冻结分数。

## 计分口径

```text
Final Resolved =
generation_apply AND scope_ok AND correctness_ok AND maintainability_pass

maintainability_pass =
judge(Claude Opus 5) AND judge(GPT-5.6 Sol) AND judge(Gemini 3.6 Flash)
```

### maintainability 三判合议规则

- **裁判固定为三个、跨三家厂商**：`claude-opus-5`、`gpt-5.6-sol`、
  `gemini-3.6-flash`；
- **全票才算通过**：任一裁判判不通过，该题即不通过。单一裁判无法单独让一
  道题通过，只能否决；
- **未决即不通过**：任一裁判没有返回可解析判定时，该 pair 记为
  `judge_error`（不是 pass、也不算模型失败），必须重跑解决，不得静默计分；
- **每个裁判独立判分**，各自的原始响应与逐项 verdict 全部留存，
  `panel_passes` 记录逐裁判结论，可复核分歧；
- **裁判身份强校验**：每次调用必须返回精确请求的模型 ID，否则拒绝该次判定；
- **协议指纹绑定裁判组**：`judge_protocol_fingerprint` 包含裁判成员列表与
  合议规则，因此单裁判的历史判分不会被误当作三判结果复用；
- 裁判组成员重复会被拒绝，避免同一模型获得多票。

三个裁判本身也在榜上被评测。合议规则使自我偏好无法单独制造通过——一个裁判
抬高自己仍需另外两家同意；但读者应知悉这一事实。**correctness 门是真实执行
Oracle 测试，完全不涉及模型判分**，因此不受裁判选择影响，是判定真实能力的
独立证据。

- 固定分母：402；
- generation、correctness、judgment 各 5,628 条，task/model pair 集合完全一致；
- generated candidate 的 correctness patch SHA-256 必须等于 canonical
  generation；
- scored scope 内不允许 `infra_retry`、`apply_error` 或缺失 correctness；
- judge result 必须绑定同一 canonical generation；
- 12 道排除题不计入任何模型分母：6 道不可恢复 legacy、2 道全模型 metadata
  defect，以及 Greptime、Pasfmt、PiConGPU、dotnet/diagnostics 四道不可执行
  Oracle contract。

## 完整去重 leaderboard

GPT-5.6 Sol、Claude Opus 5 采用与 GPT-5.5、Claude Opus 4.8 同批执行的
fresh 结果；其余 10 个模型采用 audited latest cohort。每个模型只出现一次。

| Rank | Model | Generation | Scope | Correctness | Maintainability | Final Resolved | Rate |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | GPT-5.6 Sol | 400 | 389 | 351 | 192 | **159/402** | 39.6% |
| 2 | Claude Opus 5 | 398 | 387 | 350 | 187 | **157/402** | 39.1% |
| 3 | GPT-5.6 Terra | 402 | 394 | 325 | 186 | **149/402** | 37.1% |
| 4 | GPT-5.5 | 399 | 389 | 344 | 175 | **144/402** | 35.8% |
| 5 | Gemini 3.6 Flash | 364 | 342 | 293 | 175 | **132/402** | 32.8% |
| 6 | Claude Opus 4.8 | 396 | 391 | 341 | 146 | **110/402** | 27.4% |
| 7 | GPT-5.6 Luna | 402 | 400 | 267 | 137 | **96/402** | 23.9% |
| 8 | Qwen3.6-27B | 353 | 347 | 210 | 146 | **88/402** | 21.9% |
| 9 | Claude Sonnet 5 | 402 | 395 | 327 | 118 | **78/402** | 19.4% |
| 10 | Qwen3.6-35B-A3B | 335 | 333 | 186 | 79 | **45/402** | 11.2% |
| 11 | MiniMax-M2.1 | 366 | 357 | 107 | 70 | **38/402** | 9.5% |
| 12 | GPT-OSS-120B | 401 | 395 | 210 | 39 | **26/402** | 6.5% |
| 13 | Devstral-2-123B | 377 | 368 | 155 | 47 | **20/402** | 5.0% |
| 14 | Qwen3-Coder-Next-FP8 | 316 | 310 | 85 | 26 | **12/402** | 3.0% |

Kimi K2.5 尚未完成全流程；Gemini 3.5 Flash 没有 fresh 402-task
同协议结果，因此二者不伪装成可比行。

## 为什么旧榜无效

旧榜混合了不一致的 generation/correctness patch、定向重跑、不同的 infra
与 retry 策略，并使用参赛模型 Claude Opus 4.8 作为 judge。审计还发现：

- TSJS candidate build failure 被错误记为 infra；
- JVM mandatory prime failure 没有用 exact-start baseline 区分模型失败；
- correctness overlay 在高优先级 definitive rerun 覆盖前过早拒绝中间
  `infra_retry`；
- C# 最终 pass 会被早期 retry 留下的 `infra_error=true` 覆盖。
- Rust Alacritty 并行 replay 共用 Xvfb `:99`，导致 exact-start baseline
  发生端口冲突。

这些问题均已 fail-closed 修复；旧 408-task archives 继续保留，仅供审计，
不得用于模型比较。

## Fresh 跨代 leaderboard

GPT-5.5、GPT-5.6 Sol、Claude Opus 4.8、Claude Opus 5 重新执行了同一
402-task scope、同一 correctness runner、同一 retry/infra 策略和同一
Sonnet 4.6 judge。结果不再使用任何旧冻结分数：

| Rank | Model | Generation | Scope | Correctness | Maintainability | Final Resolved | Rate |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | GPT-5.6 Sol | 400 | 389 | 351 | 192 | **159/402** | 39.6% |
| 2 | Claude Opus 5 | 398 | 387 | 350 | 187 | **157/402** | 39.1% |
| 3 | GPT-5.5 | 399 | 389 | 344 | 175 | **144/402** | 35.8% |
| 4 | Claude Opus 4.8 | 396 | 391 | 341 | 146 | **110/402** | 27.4% |

因此统一口径下不存在旧榜倒挂：GPT-5.6 Sol 高于 GPT-5.5，Claude
Opus 5 高于 Opus 4.8。

## Artifacts

完整 14-model deduplicated release：

```text
/data/v-peiyangliu/reviewbench-openmodels/evaluation-20260815/
  complete14-corrected402-v1/
```

根 `SHA256SUMS` 的 SHA-256 为
`abe93c3357934bde5ab10205e9fa6bc87cbcd07497eb1eb8d63b0fee1c534fa6`。

Audited combined release：

```text
/data/v-peiyangliu/reviewbench-openmodels/evaluation-20260815/
  latest12-corrected402-v1/
```

其中包含冻结 scope、402-task canonical generations、attested judgments、
fail-closed correctness overlay、逐题四门结果、汇总 report 和 checksums。
根 `SHA256SUMS` 的 SHA-256 为
`dd719331f3642b1c6f58b582a5f8a782295442c231eab70116a62f9fe5963d3f`。

Fresh cross-generation release：

```text
/data/v-peiyangliu/reviewbench-openmodels/evaluation-20260815/
  fresh4-corrected402-v1/
```

根 `SHA256SUMS` 的 SHA-256 为
`9eedf590be9de4c7b0a8fadb48b40b2519a3205ecc1c4b696a1ced07f3597c14`。
