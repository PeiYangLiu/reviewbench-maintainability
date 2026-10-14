> **Historical archive — not current package instructions or a current leaderboard.**
> Preserve the protocol, roster, denominator, incomplete/withdrawn flags and audit
> caveats stated below; do not reinterpret these results using today's judge policy.
> See the [archive index](README.md) and [current documentation](../README.md).
> Historical commands and local artifact references may require assets not shipped
> in this repository. Publication changes are limited to this notice, portable
> path placeholders and repaired links; original bytes remain in the backup.

# ReviewBench-Maintainability 数据血缘

更新时间：2026-08-08

本文以 v1 的逐字段血缘为基础，并延伸到两个 reserve、475 题 general
release 和 414 题 hard release。它回答以下问题：

1. v1 100 道、475 题累计集和 414 hard 分别从哪些候选池进入。
2. 一道题的 comment、commit、before、golden、质量判断和 oracle 分别来自哪里。
3. `data/releases/v1/` 每个文件怎样生成。
4. 三模型评测的 generation、correctness、judgment 和结果怎样连接。
5. 哪些是最终数据，哪些只是 session 中间产物。

## 1. 最终数据总览

| 文件 | 记录数 | 大小 | SHA-256 |
|---|---:|---:|---|
| `tasks.jsonl` | 100 | 2,086,023 bytes | `5a48d7522abc9bf3f0fd04247f7564b2547013a1e41202767d061390cc634b3b` |
| `oracles.jsonl` | 100 | 821,459 bytes | `96d8f537267bd6d08e3024b4a578b2b87ac2fbf82bc3a32b9829885e26186b39` |
| `provenance.jsonl` | 100 | 117,225 bytes | `65c16bb127012dfdd3e4d31b5ae15e883b88e8759b4cff65defb3d85f1e9260e` |
| `report.json` | 1 | 702 bytes | `9c5cc54923a6ffb35956ffc1aaeac439d060e98cd36e9f1acc894bd4c60a7e60` |

`manifest.json` 记录门槛和统计，`SHA256SUMS` 用于完整性验证。

## 2. 一道候选题最初从哪里来

原始候选来自 GitHub 已合并 PR 的 review comment。

任务身份由以下字段组成：

- GitHub repository
- PR number
- review comment ID
- maintainability category

ID 形如：

```text
github__owner__repo__pull_123__comment_456__modularity
```

原始信息来源：

| 字段 | 来源 |
|---|---|
| `repo`、`pr_number` | GitHub PR metadata |
| comment author、body、URL、时间 | GitHub review comment |
| `task_instruction` | 去除 suggestion 泄露后的 reviewer comment |
| `locality.path/line` | review comment anchor |
| `start_sha` | comment 所在 commit |
| `target_sha` | 合并 PR 的最终 head |
| `base_sha`、`merge_commit_sha` | PR metadata |
| thread resolved | GitHub GraphQL review thread |
| file/region touched | comment 后 commit/file 变化 |
| test/source paths | PR changed files |

`provenance.jsonl` 中的 `mined_source_file` 和 `mined_source_line` 指向候选最初出现的 batch 文件。该路径属于被忽略的 mining artifact，不是发布依赖。

## 3. 候选筛选的完整时间线

### 3.1 首轮严格质量审计

| 阶段 | 数量 |
|---|---:|
| before、局部 patch、完整同文件 patch 齐全 | 1,763 |
| 双模型一致通过质量门 | 46 |
| 初始 strict covered oracle | 6 |

这一步证明旧 `CLEAN` 口径不足。旧 CLEAN 只能说明 golden 通过测试并回应 reviewer，不保证题目本身是明确、实质的质量提升。

### 3.2 历史 159 道 medium/hard 池

来源：

```text
data/difficulty/hp_strict_scored_v2.jsonl
```

漏斗：

