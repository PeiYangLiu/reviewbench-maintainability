# ReviewBench Strict Reserve 20260728

本目录冻结 2026-07-28 完成的下一轮严格维护性题目。

## 最终规模

- 新增 strict tasks：169
- hard：14
- medium：155
- repositories：160
- pull requests：164
- 与 v1 及 20260721 reserve 的 ID 重复：0
- 与旧 306 题的语义修法重复：0
- 本批内部语义重复：0

累计预览：

- tasks：475
- hard：40
- medium：435
- repositories：410

## 筛选漏斗

| 阶段 | 数量 |
|---|---:|
| 挖掘唯一候选 | 109,619 |
| 可进入质量池 | 70,939 |
| 优先 evidence 批次 | 3,000 |
| 完整 evidence | 2,581 |
| GPT-5.6 Sol 严格通过 | 533 |
| Claude Opus 4.8 二次通过 | 280 |
| Oracle 前语义去重 | 268 |
| Oracle strict pass | 180 |
| 同解法重复排除 | 11 |
| 最终新增 reserve | 169 |

## 质量门

每题必须同时满足：

- 原代码存在明确 code smell
- reference fix 有实质、可归因的维护性提升
- 维护性是主要目标，不是功能修复
- 非纯 style 或机械 easy
- 本质难度至少 medium
- GPT-5.6 Sol 与 Claude Opus 4.8 一致严格通过
- quality score 至少 4
- confidence 至少 0.8
- 不接受 partial

## Oracle 门

每个发布题都完成：

- 精确 `start_sha`
- source-only golden
- start 3 次稳定运行
- golden 3 次稳定运行
- 非空 PASS_TO_PASS
- 真实公开或可观察行为断言
- changed-old production line coverage
- 不使用 target tests 掩盖回归
- 不绑定 golden 私有 helper、签名、AST、MRO 或内部调用次数

语言线结果：

| 语言线 | 输入 | Strict pass |
|---|---:|---:|
| Native C/C++ | 35 | 33 |
| Java | 42 | 33 |
| Kotlin | 41 | 28 |
| Rust | 32 | 23 |
| TypeScript/JavaScript | 29 | 22 |
| C# | 26 | 14 |
| Scala/Ruby/PHP | 44 | 12 |
| Go/Python | 12 | 11 |
| Other | 7 | 4 |
| 合计 | 268 | 180 |

## 最终审计修正

首次 Native 结果使用了 toy reimplementation 和 `#line` probe，未执行真实仓库
production code。该批 35 条结果全部作废并归档。Native 线随后从零重做，实际
checkout、编译和链接真实 repository source/header，使用真实 gcov/llvm-cov，
最终 33/35 通过。

Other 线也复核了 extracted `.behavior.inc` 和 stub 风险，撤销 2 个不能证明真实
production coverage 的 pass。

最终 Oracle 审计还识别出 10 组同 PR、同 source scope、同修法和同行为 Oracle
的重复评论，共排除 11 道冗余题。排除记录在
`semantic_duplicate_exclusions.json`。

## 文件

| 文件 | 内容 |
|---|---|
| `tasks.jsonl` | 169 道最终 strict tasks |
| `oracles.jsonl` | 169 条最丰富的成功 Oracle 记录 |
| `provenance.jsonl` | 任务、质量判断、Oracle 来源与记录哈希 |
| `failures.jsonl` | 88 个 Oracle 拒绝/阻塞与 11 个语义重复 |
| `report.json` | 本批漏斗、难度、语言、仓库和累计统计 |
| `cumulative_preview.jsonl` | v1 + 20260721 + 20260728，共 475 道 |
| `cumulative_report.json` | 累计统计 |
| `manifest.json` | 机器可读发布配置和 artifact hash |
| `SHA256SUMS` | 全部发布文件的 SHA-256 |

所有绝对路径均替换为 `${REVIEWBENCH_SESSION_FILES}`、
`${REVIEWBENCH_REPOSITORY}` 或 `${HOME}` 占位符。
