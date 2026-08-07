# ReviewBench Strict Reserve 20260721

这是 v1 发布后的第一批严格储备题，不会修改或覆盖 `data/releases/v1/`。

## 结果

| 指标 | 数量 |
|---|---:|
| 新挖掘候选 | 25,535 |
| Prefilter 后 | 22,244 |
| 全量可进入质量池 | 16,647 |
| 第一批优先证据任务 | 3,000 |
| 完整严格证据 | 2,565 |
| GPT-5.6 Sol 严格通过 | 419 |
| Claude Opus 4.8 严格通过 | 218 |
| 语义去重后双模型任务 | 214 |
| 最终 strict reserve | **206** |
| 未通过或阻塞 | 8 |

最终 206 道包含：

- 15 hard
- 191 medium
- 178 个仓库
- 与 v1 无 ID 或语义 patch 重复
- 每仓库最多 5 道

## 语言分布

| 语言 | 数量 |
|---|---:|
| Go | 85 |
| Python | 50 |
| Java | 11 |
| Scala | 9 |
| TypeScript | 8 |
| C# | 7 |
| Kotlin | 6 |
| PHP | 6 |
| Rust | 6 |
| C++ | 5 |
| Other | 5 |
| JavaScript | 3 |
| C | 2 |
| C/C++ | 1 |
| Ruby | 1 |
| Shell | 1 |

## 严格门

每道储备题均满足：

1. 完整 start source、anchor-local golden 和完整同文件 diff。
2. GPT-5.6 Sol 与 Claude Opus 4.8 独立一致通过 `quality-improvement/v1`。
3. 两个判断的质量分至少为 4，置信度至少为 0.8。
4. 两个模型都判断本质难度至少为 medium。
5. start 与 source-only golden 各稳定运行 3 次。
6. PASS_TO_PASS 非空。
7. 测试或行为 oracle 真实命中 changed-old source line。
8. golden correctness 通过。
9. Oracle 不绑定 golden 私有 helper、签名、源码布局或 target tests。
10. 与 v1 和本批内部语义去重。

## 未通过的 8 道

- Miden VM：source-only golden 依赖 PR 中无关 API 演进，无法在 start base 编译。
- JoinMe：review 锚点实际为测试代码，没有可归因 production changed-old line。
- Nussknacker：golden 连续三次出现确定性行为回退。
- Deckhouse：golden 行为回退。
- Nydus：source-only golden 依赖无关 target API 演进。
- Java Pub/Sub：golden 修复 `ConcurrentModificationException`，属于功能变化。
- MapLibre：golden 行为回退。
- ONNX Runtime QNN：缺少不可再分发的 QAIRT/QNN SDK。

## 文件

| 文件 | 说明 |
|---|---|
| `tasks.jsonl` | 206 道严格储备任务 |
| `oracles.jsonl` | 与任务一一对应的最终规范化 Oracle |
| `provenance.jsonl` | 每题来源、模型、commit 和记录哈希 |
| `report.json` | 206 道储备集报告 |
| `failures.jsonl` | 8 道未通过或阻塞题 |
| `cumulative_preview.jsonl` | v1 100 道加本批 206 道，共 306 道预览 |
| `cumulative_report.json` | 306 道累计统计 |
| `manifest.json` | 机器可读构建口径和哈希 |
| `SHA256SUMS` | 数据完整性校验 |

`cumulative_preview.jsonl` 只是 v2 候选预览。持续 miner 和剩余 13,647 道未处理质量池仍会产生更多储备题，因此尚未冻结正式 v2。

## 306 题模型评测

完整三模型评测位于：

```text
evaluations/three-model-20260724/
```

最终 Resolved@1：

- GPT-5.5：184/306
- Gemini 3.5 Flash：182/306
- Claude Opus 4.8：180/306