| 阶段 | 数量 |
|---|---:|
| 原始任务 | 159 |
| commit/file 可达 | 137 |
| 完整证据 | 135 |
| GPT 第一模型通过 | 24 |
| 双模型通过 | 19 |
| strict oracle 通过 | 6 |

这 6 道与首轮 6 道无重复，严格池到 12。

### 3.3 专用环境 recovery

第一轮回收新增 9 道，严格池从 12 到 21。

第二轮把 39 道环境失败分为：

- environment 11
- baseline 10
- PASS_TO_PASS 10
- coverage/oracle 8

39 道全部回收，发布层语义去重后严格池到 60。

实际使用过的 recovery 类型包括：

- 精确 Go toolchain
- nested `go.mod`
- build tags
- Redis/MySQL sidecar
- Bazel、Gradle
- Xdebug、gcov
- Python 3.8/3.9/3.11/3.12
- 历史 NumPy、Cython、Meson
- Fortran、OpenBLAS
- no-deps editable install
- SCM version override
- session-only 行为测试

### 3.4 持续 mining 第一增量

| 阶段 | 数量 |
|---|---:|
| 新 mined candidates | 1,833 |
| Go/Python 质量池 | 799 |
| 完整证据 | 687 |
| GPT 通过 | 78 |
| 双模型通过 | 19 |
| strict oracle 通过 | 19 |

严格池从 60 到 79。

### 3.5 未审阅长尾

| 阶段 | 数量 |
|---|---:|
| 因每仓库 cap 未审阅的候选 | 479 |
| 完整证据 | 358 |
| GPT 通过 | 17 |
| 双模型通过 | 2 |
| strict oracle 通过 | 2 |

严格池从 79 到 81。

### 3.6 第二增量主池

| 阶段 | 数量 |
|---|---:|
| mined candidates | 693 |
| 质量池 | 368 |
| 完整证据 | 322 |
| GPT 通过 | 30 |
| 双模型通过 | 14 |
| strict oracle 通过 | 13 |

13 道 oracle 成功题中有一对共享同一局部参考修法，语义去重后净增 12。严格池从 81 到 93。

### 3.7 第二增量跨语言回收

此前只因语言未选而漏审的候选重新进入流程：

| 阶段 | 数量 |
|---|---:|
| 质量池 | 63 |
| 完整证据 | 49 |
| GPT 通过 | 5 |
| 双模型通过 | 2 |
| strict oracle 通过 | 2 |

两道都是 TypeScript，分别为 Sourcegraph 和 Sentry。严格池从 93 到 95。

### 3.8 第三增量

| 阶段 | 数量 |
|---|---:|
| mined candidates | 395 |
| 质量池 | 254 |
| 完整证据 | 198 |
| GPT 通过 | 12 |
| 双模型通过 | 6 |
| strict oracle 通过 | 6 |

6 道分别来自：

- JohnStarich/go
- Buildah
- CPython
- SpiceDB
- google-cloud-python
- Altair

此时严格合格候选为 101。发布器保留 100 道，CPython 进入，Vitess medium 题转入储备。

### 3.9 第四增量储备

| 阶段 | 数量 |
|---|---:|
| mined candidates | 251 |
| 质量池 | 158 |
| 完整证据 | 120 |
| GPT 通过 | 15 |
| 双模型通过 | 3 |

这 3 道来自同一 Optimism PR，未执行 v1 oracle，不计入 v1。

## 4. 最终 100 道的 cohort 组成

`provenance.jsonl` 对每题记录 `cohort`。

| Cohort | 最终题数 |
|---|---:|
| `strict_base_recovery_20260713` | 59 |
| `continuous_mining_increment1_20260714` | 19 |
| `uncapped_longtail_20260714` | 2 |
| `increment2_main_20260714` | 12 |
| `increment2_cross_language_20260714` | 2 |
| `increment3_20260714` | 6 |
| 合计 | **100** |

原严格 60 中有一道 Vitess medium 在最后 101 选 100 时被替换，因此最终 base cohort 为 59。

