# ReviewBench-Maintainability 周报：撤榜、重审与三判合议

报告日期：2026-08-18
覆盖周期：2026-08-15 — 2026-08-18

---

## 一、本周结论

本周的核心工作不是「跑出更多分数」，而是**发现旧榜不可信、撤回、系统性重建**。

1. **撤回了 408-task leaderboard**。触发点是一个反常识现象：GPT-5.5 高于
   GPT-5.6、Opus 4.8 高于 Opus 5。审计确认这不是真实结论，而是评测口径缺陷。
2. **重建为 402-task 审计榜**，并发布 **14 模型完整去重榜**，全部
   fail-closed 校验通过（canonical patch mismatch = 0，计分范围内 infra = 0）。
3. **maintainability 判分改为三判合议**（Opus 5 / GPT-5.6 Sol / Gemini 3.6
   Flash，全票通过才算通过），当前重判接近完成。
4. **新增 SFT adapter 评测能力**（此前流水线完全不支持 LoRA），两个 adapter
   的 402 题生成已全部完成。

本周共 14 个提交，全量测试 **640 passed**。

---

## 二、为什么撤榜：五类可证伪的缺陷

审计不是"感觉不对"，每一条都有可执行证据，且都已 fail-closed 修复。

| # | 缺陷 | 后果 | 修复提交 |
|---|---|---|---|
| 1 | TSJS 候选构建失败被一律记为 infra | 模型的真实失败被当作基础设施问题豁免 | `323c699` |
| 2 | JVM mandatory prime 失败未做 exact-start baseline 区分 | 同上 | `9f009a7` |
| 3 | correctness overlay 在高优先级重跑覆盖前过早拒绝中间态 infra | 已修好的题仍被判为未决 | `41b2064` |
| 4 | C# 终态 pass 被早期重试残留的 `infra_error` 覆盖 | **真实通过被改判为未决** | `281b86e` |
| 5 | Rust Alacritty 并行replay 共用 Xvfb `:99` | 端口冲突导致 baseline 假失败 | `0ad4564` |

此外还有两个口径问题：

- **旧裁判是参赛选手**：408 榜由 Claude Opus 4.8 判分，而它本身在榜上被评测。
- **跨代口径不一致**：Opus 4.8 曾做过"仅失败样本"的定向重跑，与 Opus 5 不同口径。

**验证方式**：修复后逐题重放，用 exact-start baseline 判定归属。例如
FluidFramework 一题，候选在 Biome 检查失败，而同一 exact-start baseline
193/193 构建成功 → 判定为模型失败而非基础设施问题。

---

## 三、402-task 审计口径

```text
Final Resolved =
generation_apply AND scope_ok AND correctness_ok AND maintainability_pass
```

从 414 题中排除 12 题，**对所有模型一致**：

| 类别 | 题数 | 说明 |
|---|---:|---|
| 不可恢复 legacy harness | 6 | Angular、gRPC、Nomad、Yandex、Scala.js ×2 |
| 全模型 metadata defect | 2 | RapidPro、Spack |
| 不可执行 Oracle contract | 4 | Greptime、Pasfmt、PiConGPU、dotnet/diagnostics |

fail-closed 不变量：

- generated candidate 的 correctness patch SHA-256 必须等于 canonical generation；
- 计分范围内不允许 `infra_retry` / `apply_error` / 缺失 correctness；
- judgment 必须绑定同一 canonical generation；
- overlay 的模型全集由 expected-model 文件固定，不能静默漏模型。

---

## 四、14 模型完整榜（单裁判口径，即将被三判替换）

生成 / Scope / **Correctness 三门不受裁判影响**；Maintainability 与 Final
Resolved 正在按三判规则重算。

| # | Model | Gen | Scope | Correct | Maint | Final |
|---:|---|---:|---:|---:|---:|---:|
| 1 | GPT-5.6 Sol | 400 | 389 | 351 | 192 | **159/402 (39.6%)** |
| 2 | Claude Opus 5 | 398 | 387 | 350 | 187 | **157/402 (39.1%)** |
| 3 | GPT-5.6 Terra | 402 | 394 | 325 | 186 | **149/402 (37.1%)** |
| 4 | GPT-5.5 | 399 | 389 | 344 | 175 | **144/402 (35.8%)** |
| 5 | Gemini 3.6 Flash | 364 | 342 | 293 | 175 | **132/402 (32.8%)** |
| 6 | Claude Opus 4.8 | 396 | 391 | 341 | 146 | **110/402 (27.4%)** |
| 7 | GPT-5.6 Luna | 402 | 400 | 267 | 137 | **96/402 (23.9%)** |
| 8 | Qwen3.6-27B | 353 | 347 | 210 | 146 | **88/402 (21.9%)** |
| 9 | Claude Sonnet 5 | 402 | 395 | 327 | 118 | **78/402 (19.4%)** |
| 10 | Qwen3.6-35B-A3B | 335 | 333 | 186 | 79 | **45/402 (11.2%)** |
| 11 | MiniMax-M2.1 | 366 | 357 | 107 | 70 | **38/402 (9.5%)** |
| 12 | GPT-OSS-120B | 401 | 395 | 210 | 39 | **26/402 (6.5%)** |
| 13 | Devstral-2-123B | 377 | 368 | 155 | 47 | **20/402 (5.0%)** |
| 14 | Qwen3-Coder-Next-FP8 | 316 | 310 | 85 | 26 | **12/402 (3.0%)** |

