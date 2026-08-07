# ReviewBench v1 三模型评测（2026-07-14）

本目录保存 Claude Opus 4.8、Gemini 3.5 Flash 和 GPT-5.5 在 ReviewBench-Maintainability v1 全部 100 题上的一次完整评测。

## 最终结果

| 模型 | 成功生成补丁 | Scope | Correctness | 维护性盲裁 | Resolved@1 | Hard |
|---|---:|---:|---:|---:|---:|---:|
| Gemini 3.5 Flash | 97/100 | 97/100 | 78/100 | 79/100 | **66/100** | **6/11** |
| Claude Opus 4.8 | 100/100 | 100/100 | 76/100 | 77/100 | **63/100** | **5/11** |
| GPT-5.5 | 98/100 | 98/100 | 73/100 | 73/100 | **61/100** | **7/11** |

整体排名是 Gemini 66、Claude 63、GPT-5.5 61。Hard 子集的顺序不同，GPT-5.5 为 7/11，Gemini 为 6/11，Claude 为 5/11。

`Resolved@1` 只有在以下四项全部通过时才为 true：

1. 模型输出能转换为非空补丁。
2. 只修改锚点生产文件，改动不超过 400 行。
3. 已验证的 PASS_TO_PASS correctness oracle 通过。
4. 维护性盲裁为 `addressed=yes`、质量分至少 4、行为保持且改动聚焦。

“成功生成补丁”指模型的 SEARCH/REPLACE 输出成功定位 start 源码，并由系统生成规范 unified diff。所有已经生成的补丁都能应用到精确 `start_sha`；generation failure 发生在补丁文件生成之前。

## 生成设置

三个模型使用相同的 system prompt、reviewer concern 和 start revision 源码。提示中不包含 target、golden patch、质量判断或 oracle 结果。

- 模型：`claude-opus-4.8`、`gemini-3.5-flash`、`gpt-5.5`
- 输出协议：精确 `SEARCH/REPLACE`
- 每题最多 3 次协议重试
- 统一输出预算：16,384 tokens
- reasoning effort：Gemini 和 GPT-5.5 为 medium
- 源码上下文：94 题为完整文件，6 个超大文件为锚点周围最多 100,000 字符
- 候选范围：只允许修改 reviewer 锚定的生产文件
- 候选修复：禁止

Gemini 在 XRMocap 一题的 3 次 16K 调用都被 reasoning token 用尽并返回空正文。该题单独用 32K 恢复一次，补丁成功生成。其他不可应用输出没有增加尝试次数：

- Gemini generation failure：Azure aaz-dev-tools、OPA frameworks 第二题、TiDB 57553
- GPT-5.5 generation failure：XRMocap、TiDB 57553
- Claude generation failure：无

## Correctness

每道题都复用 v1 发布时验证过的历史环境和 PASS_TO_PASS 集合。每个 task-model 组合执行以下流程：

1. 恢复到精确 `start_sha`。
2. 恢复同一份 session-only 行为 oracle。
3. 应用原始模型补丁。
4. 按需要编译。
5. 运行完整 P2P 行为测试和非耦合回归。

评测没有应用 target 或 golden，没有修改候选补丁。基础设施问题可以修复后重跑，候选编译或测试失败不能重试为成功。

模型评测前审计了 100 道 oracle。13 道题的 oracle 原先含有 golden 私有 helper、签名、MRO、源码位置或内部调用次数断言，已改为公开或可观察行为断言，并重新验证 start/golden 各 3 次。

## 维护性盲裁

统一裁判是 `claude-opus-4.8`。裁判只看到 reviewer concern、start 源码和候选 diff，不看到候选模型名称、golden 或测试结果。

候选按设计不能修改测试。测试由独立 correctness oracle 负责，因此裁判不会仅因候选未新增测试而拒绝生产代码修法。

这个设计仍有一个明确限制：Claude 同时是候选模型之一。虽然模型身份被隐藏，自裁偏差仍不能完全排除。结果应结合 correctness 和逐题数据解释，不应只看单一盲裁通过率。

## 成对结果

| 模型对 | 两者都解出 | 左侧独有 | 右侧独有 | 两者都未解出 |
|---|---:|---:|---:|---:|
| Claude vs Gemini | 56 | 7 | 10 | 27 |
| Claude vs GPT-5.5 | 54 | 9 | 7 | 30 |
| Gemini vs GPT-5.5 | 53 | 13 | 8 | 26 |

## 文件

| 路径 | 内容 |
|---|---|
| `report.json` | 最终排行榜、分难度、语言、类别及成对统计 |
| `results.jsonl` | 300 个 task-model 的四门结果 |
| `generation.jsonl` | 生成状态、协议重试、token usage 和补丁路径 |
| `correctness.jsonl` | 归一化后的 300 条 oracle 执行结果 |
| `judgments.jsonl` | 300 条盲裁 verdict |
| `patches/<model>/` | 295 个可应用候选补丁 |
| `manifest.json` | 机器可读评测设置与总结果 |
| `SHA256SUMS` | 本目录最终产物哈希 |

所有任务通过 `id` 连接，不能依赖 JSONL 物理行号。