## 5. `tasks.jsonl` 如何生成

### 5.1 Candidate metadata

来自 mining 与 prefilter：

- reviewer concern
- PR/comment identity
- start/target
- source/test paths
- addressed evidence

### 5.2 Quality evidence

`reviewbench-evidence` 从本地 commit cache 读取：

```text
git show <start_sha>:<path>
git show <target_sha>:<path>
```

然后计算真实两点 diff。

保存：

- `quality_evidence.before`
- `quality_evidence.patch`
- `quality_evidence.full_patch`
- patch completeness/truncation status

局部 patch 用来判断评论对应修法，完整同文件 patch 用来检查关联 helper 和质量回退。

### 5.3 双模型 quality judgment

每题保存两条：

- `gpt-5.6-sol-quality-v1`
- `claude-opus-4.8-quality-v1`

两个模型都必须：

- pass
- maintainability primary
- score 至少 4
- confidence 至少 0.8
- medium/hard
- 非行为变化
- 非 style-only
- 无质量回退

### 5.4 Release validation

`reviewbench-release` 写入：

- `judged_difficulty`
- `pass_to_pass`
- `golden_correctness`
- `changed_lines_covered`
- `quality_gate_version`

难度保守合并：

- 两个模型都 hard，发布记 hard
- 只要至少一个模型为 medium，发布记 medium

最终为 11 hard、89 medium。

## 6. `oracles.jsonl` 如何生成

每个最终任务从所有 direct oracle 和 recovery 结果中选取最丰富的成功记录。

成功条件：

- golden correctness true
- PASS_TO_PASS 至少 1
- changed-line coverage true
- 无 setup error

### 6.1 Oracle 来源分布

| 来源 artifact | 题数 |
|---|---:|
| `increment2_oracle_all_normalized.jsonl` | 10 |
| `recovery_wave2_p2p.jsonl` | 10 |
| `recovery_wave2_baseline.jsonl` | 9 |
| `recovery_wave2_env_b.jsonl` | 6 |
| `incremental_recovery_go_1.jsonl` | 6 |
| `incremental_recovery_go_2.jsonl` | 6 |
| `recovery_wave2_env_a.jsonl` | 5 |
| `increment3_oracle_success5_normalized.jsonl` | 5 |
| `recovery_wave2_coverage_oracle.oracle.jsonl` | 4 |
| `recovery_wave2_coverage_oracle.jsonl` | 4 |
| `oracle_reserve_go.jsonl` | 4 |
| `incremental_recovery_python_1.jsonl` | 3 |
| `incremental_recovery_python_2.jsonl` | 3 |
| `legacy159_recovery_python_monorepos.jsonl` | 3 |
| `legacy159_recovery_go.jsonl` | 3 |
| `legacy159_oracle_native_normalized.jsonl` | 3 |
| `longtail_recovery_go.jsonl` | 2 |
| `legacy159_oracle_typescript_normalized.jsonl` | 2 |
| `legacy159_recovery_scientific_python.jsonl` | 2 |
| 其余 10 个单题 artifact | 10 |

完整单题来源见 `provenance.jsonl` 的 `oracle_source_artifact`。

### 6.2 Solution-coupling 审计

模型评测 smoke 发现 CPython oracle 调用了 golden 私有 helper 的固定签名。随后对全部 100 道做审计，共修复 13 道：