**倒挂已消失**：统一口径下 GPT-5.6 Sol > GPT-5.5、Opus 5 > Opus 4.8，与
预期一致。GPT/Opus 四个跨代模型均为**同批重跑**，未引用任何旧冻结分数。

去重规则：Sol 与 Opus 5 采用与 GPT-5.5 / Opus 4.8 同批执行的 fresh 结果，
其余 10 个模型采用 audited cohort，每个模型只出现一次。

---

## 五、三判合议（本周方法学变更）

### 规则

```text
maintainability_pass =
judge(Claude Opus 5) AND judge(GPT-5.6 Sol) AND judge(Gemini 3.6 Flash)
```

- **全票通过**才算通过，任一裁判否决即不通过；
- **未决即不通过**：任一裁判无有效判定 → 该 pair 不计分，必须解决，不得静默；
- 裁判身份强校验，每次调用必须返回精确请求的模型 ID；
- 协议指纹绑定裁判组与合议规则，单裁判历史结果不会被误复用。

### 实证：单裁判确实脆弱

三判**一致率仅 60.7%**（3206/5281）——近四成的题存在裁判分歧。各裁判宽严差异极大：

| Solver | 三判合议 | Opus 5 | Sol | Gemini |
|---|---:|---:|---:|---:|
| GPT-5.6 Sol | 147 | 162 | 228 | 316 |
| Claude Opus 5 | 139 | 149 | 178 | 274 |
| Gemini 3.6 Flash | 138 | 157 | 186 | 279 |
| Qwen3.6-27B | 63 | 77 | 120 | 256 |

Gemini 最宽松（对 Qwen3.6-27B 判过 256 题），Opus 5 最严格（77 题），
**相差 3.3 倍**。这解释了为何单裁判榜不稳健。

自我偏好检查：Opus 5 给自己 149、给 Sol 162；Gemini 给自己 279、给 Sol 316
——两家都不把自己排第一。仅 Sol 给自己最高分，但它本身即榜首。合议规则下
单个裁判无法独自制造通过，只能否决。

### 判分基础设施的两个缺陷（本周修复）

**缺陷一：固定输出预算系统性淘汰难题**（`265559c`）

Opus 5 是推理模型，会先消耗预算再输出可见内容。固定上限下，越难的 patch
越容易被截断成空响应，被记为"无法评分"。

| 预算策略 | judge_error |
|---|---:|
| 固定 2048 | 573 |
| 固定 4096 | 126 |
| **4096 截断自动升档至 16384** | **1** |

证据：4096 下成功样本 max=4093、p99=3830，分布明显被上限截断。改为按
`finish_reason == "length"` 识别截断并翻倍重试后，**316 次调用被救回**
（Opus 5 311 次、Gemini 5 次）。升档上限进入协议指纹，保证全体 pair 同规则。

**缺陷二：安全拒答被误判为截断**（`e93116f`）

`content_filter` 与预算耗尽同样返回空响应，被我的升档逻辑误判，白白翻倍三次。
现已单独分类：不重试、不计为模型失败、并**阻止完成标记**，强制做显式范围决策。

29 例集中在 **3 道 metasploit-framework 题**（渗透测试框架代码触发 Opus 5
安全过滤）。这是**待决问题**，见第八节。

---

## 六、SFT adapter 评测（新增能力）

配合 `reviewbench-sft`（Qwen3.6-27B LoRA 微调）的评测需求。

### 流水线此前完全不支持 LoRA，本周补齐（`c243764`）

采用 fail-closed 设计，**并在首次提交即拦下一个真实错误**：job 脚本硬编码
`--served-model-name "$MODEL_ID"`。若无守卫，作业会加载 adapter 却请求基座
模型，**跑出基座成绩并当作 SFT 结果上报**（`5ed7953` 修复）。

守卫覆盖：adapter 基座不匹配、rank 超出上限、请求身份与 adapter 名不一致；
并将 adapter 权重 SHA-256 固化进 manifest。

