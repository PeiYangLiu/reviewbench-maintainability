> **Historical archive — not current package instructions or a current leaderboard.**
> Preserve the protocol, roster, denominator, incomplete/withdrawn flags and audit
> caveats stated below; do not reinterpret these results using today's judge policy.
> See the [archive index](README.md) and [current documentation](../README.md).
> Historical commands and local artifact references may require assets not shipped
> in this repository. Publication changes are limited to this notice, portable
> path placeholders and repaired links; original bytes remain in the backup.

# v2 协议主榜（agentic-line-edit/v2，audited-402）

**生成日期**：2026-08-29
**协议**：`agentic-line-edit/v2`（历史用标准 assistant/user 多轮消息）
**题集**：audited-402
**判分**：三判合议 `claude-opus-5 / gpt-5.6-sol / gemini-3.6-flash`，rubric v2，全票才过

> **与 v1 主榜不可比。** 不仅协议不同，实测上游服务配置也变了（P80）：
> Claude 系在本轮被默认开启扩展思考，行为从「直接改」变为「反复探索」。
> v1↔v2 的差值**不能**解释为协议效应。

## 一、榜单

| # | 模型 | Generated | Scope | Correct | Maint | Final | Rate |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | claude-opus-5 | 345 | 303 | 278 | 150 | **120** | 29.9% |
| 2 | gpt-5.6-sol | 400 | 359 | 317 | 154 | **110** | 27.4% |
| 3 | gpt-5.5 | 399 | 357 | 316 | 148 | **107** | 26.6% |
| 4 | gpt-5.6-terra | 402 | 364 | 279 | 126 | **89** | 22.1% |
| 5 | claude-opus-4.8 | 364 | 330 | 299 | 98 | **76** | 18.9% |
| 6 | gpt-5.6-luna | 402 | 362 | 263 | 102 | **74** | 18.4% |
| 7 | Qwen/Qwen3.6-27B | 374 | 335 | 208 | 76 | **52** | 12.9% |
| 8 | claude-sonnet-5 | 229 | 210 | 194 | 64 | **49** | 12.2% |
| 9 | Qwen/Qwen3.6-35B-A3B | 375 | 333 | 160 | 49 | **33** | 8.2% |
| 10 | gemini-3.6-flash | 187 | 169 | 118 | 53 | **32** | 8.0% |
| 11 | MiniMaxAI/MiniMax-M2.1 | 375 | 333 | 121 | 34 | **21** | 5.2% |
| 12 | Qwen/Qwen3-Coder-Next-FP8 | 348 | 310 | 118 | 19 | **17** | 4.2% |
| 13 | mistralai/Devstral-2-123B-Instruct-2512 | 369 | 323 | 124 | 17 | **11** | 2.7% |
| 14 | openai/gpt-oss-120b | 397 | 359 | 181 | 10 | **8** | 2.0% |

`Final = Generation ∧ Scope ∧ Correctness ∧ Maintainability`，分母恒为 402。

## 二、口径与产物

| 项 | 值 |
|---|---|
| 生成 | 8 个 API 模型走本地 llm-gateway；6 个自建模型走 AMLT `v2-protocol-20260826` |
| correctness | 5,152 对（368 道 modern 任务 × 14），`correctness-20260828-v2/` |
| | pass 2,976 / fail 1,427 / candidate_missing 612 / scope_error 93 / infra_retry 44 |
| 判分 | 5,628 条，14,864 次裁判调用，`judging-20260828-v2/` |
| 未计入 | 34 道 published-legacy 任务缺少 replay 契约，本轮按 `--allow-partial-modern` 排除，对全部 14 个模型一致 |

## 三、⚠️ 已知偏差：输出预算截断（P82）

本轮 `--max-output-tokens=4096`。实测该预算**系统性惩罚「先写散文再吐 JSON」的
模型**，且分布极不均匀：

| 模型 | `finish_reason=length` 占比 | 失败题中含截断轮的比例 |
|---|---:|---:|
| Qwen3.6-35B-A3B | 35.5% | **100%** |
| claude-opus-4.8 | 25.1% | **94.7%** |
| **claude-opus-5（榜首）** | 14.6% | **75.4%** |
| claude-sonnet-5 | 9.9% | 43.9% |
| Qwen3.6-27B | 9.9% | 60.7% |
| GPT-5.x 四个 | 0.3–2.1% | **0%** |

**这张榜低估了 Claude 系与 Qwen 系，且低估幅度与输出风格相关，不是随机噪声。**
排名的相对顺序在 GPT 与 Claude 之间尤其需要保留。修法是整榜提高预算重跑
（见 P82），未做之前本榜应标注为**有已知偏差的口径**。

## 四、读这张榜时必须知道的三件事

1. **Claude Opus 5 登顶靠的是转化率，不是产量**。它只生成 345（Sol 400、Terra 402），
   但 Correct 278 里有 150 过判分。Sol 生成 400、Correct 317，Maint 反而只多 4。

2. **生成率低不等于分数低**。`claude-sonnet-5` 只生成 229，仍排第 8；
   `openai/gpt-oss-120b` 生成 397（第 3 多）却垫底——它的补丁能编译能过测试
   （Correct 181），但几乎全部过不了维护性判分（Maint 10）。
   **Correct 与 Maint 是两种能力，不能互相替代。**

3. **correctness 这道门的强度有限**（P81）：它证明「没改坏」，不证明「改到了点子上」。
   实测候选补丁只改 1–2 行时，correctness pass 后过判分的比例是 38.6%，
   而 ≥6 行是 41.2%——**判分门才是真正的判别器**。