| 任务 | 原耦合 | 修复后行为 |
|---|---|---|
| CPython 106703 | `_translate` 和 `_join_translated_parts` 固定签名 | 只测公开 fnmatch/glob regex 行为 |
| Azure aaz-dev-tools 153 | 固定三个 private portal helper | 完整模块生成 portal JSON |
| Gaffer 4735 | 抽取 private method、固定内部调用次数 | UI 入口和无编辑副作用 |
| FReDT 130 | 只抽取命名 class method | 真实 generator 最终 dataset |
| easycut 896 | 文本定位 private method | 公开事件入口、G-code 和状态 |
| Portkit 101 | 只抽取 `simulate_ai_conversion` | 完整 backend 的 DB/cache/file 状态 |
| Neuroconv 1003 | 源码字符串判断变量名 | in-memory/on-disk DataIO 行为 |
| BigQuery DataFrames 149 | 固定三个 helper 名 | 公开 `get_dummies()` 结果 |
| Lyse 119 | 固定文件、private global 和函数名 | 注册、延迟和 worker 通信 |
| Pwndbg 1159 | 只执行 Chunk AST、固定 lookup 顺序 | 公开属性、解码和缓存 |
| Draco 245 | 固定 inheritance/MRO | 真实 container dataset 和序列化 |
| Anaconda 5250 | 固定 `_write_s390_device_config` | 完整 storage task 和 chzdev 行为 |
| Cosmos SDK 21554 | 固定替代签名和 factory 次数 | 公开 genesis export 结果 |

每个修复都重新完成：

- start 3 次
- source-only golden 3 次
- 非空 P2P
- changed-old-line coverage

修复详情内嵌在 `oracle_independence_fix`。

### 6.3 路径处理

本机绝对路径替换为：

- `${REVIEWBENCH_SESSION_FILES}`
- `${REVIEWBENCH_REPOSITORY}`
- `${HOME}`

它们是 provenance placeholder，不表示临时 worktree 随发布提交。

## 7. `provenance.jsonl` 如何生成

每条与 `tasks.jsonl`、`oracles.jsonl` 按 ID 一一对应。

主要字段：

- `cohort`
- `mined_source_file`
- `mined_source_line`
- `start_sha`
- `target_sha`
- `evidence_version`
- `quality_gate_version`
- `quality_models`
- `quality_difficulties`
- `released_difficulty`
- `oracle_source_artifact`
- `oracle_solution_coupling_audited`
- `oracle_independence_fix`
- `pass_to_pass`
- `changed_lines_covered`
- `task_record_sha256`
- `oracle_record_sha256`

它提供任务级而不是只提供汇总级血缘。

## 8. `report.json`、`manifest.json`、`SHA256SUMS`

### `report.json`

由 `reviewbench-release` 自动生成，记录：

- complete
- selected
- eligible
- failures
- difficulty
- language
- category
- repositories
- selection policy

### `manifest.json`

手工冻结版本口径和机器可读要求：

- quality gate version
- oracle schema
- target
- selection limits
- quality threshold
- stability threshold
- statistics
- artifact hashes

### `SHA256SUMS`

覆盖：

- tasks
- oracles
- provenance
- report

## 9. 三模型评测数据血缘

目录：

```text
data/releases/v1/evaluations/three-model-20260714/
```

### 9.1 `generation.jsonl`

300 条 task-model generation status。

输入：

- `tasks.jsonl`
- start revision source
- reviewer concern

不输入：

- target
- golden
- quality judgments
- oracle results

统一设置：

- SEARCH/REPLACE
- 最多 3 次协议重试
- 16,384 output tokens
- 100,000 字符 source context 上限
- 94 完整文件
- 6 锚点 excerpt

结果：

| 模型 | 可应用 | 失败 |
|---|---:|---:|
| Claude Opus 4.8 | 100 | 0 |
| Gemini 3.5 Flash | 97 | 3 |
| GPT-5.5 | 98 | 2 |

Gemini XRMocap 三次 16K 都被 reasoning 用尽且正文为空，因此单独用 32K 恢复。其他不可应用输出没有增加尝试。

### 9.2 `patches/`

保存 295 个实际候选 patch：

- Claude 100
- Gemini 97
- GPT-5.5 98

补丁是最终 generation 的直接产物，没有人工修复。

### 9.3 `correctness.jsonl`

300 条归一化结果。

原始执行按 12 个环境来源分片，历史执行器输出过多种 schema。`summarize_release_evaluation.py` 统一为：