### 生成已完成：804 / 804

| adapter | 状态 |
|---|---|
| `stage1-full`（主实验） | 402 / 402 |
| `goldenonly`（消融） | 402 / 402 |

与冻结基线完全同口径：`40G2-A100`、TP=2、revision `6a9e13bd`、
`agentic-line-edit/v1`、402 审计 scope。分片并行（`1470f63`）将生成从
约 33 小时压缩到约 8 小时，且不改变任何单题协议。

### 必须纠正的基线引用

SFT 侧文档引用的基线 `105/408 (25.7%)` 是**已撤回**的旧榜：

| 门 | 旧引用（已撤回） | 当前审计 |
|---|---:|---:|
| correctness | 202 | **210** |
| maintainability | 171 | **146** |
| Final | 105/408 (25.7%) | **88/402 (21.9%)** |

maintainability 相差 25 分源于换裁判。若用新口径 SFT 结果去减旧口径基线，
**会凭空多出约 25 分虚假下跌，真实增益将被完全掩盖**。

一个有利的副作用：SFT 侧担心的"蒸馏裁判口味"混淆被削弱——训练轨迹标签来自
Opus 4.8，而现在是三判合议，不再同源。

---

## 七、其他进展

- **Kimi K2.5**：377 题余量四分片独立跑，反复被 STD 优先级抢占，已多次续跑
  （断点恢复正常工作）。不阻塞主榜。
- **correctness 基础设施**：七 lane 真并行、Native 窄锁、Rust pair 级并发、
  Celeritas 短路径与 object alternates 等（上周期提交，本周持续受益）。

---

## 八、待决问题与风险

### 待决：3 道 metasploit 题的范围决策

29 个 pair 因 Opus 5 安全拒答无法判定。**需要决策**，两个候选方案：

- **方案 A（推荐）**：将这 3 题从 402 scope 中排除，分母降为 399，对所有模型
  一致。与既有方法论一致（已排除的 12 题同理）。
- **方案 B**：为这些题降级为二判，但会破坏协议一致性，不推荐。

注意：过滤是**按 patch 内容触发**，并非该题对所有模型都失败（12/11/6 例
分布于 14 个模型），因此方案 A 会丢弃部分已成功的判定，但换取分母对称。

### 风险

1. **三判裁判本身在榜上**。合议使自我偏好无法单独制造通过（只能否决），
   且数据显示 Opus 5 / Gemini 都不把自己排第一，但此事实应对外声明。
2. **裁判分歧率高（39.3%）**。这本身是有价值的发现：单裁判 benchmark 的
   maintainability 数值不应被视为稳健量。
3. **集群配额紧张**。A100/H100 排队常达数小时至一天，是当前主要工期风险。

### 可以对外陈述

- 402 题审计口径与 12 题排除清单及其理由；
- 14 模型 Generation / Scope / **Correctness** 三门结果（与裁判无关）；
- 五类评测缺陷及其 fail-closed 修复；
- 三判一致率 60.7% 这一方法学发现。

### 暂不能对外陈述

- 三判口径下的最终 Final Resolved 排名（重判收尾中，且 metasploit 范围待决）；
- SFT 是否带来提升（correctness 尚未开跑）。

---

## 九、下一阶段计划

1. 完成三判重判，就 metasploit 3 题做出范围决策并落文档；
2. 重算并发布三判口径 14 模型榜，替换现有单裁判数值；
3. 跑 SFT 两个 adapter 的 402 题 correctness（**单独报此门**——它可执行、与
   裁判无关，是判定"真能力提升 vs 蒸馏裁判偏好"的关键证据）；
4. 用同一三判规则评 SFT，与**同口径**基线 88/402 对比，主实验与 golden-only
   消融并列报告；
5. Kimi K2.5 继续独立推进。

---

## 十、产物

| 产物 | 路径 |
|---|---|
| 14 模型完整榜 | `evaluation-20260815/complete14-corrected402-v1/` |
| 审计 latest cohort | `evaluation-20260815/latest12-corrected402-v1/` |
| Fresh 跨代 cohort | `evaluation-20260815/fresh4-corrected402-v1/` |
| 三判判分（进行中） | `judging-20260818-panel3/complete14-402/` |
| SFT 生成 | AMLT `rbsft-eval-402s`（8 分片） |
| 榜单文档 | `docs/LATEST_MODEL_LEADERBOARD_20260815.md` |
| 已撤回旧榜（仅存档） | `docs/LATEST_MODEL_PARTIAL_LEADERBOARD_20260815.md` |

每个 release 均含冻结 scope、canonical generation、attested judgments、
fail-closed correctness overlay、逐题四门结果、report 与 `SHA256SUMS`。
