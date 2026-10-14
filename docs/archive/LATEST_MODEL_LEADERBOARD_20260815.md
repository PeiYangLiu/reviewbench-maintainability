> **Historical archive — not current package instructions or a current leaderboard.**
> Preserve the protocol, roster, denominator, incomplete/withdrawn flags and audit
> caveats stated below; do not reinterpret these results using today's judge policy.
> See the [archive index](README.md) and [current documentation](../README.md).
> Historical commands and local artifact references may require assets not shipped
> in this repository. Publication changes are limited to this notice, portable
> path placeholders and repaired links; original bytes remain in the backup.

# 414-hard 最新模型 Audited Leaderboard

更新时间：2026-08-20

> 本页数值为**三判合议终版**（2026-08-20 重判完成）。此前的单裁判
> `claude-sonnet-4.6` 数值已全部作废。

> 本榜替代已撤回的 408-task snapshot。计分覆盖 **398 道可比较且可判题**，
> correctness 必须绑定 canonical patch，且 maintainability 由 **Opus 5 /
> GPT-5.6 Sol / Gemini 3.6 Flash 三判合议、全票通过**才算通过。
> GPT/Opus 跨代模型只使用 fresh 结果，不混入旧冻结分数。

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

- 固定分母：398（402 审计题减去 4 道无法在同一协议下判定的题）；
- generation、correctness、judgment 各 5,628 条（402 题 × 14 模型），
  task/model pair 集合完全一致；计分时统一收缩到 398 题；
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
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | GPT-5.6 Sol | 396 | 388 | 348 | 146 | **126/398** | 31.7% |
| 2 | Claude Opus 5 | 397 | 388 | 349 | 138 | **118/398** | 29.6% |
| 3 | Gemini 3.6 Flash | 360 | 342 | 289 | 137 | **115/398** | 28.9% |
| 4 | GPT-5.5 | 395 | 387 | 341 | 131 | **111/398** | 27.9% |
| 5 | GPT-5.6 Terra | 398 | 392 | 322 | 125 | **101/398** | 25.4% |
| 6 | Claude Opus 4.8 | 392 | 389 | 339 | 101 | **88/398** | 22.1% |
| 7 | GPT-5.6 Luna | 398 | 397 | 265 | 77 | **70/398** | 17.6% |
| 8 | Claude Sonnet 5 | 398 | 392 | 324 | 71 | **58/398** | 14.6% |
| 9 | Qwen3.6-27B | 349 | 344 | 207 | 66 | **54/398** | 13.6% |
| 10 | Qwen3.6-35B-A3B | 331 | 329 | 184 | 30 | **26/398** | 6.5% |
| 11 | MiniMax-M2.1 | 362 | 354 | 104 | 20 | **19/398** | 4.8% |
| 12 | Devstral-2-123B | 374 | 365 | 153 | 15 | **15/398** | 3.8% |
| 13 | GPT-OSS-120B | 397 | 391 | 208 | 12 | **12/398** | 3.0% |
| 14 | Qwen3-Coder-Next-FP8 | 312 | 307 | 84 | 11 | **11/398** | 2.8% |

Kimi K2.5 的生成已覆盖全部 402 题，但尚未走完 correctness 与判分，
因此暂不列入；Gemini 3.5 Flash 没有同协议的 fresh 结果，同样不列入。
二者都不伪装成可比行。

## 计分范围为什么是 398 而不是 402

审计口径的 402 题里，有 4 道题在三判协议下**得不到判定**，对全部 14 个模型
一并排除，使「所有模型在同一协议下被判」这一不变量成立：

| 题 | 未决 pair | 原因 |
| --- | ---: | --- |
| metasploit-framework ×3 | 29 | 三个裁判都触发安全拒答，重试无效 |
| microsoft/FluidFramework ×1 | 1 | Opus 5 稳定超出 16384 升档上限 |

两类都属于「协议给不出判决」，不是「判决不合心意」：

- 安全拒答与预算耗尽被分开处理，重试对拒答无效（升档只对截断有效）；
- 提高升档上限会改变 `judge_protocol_fingerprint`，为 1 个 pair 触发全部
  5628 对重判，代价与收益不成比例；
- 排除对所有模型一致，与既有 12 道题的排除方法论相同。

排除前后**排名完全不变**，仅个别绝对值微调。

## 一次被拦下的判分污染

重判过程中判分网关中途停止服务，55 个 pair 收到 `Connection refused`。这些
pair 全部集中在 4 个开源模型上——若被当作「未通过」计分，会系统性压低它们的
分数。fail-closed 将其记为 `judge_error` 而非静默失败，网关恢复后重跑全部
恢复为正常判定。

需要注意：判分脚本的默认端点是 `127.0.0.1:4399`，而当前可用网关在
`127.0.0.1:8787`，必须显式传 `--chat-endpoint`。

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

## 代际顺序已恢复

撤榜的触发点是两处倒挂：GPT-5.5 高于 GPT-5.6、Opus 4.8 高于 Opus 5。
在三判合议 + 398 题口径下，两处都不再存在：

| 对比 | 旧单裁判榜 | 三判合议终版 |
| --- | --- | --- |
| GPT-5.6 Sol vs GPT-5.5 | 倒挂 | **126 > 111** ✅ |
| Claude Opus 5 vs Opus 4.8 | 倒挂 | **118 > 88** ✅ |

这四个模型跑的是同一 scope、同一 correctness runner、同一 retry/infra 策略，
不使用任何旧冻结分数，因此倒挂的消失来自判分口径修正，而非样本差异。

单裁判不稳健有直接证据：三个裁判的判定一致率仅 **60.7%**（3206/5281），
且宽严差异可达 3.3 倍——对 Qwen3.6-27B，Gemini 判过 256 题，Opus 5 只判过
77 题。任何单裁判结论都不足以支撑跨代比较。

## Artifacts

三判合议判分（终版）：

```text
${REVIEWBENCH_WORKSPACE}/reviewbench-openmodels/judging-20260820-panel3/
  complete14-402/judgments.jsonl
```

fail-closed correctness overlay（含 context-fit 重跑）：

```text
${REVIEWBENCH_WORKSPACE}/reviewbench-openmodels/correctness-20260820-merged/
```

context-fit 重跑后的 canonical generations：

```text
${REVIEWBENCH_WORKSPACE}/reviewbench-openmodels/generation-20260820-contextfit/
```

基础 14-model release（scope、oracles、原始 generations）：

```text
${REVIEWBENCH_WORKSPACE}/reviewbench-openmodels/evaluation-20260815/
  complete14-corrected402-v1/
```

其中包含冻结 scope、canonical generations、attested judgments、
fail-closed correctness overlay、逐题四门结果、汇总 report 和 checksums。
