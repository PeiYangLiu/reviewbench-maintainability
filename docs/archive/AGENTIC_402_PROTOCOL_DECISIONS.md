> **Historical archive — not current package instructions or a current leaderboard.**
> Preserve the protocol, roster, denominator, incomplete/withdrawn flags and audit
> caveats stated below; do not reinterpret these results using today's judge policy.
> See the [archive index](README.md) and [current documentation](../README.md).
> Historical commands and local artifact references may require assets not shipped
> in this repository. Publication changes are limited to this notice, portable
> path placeholders and repaired links; original bytes remain in the backup.

# ReviewBench 402：剩余契约问题与待确认的版本修订

更新时间：2026-09-21（America/Los_Angeles）。

本文件是修订提案，**不是已执行的评测、准入或授权**。原始 402 个 ID、
原始测试、评分规则、已消费模型尝试和既有结果均保留。

## 当前可直接推进的工作

IoTDB 已完成真实 public 构建和 start3/golden3 校准，六轮原始 Java 测试
均为 1/1。独有运行缓存已移到源码目录外，没有跳过许可证检查。
其唯一首次模型 episode 已完成：20 份真实模型响应、16 份工具反馈，
模型候选的原正确性测试 1/1 通过，原三裁判 T/F/T，最终未解决。
publication104 已完成真实上传、独立 GET 和 220 项物理文件恢复核验，
汇总为 397 completed／174 resolved／223 not resolved，另 3 incomplete
和 2 model_failed。该流程不依赖下面三个问题的决策。

## 三个独立问题

| 题目 | 已证实的问题 | 对原协议的影响 |
|---|---|---|
| Agoric 8072 | 原测试的模拟回调在第 134、166、204 行把 initiate/retrieve/discard 对象都断言为同一个旧具体指针类型；目标实现使用不同的具名具体类型，且事件回调接口也发生变化。 | 保留旧测试字节时，直接采用目标实现会遇到类型/接口冲突。添加空旧类型或单个别名不修复真实调用。 |
| Cosmos ibc-go 3520 | 既有候选通过可选旧参数维持调用形式，但实际忽略调用者传入的 Subspace，改读 Keeper 中的另一 Subspace。两者取值不同时，迁移结果可以不同。 | 该候选尚未证明保留旧显式输入语义，也不是目标的精确两参数 API。静态编译形状检查不等于语义校准。 |
| TiDB 68028 | 原模型答案和 162 项正确性证据已保留；原裁判完整输入为 17,073,305 字符，10,667,728 个 o200k tokens。原三裁判单请求准入尚未闭合。 | 缺少完整三裁判结果，不应把缺项填成失败、裁掉输入后仍称同一请求，或重复已消费的模型生成。 |

## 建议的独立版本方案（尚未执行）

1. **Agoric：修订测试适配层，而不是添加生产兼容占位代码。**
   保留原 18 项行为场景与语义断言；审查把旧具体类型探针改为真实 JSON
   消息/事件接口的观测方式。逐项记录旧、新断言与事件顺序的对应关系，
   再在完整 start 和完整目标候选上执行新版本校准。
   仅替换类型名字并不自动构成等价证明。
2. **Cosmos：明确新的默认参数来源契约。**
   测试适配必须明确区分调用者 Subspace 与 Keeper Subspace，并保留两者
   不同取值的反例。原迁移结果断言、存储键及参数读取行为逐项对照，
   “忽略旧实参”本身不是语义等价的证据。
3. **TiDB：为超长输入另立裁判协议版本。**
   保留原答案、原 162 项正确性证据和原三位裁判；若启用分块/多轮复核，
   应明确输入覆盖、块间依赖、失败处理、预算和最终三票形成规则。
   新协议结果与旧单请求协议结果分别标记，不把多轮复核冒充原始单请求。

只有在修订方案确认后，才实施对应新版本；现有正式统计不提前纳入这些结果。
这里没有提出删除题目、减少断言、替换评委或重新抽样答案。

## 另一个已完成的只读诊断：终止协议

对正式 398 条 generation 的 trace，398/398 通过 SHA256 与任务身份核验。
其中 365 条 `format_error_limit` 均曾通过非精确命令成功输出提交标记，
但原环境仍记为 `submitted=false`；常见形式是：

```bash
cd /workspace && echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
```

首次此类事件之后记录了 2,104 次模型响应和 504 次工具动作。90 题还有
非简单提交重试动作；没有建立标记时刻与最终 patch 的配对哈希，
这些证据并未证明 patch 不变、所有后续调用都无用或这些题都应算成功。
旧结果、终止原因和成本记录不改。

后续 agent 协议宜单独版本化验证明确的提交工具，避免把 shell 命令的
文本完全相等作为唯一结束信号；不在本次已冻结运行中途切换。

## 可复核证据

以下路径均相对项目根目录：

- `.benchmarks/agentic-agoric8072-reference-completion-20260921-v1/MAIN_FIXED_TEST_TYPE_HOLD.json`
- `.benchmarks/agentic-ibcgo3520-reference-completion-20260921-v1/MAIN_SEMANTIC_HOLD.json`
- `.benchmarks/agentic-ibcgo3520-reference-completion-20260921-v1/MIGRATION_SEMANTICS_REVIEW.json`
- `docs/AGENTIC_402_REMAINING_4.md`
- `.benchmarks/agentic-iotdb5976-reference-completion-20260921-v1/main-native-corrected-runtime-v2/RESULT.json`
- `.benchmarks/agentic-iotdb5976-reference-completion-20260921-v1/main-native-corrected-runtime-v2-native-reports/CAPTURE.json`
- `.benchmarks/agentic-iotdb5976-reference-completion-20260921-v1/MAIN_FIRST_EPISODE_REVIEW.json`
- `.benchmarks/agentic-402-termination-audit-20260921-v1/REPORT.md`
- `.benchmarks/agentic-402-termination-audit-20260921-v1/RESULT.json`
