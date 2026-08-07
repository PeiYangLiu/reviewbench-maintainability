# ReviewBench 306-Task Agentic Three-Model Evaluation

评测日期：2026-07-25

本次评测对累计 306 道严格题目执行全新的 `agentic-line-edit/v1` 协议：

- v1 正式集：100 道
- 20260721 strict reserve：206 道
- hard：26 道
- medium：280 道
- 三个候选模型全部重新生成、重新执行 correctness、重新盲裁
- 没有复用 2026-07-24 SEARCH/REPLACE baseline 的候选结果

## 最终总榜

| 模型 | 成功生成补丁 | Scope | Correctness | 维护性通过 | Resolved@1 | Hard |
|---|---:|---:|---:|---:|---:|---:|
| **GPT-5.5** | **306/306** | **303/306** | **249/306** | **233/306** | **198/306** | 10/26 |
| Claude Opus 4.8 | 305/306 | **303/306** | 240/306 | 221/306 | **187/306** | **11/26** |
| Gemini 3.5 Flash | 255/306 | 254/306 | 196/306 | 172/306 | **157/306** | 7/26 |

总体排名：

1. GPT-5.5：198
2. Claude Opus 4.8：187
3. Gemini 3.5 Flash：157

## 与 non-agentic baseline 的区别

2026-07-24 baseline 使用 SEARCH/REPLACE 文本协议。模型先复制待替换源码，
host 定位 SEARCH 文本后构造 after 文件，最后才由 Git 生成 patch。其结果为：

| 模型 | SEARCH/REPLACE Resolved@1 | Agentic Resolved@1 | 差值 |
|---|---:|---:|---:|
| GPT-5.5 | 184 | **198** | **+14** |
| Claude Opus 4.8 | 180 | **187** | **+7** |
| Gemini 3.5 Flash | 182 | **157** | **-25** |

两次评测不能只把 7 个旧 generation failure 补齐后混合比较。协议改变了模型可观察
信息、行动次数和失败模式，因此本次对 918 个 task-model pair 全部重新执行。

## Agentic 协议

模型每轮返回一个 JSON action：

- `read`：读取 `start_sha` 下的仓库文件
- `find`：在文件中做 literal search
- `edit`：按 1-based 行范围编辑唯一允许的锚点 production file
- `finish`：提前结束

host 拒绝路径穿越、跨文件编辑、重叠 edit、同位置多次插入和无改动 finish。轮次
预算为 32。若预算耗尽时 worktree 已有有效编辑，host 直接固化最后状态。最终 patch
始终由 Git `diff --binary` 生成，模型不输出 unified diff。

每个成功生成的 patch 都进入相同的单文件 scope 门，最多允许 400 个增删行。

## Gemini 生成说明

Gemini 经常返回包含多个 `assistant_action/tool_result` 的 transcript continuation。
协议层必须选择响应中最后一个有效 action；早期取首 action 的试运行已整体作废，
没有进入本发布。

正式运行中：

- 255/306 成功生成 patch
- 50 个任务在 32 轮内没有产生有效编辑
- Sniffy 任务在多种上下文和输出预算下均发生 API request timeout
- 仅对明确打满 16K reasoning/output 预算的任务执行 32K 技术恢复
- 32K 恢复成功 10 道；失败恢复不替换主结果
- gRPC 与 Nomad 两道 hard 的响应同时包含 `edit→finish`，adapter 修正为在无
  worktree 改动时优先执行 edit 后成功生成；两道均未通过维护性盲裁

Gemini 的下降主要来自 agent loop 生成失败，不是 Git patch 应用失败。成功生成的
255 个候选中，254 个通过 scope，196 个通过 correctness。

## 判分口径

`Resolved@1` 要求四门同时通过：

1. 成功得到非空 Git patch。
2. 只修改锚点 production file，增删行数不超过 400。
3. 历史环境下的稳定 PASS_TO_PASS correctness oracle 通过。
4. 隐藏候选模型身份的严格维护性裁判通过。

候选 patch 没有人工修复。correctness 使用与 benchmark 发布时相同的历史工具链、
容器、缓存、session behavior tests 和解法独立 Oracle。918 个 pair 全部有记录，
基础设施失败为 0。

维护性裁判为 Claude Opus 4.8。Claude 同时是候选模型，因此仍存在无法完全排除的
自裁偏差。

## 文件

| 路径 | 内容 |
|---|---|
| `report.json` | 完整总榜、难度、语言、类别和 pairwise 统计 |
| `results.jsonl` | 918 个 task-model 的最终四门结果 |
| `generation.jsonl` | 918 条紧凑 generation 状态 |
| `correctness.jsonl` | 918 条统一 schema 的 correctness 结果 |
| `judgments.jsonl` | 918 条维护性盲裁结果 |
| `patches/<model>/` | 866 个模型 patch |
| `traces/<model>/` | 918 个 agent action/tool trace |
| `judge_raw/<model>/` | 成功进入盲裁的原始裁判输出 |
| `manifest.json` | 机器可读配置、policy、记录数和总榜 |
| `SHA256SUMS` | 发布目录中所有产物的 SHA-256 |

所有 JSONL 通过任务 `id` 与模型名称连接，不能依赖物理行号。发布路径均为相对路径
或占位符，不包含本机 worktree、cache、凭据或原始绝对路径。