- `oracle_replayed`
- `patch_apply`
- `correctness_ok`
- `pass_to_pass_total`
- `pass_to_pass_passed`
- `failed_tests`
- `command`
- `source_shard`

Correctness 结果：

- Claude 76
- Gemini 78
- GPT-5.5 73

### 9.4 `judgments.jsonl`

300 条隐藏候选模型身份的维护性判断。

裁判：Claude Opus 4.8。

通过要求：

- addressed yes
- quality 至少 4
- behavior preserved
- focused

结果：

- Claude 77
- Gemini 79
- GPT-5.5 73

### 9.5 `results.jsonl`

300 条四门合并结果。

公式：

```text
Resolved =
  generation_apply
  AND scope_ok
  AND correctness_ok
  AND maintainability_pass
```

最终：

- Gemini 66
- Claude 63
- GPT-5.5 61

### 9.6 `report.json`

聚合：

- leaderboard
- hard/medium
- language
- category
- failure reasons
- pairwise

### 9.7 306 题 agentic 评测

目录：

```text
data/releases/reserves/20260721/evaluations/agentic-three-model-20260725/
```

该评测不复用 `three-model-20260724` 的候选结果。三个模型对 v1 100 题和 reserve
206 题全部重新执行：

1. `agentic-line-edit/v1` generation
2. 单文件 scope
3. 历史环境 correctness replay
4. 隐藏模型身份的维护性盲裁

Generation policy：

- `read`、`find`、`edit`、`finish` JSON actions
- 32 轮
- 16,384 output tokens
- 多对象响应通常选择最后一个有效 action；若 worktree 尚无改动且最后 action 为
  `finish`，优先执行同一响应中的最后一个 `edit`
- 轮次耗尽时固化最后有效 worktree
- 最终 patch 由 `git diff --binary` 生成
- candidate repair 为 false

Gemini 对明确耗尽 16K reasoning/output 预算的任务使用独立 32K 恢复。只有成功恢复
才替换主记录，共恢复 10 道。另有 2 道 `edit→finish` adapter failure 在修正
action 选择后恢复。Sniffy 在 16K/8K output 与 30K/10K context 的多次
受控尝试中均未产生首个 checkpoint，记录为 `generation_api_timeout`。

记录数：

| Artifact | 数量 |
|---|---:|
| generation | 918 |
| correctness | 918 |
| judgments | 918 |
| results | 918 |
| patches | 866 |
| traces | 918 |
| judge raw | 861 |

最终结果：

| 模型 | Generated | Correctness | Maintainability | Resolved | Hard |
|---|---:|---:|---:|---:|---:|
| GPT-5.5 | 306 | 249 | 233 | 198 | 10 |
| Claude Opus 4.8 | 305 | 240 | 221 | 187 | 11 |
| Gemini 3.5 Flash | 255 | 196 | 172 | 157 | 7 |

`package_agentic_evaluation.py` 将 session 产物转换成可移植发布：

- generation 只保留紧凑状态，完整 action/tool history 单独放在 `traces/`
- correctness 统一为单条 task-model schema
- patch、trace 和 judge raw 路径改为相对路径
- session root、repository root、recovery root 和 home 替换为占位符
- `manifest.json` 记录 protocol、policy、记录数和 leaderboard
- `SHA256SUMS` 覆盖发布目录全部文件

Hard：

- GPT-5.5 7/11
- Gemini 6/11
- Claude 5/11

## 10. 中间产物在哪里

中间产物不进入版本控制。

主要 session artifact 名称：

- `release_strict_60.jsonl`
- `release_strict_79.jsonl`
- `release_strict_81.jsonl`
- `release_strict_95.jsonl`
- `release_strict_100.jsonl`
- `quality_combined_dual_v3.jsonl`
- `mining_increment*_candidates.jsonl`
- `mining_increment*_evidence*.jsonl`
- `increment*_judge_*.jsonl`
- `increment*_oracle_*.jsonl`
- `recovery_wave2_*.jsonl`
- `model_eval_v1/`

