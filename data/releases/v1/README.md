# ReviewBench-Maintainability v1

这是首个达到严格发布门的 100 题版本。这里的 100 指最终可评测任务，不是候选数，也不是只通过质量审阅但尚未构建 oracle 的题目数。

## 文件

| 文件 | 行数 | 内容 |
|---|---:|---|
| `tasks.jsonl` | 100 | 最终任务、精确 start/target、局部与完整同文件 golden 证据、两个模型的独立质量判断、发布验证摘要 |
| `oracles.jsonl` | 100 | 与任务 ID 一一对应的 correctness、PASS_TO_PASS、coverage、环境和 recovery 元数据 |
| `provenance.jsonl` | 100 | 每题所属挖掘批次、原始候选来源、质量模型、oracle 来源和记录哈希 |
| `report.json` | 1 | 发布层聚合统计 |
| `manifest.json` | 1 | 版本、门槛、文件哈希和统计口径 |
| `SHA256SUMS` | 4 | 四个机器生成数据文件的 SHA-256 |

`tasks.jsonl` 与 `oracles.jsonl` 按 `id` 一一对应。读取方不能依赖两个文件的物理行号，应按 `id` 连接。

完整代码地图、任务级数据血缘和当前状态分别见：

- [`../../../docs/CODEBASE_GUIDE.md`](../../../docs/CODEBASE_GUIDE.md)
- [`../../../docs/DATA_PROVENANCE.md`](../../../docs/DATA_PROVENANCE.md)
- [`../../../docs/PROGRESS_20260714.md`](../../../docs/PROGRESS_20260714.md)

## 严格保证

每一道题都满足以下条件：

1. 原代码中存在可从证据中明确指出的维护性问题。
2. 人类 golden 对该问题有实质、可归因的改善。
3. 主要目标是代码质量或可维护性，不是功能修复、功能新增、性能、安全、文档、测试补充或纯样式调整。
4. 两个不同模型独立判断并一致通过 `quality-improvement/v1`。
5. 两个判断的质量分均至少为 4，置信度均至少为 0.8。
6. 保守合并后的本质难度至少为 medium。只有两个模型都判断为 hard 时，发布层才记为 hard。
7. start 与 source-only golden 均经过三次稳定运行。
8. 存在至少一个非空 PASS_TO_PASS 测试。
9. 测试真实执行至少一条局部修法涉及的 changed-old source line。
10. golden correctness 通过。
11. 按仓库、PR、文件和归一化局部 patch 做语义去重。
12. 每个仓库最多保留 5 道。

发布命令以目标 100 运行并返回 0。`report.json` 中 `complete=true`、`eligible=100`、`selected=100`，且 `failure_reasons` 为空。

## 组成

| 维度 | 统计 |
|---|---|
| 任务 | 100 |
| 仓库 | 86 |
| PR | 97 |
| 难度 | 11 hard，89 medium |
| 语言 | Go 52，Python 36，TypeScript 5，PHP 2，C 1，C++ 1，C/C++ 1，Java 1，Rust 1 |
| 类别 | modularity 24，api_design 19，complexity 16，duplication 16，testability 11，readability 8，dead_code 4，naming 2 |
| PASS_TO_PASS | 最少 1，中位数 3，最多 742，总计 3,297 |
| 质量判断 | GPT-5.6 Sol 100 条，Claude Opus 4.8 100 条 |

## Oracle 元数据

`oracles.jsonl` 保存最终发布所用的最丰富成功记录。不同仓库的历史构建系统差异很大，因此记录形状允许语言和 recovery 专用字段，例如：

- `pass_to_pass` 或 `pass_to_pass_tests`
- `test_command`、`commands`
- `coverage`、`changed_lines_covered`
- `environment`、`docker_image`、`toolchain`
- `behavior_assertions`
- `source_only_golden`
- `recovery_status`

模型评测前又执行了一轮 solution-coupling 审计。13 道题的 oracle 被改为只检查公开或可观察行为，不再锁死 golden 的私有 helper 名称、签名、MRO、源码位置或内部调用次数。修复记录保存在对应 oracle 的 `oracle_independence_fix` 字段中。

所有本机绝对路径都已替换为 `${REVIEWBENCH_SESSION_FILES}`、`${REVIEWBENCH_REPOSITORY}` 或 `${HOME}` 占位符。它们用于说明验证来源，不表示发布包附带临时 worktree、缓存或日志。正式数据目录只包含任务和 oracle 元数据，不包含挖掘中间文件。

## 复核发布门

在项目根目录运行：

```bash
reviewbench-release \
  --tasks data/releases/v1/tasks.jsonl \
  --evaluations data/releases/v1/oracles.jsonl \
  --target 100 \
  --out /tmp/reviewbench-v1-tasks.jsonl \
  --report /tmp/reviewbench-v1-report.json

cmp data/releases/v1/tasks.jsonl /tmp/reviewbench-v1-tasks.jsonl
sha256sum -c data/releases/v1/SHA256SUMS
```

第一条命令必须返回 0，`cmp` 必须无输出，哈希校验必须全部通过。
