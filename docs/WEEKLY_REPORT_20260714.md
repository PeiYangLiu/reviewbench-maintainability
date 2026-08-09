# ReviewBench-Maintainability 周报

> 历史周报。当前权威状态见
> [`PROGRESS_20260808.md`](PROGRESS_20260808.md)。

**周期：2026.07.08 至 2026.07.14**

## 一、本周重点

本周完成了 ReviewBench-Maintainability v1 的严格构建、全量模型评测和可视化发布。项目从原先 28 道旧口径 CLEAN 题，扩展为 **100 道可执行、可复核、明确提升代码质量与可维护性的正式题目**。

## 二、数据集建设

最终 v1 包含：

| 指标 | 数量 |
|---|---:|
| 正式任务 | 100 |
| 仓库 | 86 |
| PR | 97 |
| Hard | 11 |
| Medium | 89 |
| Go | 52 |
| Python | 36 |
| TypeScript | 5 |
| 其他语言 | 7 |

每道题均满足：

- 两个独立模型一致确认属于实质维护性提升
- 质量分不低于 4，置信度不低于 0.8
- 本质难度至少 medium
- start 和 source-only golden 各稳定运行 3 次
- PASS_TO_PASS 非空
- 测试覆盖至少一条 changed-old source line
- golden correctness 通过
- 语义重复修法已去重

严格池的推进过程为：

```text
6 → 12 → 21 → 60 → 79 → 81 → 93 → 95 → 100
```

其中大量任务通过精确历史工具链、专用 Docker、nested project root、sidecar 和 session-only 行为测试完成环境回收，没有把环境失败直接当成题目失败。

## 三、Oracle 质量审计

模型评测 smoke 阶段发现，部分 oracle 会锁死人类 golden 的私有 helper 名称或签名。

随后对全部 100 道题进行解法独立性审计，共修复 **13 道 solution-coupled oracle**，主要问题包括：

- 固定私有 helper 名称或参数
- 检查 MRO 和继承结构
- AST 局部抽取导致共享 helper 不可见
- 通过源码字符串判断具体实现
- 固定内部调用次数或 lookup 顺序
- 固定 golden 的替代依赖签名

修复后统一改为公开接口或可观察行为断言，并重新验证 start/golden 三次稳定运行与 changed-line coverage。

## 四、三模型全量评测

对 Claude Opus 4.8、Gemini 3.5 Flash 和 GPT-5.5 完成 100 题全量评测。

| 模型 | 成功生成补丁 | Correctness | 维护性通过 | Resolved@1 | Hard |
|---|---:|---:|---:|---:|---:|
| **Gemini 3.5 Flash** | 97 | 78 | 79 | **66** | 6/11 |
| Claude Opus 4.8 | 100 | 76 | 77 | **63** | 5/11 |
| GPT-5.5 | 98 | 73 | 73 | **61** | **7/11** |

主要观察：

- Gemini 在总体 Resolved@1 上排名第一。
- GPT-5.5 在 hard 子集表现最好。
- Claude 的补丁生成稳定性最好，100 题全部生成可应用补丁。
- 三模型生成的补丁均未进行人工修复。
- 最终保存了 295 个候选补丁和 300 条 generation、correctness、judgment、result 记录。

Resolved@1 的定义为：

```text
补丁可应用且范围合规
AND PASS_TO_PASS correctness 通过
AND 严格维护性盲裁通过
```

## 五、代码与工程建设

新增核心模块：

- `curate_pool.py`
- `evidence.py`
- `quality_gate.py`
- `recovery.py`
- `release.py`
- `merge_candidates.py`

新增评测脚本：

- `validate_oracles.py`
- `generate_release_patches.py`
- `judge_release_patches.py`
- `summarize_release_evaluation.py`

同时增强了 Go、Python 和编译型项目的历史环境恢复能力。

当前完整测试为 **74 项全部通过**，IDE diagnostics、数据哈希和发布门均验证通过。

## 六、数据与文档整理

正式发布目录：

```text
data/releases/v1/
```

包含：

- `tasks.jsonl`
- `oracles.jsonl`
- `provenance.jsonl`
- `report.json`
- `manifest.json`
- `SHA256SUMS`

新增详细文档：

- `docs/CODEBASE_GUIDE.md`
- `docs/DATA_PROVENANCE.md`
- `docs/PROGRESS_20260714.md`

每道题都可以通过 `provenance.jsonl` 追踪到原始 mining batch、质量判断、oracle 来源和独立性修复记录。

## 七、可视化与分享

已恢复并更新案例 Web 页面，支持：

- 100 道题逐题浏览
- reviewer concern
- 改前源码
- 人类 reference 修法
- 三模型逐题结果
- 页面顶部总榜
- Correctness、维护性和 Hard 子集统计

公网临时地址：

<https://cork-ashley-adventures-began.trycloudflare.com/>

该地址使用 Cloudflare Quick Tunnel。机器或隧道进程重启后，公网地址会变化。

## 八、持续挖掘

后台 miner 仍在运行，目前累计：

| 指标 | 数量 |
|---|---:|
| 唯一候选 | 26,702 |
| 原始记录 | 30,175 |
| Candidate files | 524 |
| 最高 batch index | 1,745 |
| 总 batch | 2,700 |

v1 已冻结，不会自动吸收后续候选。新增候选将用于 hard 扩充和 v2 储备。

## 九、当前问题

- Hard 仅占 11%，后续仍需提高高难题比例。
- Oracle 环境较为异构，完整重放依赖历史工具链和专用容器。
- 当前模型只能修改锚点生产文件，多文件重构能力可能被低估。
- 统一维护性裁判为 Claude Opus 4.8，存在无法完全排除的自裁偏差。
- 当前代码、数据和文档尚未提交到 Git。
- 公网展示地址为临时 Quick Tunnel，进程重启后 URL 会变化。

## 十、下周计划

1. 整理并提交当前 v1 代码、数据和文档。
2. 为 100 道异构 oracle 制作可移植 replay bundle。
3. 增加独立第二裁判，复核三模型排名稳定性。
4. 对第四增量的 3 道双模型候选执行 strict oracle。
5. 继续挖掘 hard 题，提高 v2 的难度占比。
6. 研究多文件 solver 协议，减少单文件 scope 对架构类任务的限制。