被忽略的项目目录：

- `data/runs/`
- `data/difficulty/`
- `data/trackA/`
- `data/benchmark_splits/`

这些是实验历史或中间输入，不是 v1 发布接口。

## 11. 复核

```bash
sha256sum -c data/releases/v1/SHA256SUMS
sha256sum -c \
  data/releases/v1/evaluations/three-model-20260714/SHA256SUMS

reviewbench-release \
  --tasks data/releases/v1/tasks.jsonl \
  --evaluations data/releases/v1/oracles.jsonl \
  --target 100 \
  --out /tmp/reviewbench-v1.jsonl \
  --report /tmp/reviewbench-v1-report.json

cmp data/releases/v1/tasks.jsonl /tmp/reviewbench-v1.jsonl
```

## 12. 20260728 strict reserve 数据血缘

目录：

```text
data/releases/reserves/20260728/
```

### 12.1 Mining snapshot

- manifest batches：2,700
- raw candidate records：159,225
- unique candidate IDs：109,619
- new candidates since the 25,535-task prior snapshot：84,084
- prior eligible candidates not included in the first 3,000 tranche：13,647

### 12.2 Quality funnel

- merged quality-eligible tasks：70,939
- priority tranche：3,000
- complete `quality-evidence/v2`：2,581
- GPT-5.6 Sol strict passes：533
- Claude Opus 4.8 judged：533
- dual-model strict passes：280
- semantic-unique before Oracle：268

Both judges use `quality-improvement/v1`; release still requires score at least
4, confidence at least 0.8, intrinsic medium/hard difficulty, maintainability
as the primary goal, no behavior-fix primary objective, and no partial verdict.

### 12.3 Oracle construction

The 268 candidates were split across nine historical-environment lanes. Every
released task requires:

- exact start SHA
- source-only golden
- three stable start runs
- three stable golden runs
- non-empty P2P
- real observable behavior assertions
- real changed-old production-line coverage
- no target tests
- no private-helper/signature/AST/MRO/call-count coupling

Results:

| Lane | Input | Pass |
|---|---:|---:|
| Native | 35 | 33 |
| Java | 42 | 33 |
| Kotlin | 41 | 28 |
| Rust | 32 | 23 |
| TS/JS | 29 | 22 |
| C# | 26 | 14 |
| Scala/Ruby/PHP | 44 | 12 |
| Go/Python | 12 | 11 |
| Other | 7 | 4 |

### 12.4 Audit corrections

The first Native implementation was rejected in full during independent audit:
it compiled `oracle_main.cpp + production_focus.cpp`, copied simplified logic,
and redirected a trivial probe with `#line` so gcov appeared to hit the real
production path. Those 35 records were archived outside the release.

Native was rebuilt from scratch. The final 33 passes compile or instantiate
real repository production code, call real symbols, and report gcov/llvm-cov
from real translation units. A second independent audit found no blocking
issues.

The Other lane was also reaudited for extracted `.behavior.inc` and stub risk;
2 passes without faithful production coverage were withdrawn.

Ten groups of review comments had identical PR, source scope, fix, and behavior
Oracle. One representative is kept per group, excluding 11 redundant tasks.

### 12.5 Final release

- strict tasks：169
- hard：14
- medium：155
- repositories：160
- pull requests：164
- Oracle reject or blocked：88
- semantic duplicates excluded after Oracle：11

Cumulative v1 + 20260721 + 20260728:

- tasks：475
- hard：40
- medium：435
- repositories：410

`manifest.json` hashes all release artifacts. `SHA256SUMS` covers the manifest,
tasks, oracles, provenance, failures, duplicate exclusions, reports, README,
and cumulative preview. No machine-specific absolute paths or credentials are
included.

## 13. 20260808 hard release 血缘

