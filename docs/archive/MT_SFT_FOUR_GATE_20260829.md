> **Historical archive — not current package instructions or a current leaderboard.**
> Preserve the protocol, roster, denominator, incomplete/withdrawn flags and audit
> caveats stated below; do not reinterpret these results using today's judge policy.
> See the [archive index](README.md) and [current documentation](../README.md).
> Historical commands and local artifact references may require assets not shipped
> in this repository. Publication changes are limited to this notice, portable
> path placeholders and repaired links; original bytes remain in the backup.

# 多轮 SFT 四门对照（agentic-line-edit/v2，audited-402）

**生成日期**：2026-08-29
**口径**：与 v2 主榜完全相同——同协议、同题集、同三判面板、同服务配置。
**对照**：每个 adapter 与**它自己的基座**在**同一时间窗**测得（P80）。

## 一、四门结果

| 模型 | Generated | Correct | Maint | Final |
|---|---:|---:|---:|---:|
| Qwen/Qwen3-4B | 373 | 49 | 2 | **2** |
| 　└ +多轮 SFT | 193 | 40 | 5 | **4** |
| Qwen/Qwen3-8B | 394 | 48 | 4 | **4** |
| 　└ +多轮 SFT | 206 | 37 | 4 | **4** |
| Qwen/Qwen3-14B | 398 | 62 | 1 | **1** |
| 　└ +多轮 SFT | 157 | 36 | 4 | **4** |
| Qwen/Qwen3-32B | 399 | 56 | 4 | **4** |
| 　└ +多轮 SFT | 199 | 49 | 5 | **5** |
| Qwen/Qwen3.6-27B | 374 | 208 | 76 | **52** |
| 　└ +多轮 SFT | 378 | 191 | 70 | **53** |
| Qwen3-8B +SFT(nothink 消融) | 111 | 24 | 1 | **1** |

deadline 探针生成率: {'qwen3-32b-mtsft': 338, 'qwen3-8b-mtsft': 341}

`Final = Generation ∧ Scope ∧ Correctness ∧ Maintainability`，分母 402。

## 二、结论

**1. Qwen3-* 阶梯在维护性上处于噪声地板。** 基座 1–4、SFT 后 4–5，四档全部如此。
在这个区间**测不出数据配方的好坏**，差异无法与噪声区分。

**2. Qwen3.6-27B 上 SFT 是零效果。** 52 → 53，逐门都在噪声内
（Generated 374→378、Correct 208→191、Maint 76→70）。原因在动作分布：
27B 基座原生就是 `edit 18.4 / find 17.7 / read 38.5` 的均衡策略，
**SFT 想教的东西它已经会了**。

**3. thinking 消融方向明确。** `qwen3-8b-nothink` 生成 111、Final 1，
是全部配置里最差的一个。thinking 有用，去掉有害。

## 三、但生成率的塌陷是可修的（见 P83）

Qwen3-* 的 SFT adapter 生成率相对基座腰斩，根因**不是补丁质量**：

- 只看交了卷的题，adapter 的 `Maint|Generated` 是基座的 2–8 倍（2.5% vs 0.3–1.0%）；
- 失败题里 **209/196/244/201 道一次 edit 都没做过**——是不动手，不是改坏。

加一个停止信号（`--edit-deadline 24`，不改 adapter、不重训）：

| | 无 deadline | **加 deadline** | 基座 |
| --- | ---: | ---: | ---: |
| qwen3-8b-mtsft 生成 | 206 | **341** | 394 |
| qwen3-32b-mtsft 生成 | 199 | **338** | 399 |

**一步补回相对基座缺口的七成**，未改 adapter、未重训、未改数据。
机制见 P83：按轮切分丢掉了全局停止策略。

**但产量补回来了，分数没有**（判分实测）：

| adapter | 无 deadline | +deadline | 基座 |
| --- | ---: | ---: | ---: |
| qwen3-32b-mtsft Maint | 5 | **9** | 4 |
| qwen3-8b-mtsft Maint | 4 | **1** | 4 |

按 deadline 是否触发拆开就清楚了：**被逼出来的补丁通过率 0.0–0.7%**，
而自发交卷的是 0.6–4.1%。停止信号把「没交卷」变成了「交白卷」。

所以 `--edit-deadline` 是**诊断工具**，不是解法：它证明瓶颈在停止策略，
但要让分数跟上，得让模型**自己学会**何时收手（例如把轮次预算写进训练样本的
prompt），而不是在推理时喊停。

## 四、⚠️ 这些数字是在 4096 输出预算下测的（P82）

Qwen 系受输出截断影响严重（`Qwen3.6-35B-A3B` 35.5%、`Qwen3.6-27B` 9.9%），
**上表是下界**。已把默认预算提到 8,192 并整榜重跑，完成后会重测 adapter。