### 13.1 发布关系

`data/releases/hard/20260808/` 是 hard 专项发布，不替换 475 题 general
release。

```text
475 strict general tasks
├── 40 published hard ────────────────┐
└── 435 medium                        │
                                      ├── 414 hard release
739 semantic-unique hard candidates   │
└── 374 new strict hard ──────────────┘
```

374 道新 hard 在冻结前已经排除与既有发布相同的任务 ID 和同一局部 reference
solution。当前唯一 strict universe 是 849 道：414 hard + 435 medium。

### 13.2 新 hard 候选来源

存量池处理：

```text
70,939 quality-eligible
− 3,000 priority tranche
= 67,939 backlog
→ 56,508 complete evidence
→ 5,892 GPT strict pass
→ hard selection
→ 797 candidates
→ exclude 5 published IDs
→ 792 novel IDs
→ full same-file reference-fix semantic dedup
→ 739 Oracle engineering tasks
```

新 hard selection 要求：

- GPT `quality-improvement/v1` strict pass；
- 至少一个独立 judge 给出 intrinsic `hard`；
- 完整 before、local patch、full same-file patch；
- 非 test/docs/generated anchor；
- 维护性是 primary goal；
- 非 partial、style-only 或 behavior-fix primary。

固定 confidence threshold 没有继续作为跨模型 hard 门，因为 GPT 与 Claude 的
self-reported confidence 分布未校准。

### 13.3 Oracle 工程来源

739 道任务按语言拆为：

```text
gopy / scripted / tsjs / csharp / jvm / rust / native
```

每条 lane 的成功记录写入独立 `results.jsonl`。发布收集器
`collect_hard_oracle_passes.py` 统一识别不同 schema 的严格成功：

- `oracle_result=PASS_TO_PASS`
- 或 `status=strict_pass`
- 或 `disposition=accept`

且同时要求：

- start 三次稳定；
- source-only golden 三次稳定；
- `golden_correctness_ok=true`；
- `pass_to_pass > 0`；
- `changed_lines_covered=true`。

最后一次 lane snapshot 有 372 个当前 strict pass。发布 union 又恢复两个此前
已经完整验证、但被后续 flaky 重跑覆盖的 strict-pass 记录，得到 374 个新 hard。
因此 release merge 使用 strongest validated evidence，而不是 last-write-wins。

374 道新 hard 的 lane 来源：

| Lane | Tasks |
|---|---:|
| gopy | 73 |
| native | 65 |
| rust | 71 |
| scripted | 53 |
| jvm | 52 |
| tsjs | 42 |
| csharp | 18 |

### 13.4 Wave 冻结

新 hard 分三波完成评测：

| Wave | Tasks | Task-model pairs |
|---|---:|---:|
| wave-001 | 159 | 477 |
| wave-003 | 190 | 570 |
| wave-004 | 25 | 75 |
| **Total** | **374** | **1,122** |

每个 wave 至少包含：

- `tasks.jsonl`
- `oracles.jsonl`
- 三个模型的 generation results
- 七条 correctness lane JSONL
- calibrated rubric-v2 judgments
- normalized `results.jsonl`
- `report.json`
- generation/correctness/judgment/summary completion markers

`correctness-canonical/` 只放七个权威 lane 文件。工作目录中的重跑前备份、
interruption checkpoint 或 evidence shard 不允许被汇总器读取。

Marker 不是行数完成标志，而是 content attestation：

- generation：task file、逐题 task fingerprint、result JSONL、patch bytes；
- correctness：task、Oracle、generation hash、start revision、candidate patch hash；
- judgment：task fingerprint、generation fingerprint、rubric text、judge
  model、context window、judgment JSONL；
- summary：全部 ready input、normalized results、report、重算 leaderboard/pairwise。

三个 20260808 wave 在 fingerprint 代码加入前已经完成，因此 manifest 中标为
`legacy-continuity`。该模式验证当前 task、patch、correctness、judgment 和
summary 相互一致，并核对 judgment 引用的 patch bytes；它不把事后补写的 hash
描述成原始调用时已经存在的证明。后续新 wave 必须使用 `runtime` marker。

### 13.5 Published hard slice

既有 40 道 hard 从以下两个文件按任务 ID 连接：

- tasks：
  `data/releases/reserves/20260728/cumulative_preview.jsonl`
- calibrated results：
  session 中冻结的 475-task rubric-v2 cumulative results

任务过滤字段：

```text
release_validation.judged_difficulty == "hard"
```

Oracle 从 v1、20260721 reserve 和 20260728 reserve 的三个 `oracles.jsonl`
按 ID 连接。不得依赖物理行号。

### 13.6 四门评测连接

所有 414 道使用相同归一化结果 schema：

```text
task.id + model
→ generation_apply
→ scope_ok
→ correctness_ok
→ maintainability_pass
→ resolved
```

`resolved=true` 当且仅当四门全部通过。最终 1,242 条结果必须满足：

- 414 个唯一 task ID；
- 每题三个模型；
- 1,242 个唯一 `(task_id, model)`；
- task、Oracle、result 的 ID 集一致；
- 无 judge error；
- 无 infra-shaped success fallback。

### 13.7 Claude generation 修正

wave-003 首次通过 8787 aggregate proxy 生成 Claude candidate，产生
95/190 failure。相同 95 道在 4141 direct endpoint 上 fresh-run 后：

```text
94 generated
1 genuine max-round failure
```

修正流程：

1. 保留原 95 个成功 generation；
2. 提取 95 个失败 task ID；
3. 不恢复 exhausted trace，从原始 source fresh-run；
4. 用 94 个成功结果替换 canonical generation；
5. 只重放这 95 个 Claude correctness pair；
6. 保证其余 475 correctness record object-identical；
7. 重新执行 rubric-v2 judgment 和 summary。

普通 `--resume` 会恢复 32 轮已耗尽的 trace，因此不能产生新样本。正常 benchmark
run 保留该 terminal failure，防止只重采样失败项造成 selection bias。只有像
wave-003 aggregate-proxy 这样有独立基础设施证据的批次，才使用
`--restart-failed` 或显式 task ID 做审计 recovery，并把 correction 写入发布血缘。

### 13.8 可移植发布

`scripts/package_hard_release.py` 接受：

- published cumulative tasks/results/oracles；
- 一个或多个已完成 hard wave；
- machine root replacement。

它输出：

```text
data/releases/hard/20260808/
├── README.md
├── tasks.jsonl
├── oracles.jsonl
├── published-hard-results.jsonl
├── result-overrides.jsonl
├── results.jsonl
├── report.json
├── manifest.json
└── SHA256SUMS
```

路径替换：

| Machine root | Release placeholder |
|---|---|
| session root | `${REVIEWBENCH_SESSION_FILES}` |
| repository root | `${REVIEWBENCH_REPOSITORY}` |
| home root | `${HOME}` |

`result-overrides.jsonl` 保存发布时应用的 runner-derived correction，不把
session-only correction 隐藏成不可追溯的 last-write。manifest 同时记录
published source、三个 wave 和 correction overlay 的 content hash。

路径替换递归应用到 nested command、coverage report、artifact path 和 failure log。
打包器在写完后扫描所有发布文件；任何原始 machine root 残留都会使打包失败。

### 13.9 不进入版本控制的内容

以下仍是可重建或过大的 session artifact：

- 287 GB 级 correctness worktree 和 build output；
- Docker image/layer；
- dependency cache；
- raw API response；
- transient lock、PID、interruption log；
- per-attempt coverage binary；
- repository clone cache。

发布包保留复核所需的任务、命令、工具链、行为断言、coverage 摘要、四门结果和
hash，但不假装能够在没有历史环境恢复的全新机器上一键重放所有仓库。
