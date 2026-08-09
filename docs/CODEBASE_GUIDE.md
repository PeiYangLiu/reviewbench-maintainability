# ReviewBench-Maintainability 代码地图

更新时间：2026-08-08

本文说明当前代码的职责边界、权威入口、输入输出、测试覆盖和历史脚本关系。数据血缘见 `docs/DATA_PROVENANCE.md`，当前里程碑和未完成事项见 `docs/PROGRESS_20260808.md`。

## 1. 权威入口

当前代码按三层组织：

1. `src/reviewbench_maintainability/`：可安装、可复用的核心库和 CLI。
2. `scripts/`：环境相关、实验相关或批处理脚本。
3. `data/releases/`：经过 manifest、完整性和可移植性检查的冻结数据。

最终任务不从旧 `data/trackA/`、`data/difficulty/` 或任意
`data/runs/` 文件直接读取。权威入口是：

| 用途 | 权威文件 |
|---|---|
| 100 道任务 | `data/releases/v1/tasks.jsonl` |
| 100 条 oracle | `data/releases/v1/oracles.jsonl` |
| 任务级来源索引 | `data/releases/v1/provenance.jsonl` |
| 发布统计 | `data/releases/v1/report.json` |
| 发布口径 | `data/releases/v1/manifest.json` |
| 三模型评测 | `data/releases/v1/evaluations/three-model-20260714/` |
| 475 题 general preview | `data/releases/reserves/20260728/cumulative_preview.jsonl` |
| 414 道 hard | `data/releases/hard/20260808/tasks.jsonl` |
| 414 条 hard Oracle | `data/releases/hard/20260808/oracles.jsonl` |
| 1,242 条 hard 评测 | `data/releases/hard/20260808/results.jsonl` |
| hard 总榜 | `data/releases/hard/20260808/report.json` |
| hard 发布口径 | `data/releases/hard/20260808/manifest.json` |

## 2. 核心库

### 2.1 `build_dataset.py`

职责：从 GitHub PR、review comment、commit 和 thread 信号构造原始候选任务。

主要输出字段：

- `id`
- `repo`、`pr_number`、comment URL
- `task_instruction`
- `start_sha`、`target_sha`
- `locality`
- `addressed_evidence`
- `correctness_oracle_proxy`

CLI：`reviewbench-build`

### 2.2 `curate_pool.py`

职责：把多个候选 JSONL 合并、按 ID 去重、过滤明显不合格锚点并排序。

硬过滤：

- test anchor
- docs/example anchor
- generated/vendor anchor
- answer leakage
- 缺 commit 或 locality
- 不在指定语言、bucket 或难度范围

排序信号：

- hard 优先
- 已有严格人工标注优先
- PR 有测试改动优先
- extract、split、deduplicate、decouple 等实质性提示优先
- naming、dead code、机械 rename 等降权

CLI：`reviewbench-curate`

测试：`tests/test_curate_pool.py`

### 2.3 `merge_candidates.py`

职责：合并一个或多个 mining run 的 `candidates.jsonl`，按任务 ID 去重并固定排序。

CLI：`reviewbench-merge`

测试：`tests/test_merge_candidates.py`

### 2.4 `evidence.py`

职责：从精确 commit blob 构造 quality judge 所需证据。

每条完整证据包含：

- `before`：start revision 锚点附近源码
- `patch`：锚点局部 reference hunk
- `full_patch`：完整同文件 start-to-target diff
- `patch_complete`
- `patch_truncated`
- `status`
- `version=reviewbench-quality-evidence/v2`

关键实现：

- 两点 blob diff，不使用可能受 rebase 影响的 GitHub 三点 compare
- commit-only git cache
- 局部 hunk 聚焦
- 局部 patch 与完整同文件 patch 分开保存
- 多线程提取
- 原子 checkpoint 和 `--resume`

CLI：`reviewbench-evidence`

测试：`tests/test_evidence.py`

### 2.5 `quality_gate.py`

职责：执行 `quality-improvement/v1` 严格质量门。

单个判断通过必须同时满足：

- `decision=pass`
- `primary_kind=maintainability`
- evidence 充分
- 原代码有明确 code smell
- golden 有实质改善
- 修法可归因
- 不是 style-only
- 主要目标不是行为变化
- 没有明显质量回退
- 质量分至少 4
- 本质难度 medium 或 hard
- confidence 至少 0.8

聚合通过要求：

- 至少两个不同模型
- 所有 judgment schema 有效
- 所有模型一致严格通过

支持两种输入：

- 在线调用 `/responses`
- 导入外部 JSONL judgment

CLI：`reviewbench-quality`

测试：`tests/test_quality_gate.py`

### 2.6 `recovery.py`

职责：把通过质量门但 oracle 失败的任务写入持久 recovery queue。

主要状态：

- environment
- baseline
- no PASS_TO_PASS
- coverage
- missing evaluation
- structural ineligible

队列按可回收性排序。新 recovery 结果可以覆盖旧失败，但结构性 test anchor 不会伪装成环境问题。

CLI：`reviewbench-recovery`

测试：`tests/test_recovery.py`

### 2.7 `release.py`

职责：最终发布门和多样性选择。

每题必须满足：

- quality gate passed
- 两个 judge 的保守难度至少 medium
- golden correctness
- PASS_TO_PASS 至少 1
- changed-line coverage

选择规则：

- hard 优先
- 最低质量分优先
- 仓库轮转
- 每仓库最多 5 道
- repo、PR、path、归一化局部 patch 的语义指纹去重

当 `selected != target` 时进程返回非零。v1 用 `--target 100` 返回 0。

CLI：`reviewbench-release`

测试：`tests/test_release.py`

### 2.8 既有通用模块

| 模块 | 作用 |
|---|---|
| `github_client.py` | GitHub API 与 snapshot 访问 |
| `heuristics.py` | comment 分类和基础规则 |
| `models.py` | 候选数据结构 |
| `score_patch.py` | 本地静态质量评分 CLI |
| `static_metrics.py` | complexity、duplication、dead-code 相关指标 |
| `evaluate.py` | 面向外部 patch 和统一 oracle config 的通用评测器 |

`evaluate.py` 不是本次 100 题异构历史环境评测的唯一执行器。v1 的 oracle 包含 Go、Python、TypeScript、PHP、Java、Rust 和 native 项目，部分任务需要专用 Docker、sidecar 或 session-only 行为 harness。

## 3. Mining 与证据脚本

### 3.1 `mine_scale.py`

职责：运行可恢复的大规模 GitHub mining batch。

新增能力：

- `--resume-manifest`
- `--query-filter`
- 恢复时保留原 batch index
- serial 或并发 worker
- rate-limit 安全检查
- 每 batch 独立目录和 done marker

当前持续进程使用：

```text
data/runs/scale-big-20260615
```

该目录被 `.gitignore` 忽略，不是发布数据。

测试：`tests/test_mine_scale.py`

### 3.2 `prefilter_candidates.py`

职责：按 resolved thread、region touched、test changes、answer leakage 等信号分桶。

当前机器人过滤只使用明确自动化账号模式，避免把正常 reviewer 误判为 bot。

### 3.3 `enrich_cases.py`

职责：为旧渲染/人工审阅 case 补 before window 和局部 fix patch。

现在复用 `evidence.py` 的：

- `two_dot_file_diff`
- `focus_patch`

它仍服务旧 case renderer，不是最终 v1 批量 evidence 的权威入口。

## 4. Oracle 与环境脚本

### 4.1 `trackA_filter.py`

Python 默认 harness。

当前处理：

- 通过源码路径向上寻找真实 Python project root
- 读取 `pyproject.toml` build requirements
- 尝试 declared test/dev extras
- 固定 Python 3.12 通用环境
- `setuptools<81`
- fetch tags，支持 `setuptools_scm`
- 不用依赖 resolver 覆盖仓库自带 pytest
- 为新增测试缺失的 PR 查找已有相关测试
- 测试文件隔离运行，避免 fixture 污染
- coverage parallel mode 后 combine
- 识别 `testing/`

测试：`tests/test_tracka_filter.py`

### 4.2 `trackA_go.py`

Go 默认 harness。

当前处理：

- 从源码路径向上找最近 `go.mod`
- host 无 Go 时使用 Docker fallback
- 持久 module/build cache
- 精确 source-only golden paths
- PASS_TO_PASS
- changed-old-line coverage

测试：`tests/test_tracka_go.py`

### 4.3 `validate_oracles.py`

职责：对 quality-gated Go/Python 任务运行严格 oracle。

要求：

- start 3 次稳定
- source-only golden 3 次稳定
- 非空 P2P
- changed-old-line coverage

输出 schema：`reviewbench-oracle/v1-stable-covered`

测试：`tests/test_validate_oracles.py`

### 4.4 `docker_harness.py`、`evaluate_docker.py`

职责：为 scikit-learn、SciPy 等编译型项目提供容器环境。

当前特性：

- submodule 初始化
- Meson/editable rebuild
- localized source-only golden
- 容器内 P2P
- 模型仍受 scope 限制

### 4.5 `evaluate_e2e.py`

历史端到端原型，门顺序为：

1. apply
2. correctness
3. scope
4. maintainability judge

当前修复包括：

- golden 写新文件前创建父目录
- golden 豁免模型 scope 限制
- source-only golden
- Go/Python 语言路由
- flaky-aware baseline
- stricter `yes + quality>=4`

最终三模型 v1 评测的数据由后述 release evaluation 脚本统一整理。

## 5. 三模型评测脚本

### 5.1 `generate_release_patches.py`

职责：用冻结的 non-agentic SEARCH/REPLACE 基线从最终任务生成模型补丁。

关键约束：

- 只给模型 reviewer concern 和 start source
- 不给 golden、target、quality judgment 或 oracle 结果
- 精确 SEARCH/REPLACE
- 最多 3 次协议重试
- 支持 Claude/Gemini chat API
- 支持 GPT-5.5 responses API
- Copilot token 401/403 自动刷新
- 超大文件按锚点截取
- 原子 checkpoint 和 `--resume`

测试：`tests/test_generate_release_patches.py`

### 5.2 `generate_agentic_patches.py`

职责：让模型通过受限 line-edit tools 操作当前文件，再由 Git 生成最终 patch。

协议：`agentic-line-edit/v1`。

每轮 action：

- `read(path, start_line, end_line)`
- `find(path, query)`
- `edit(edits=[{start_line, end_line, content}])`
- `finish`

关键约束：

- 所有读取都固定在任务的 `start_sha`，锚点文件读取当前编辑状态
- 只有锚点 production file 可编辑，读取其他文件不授予写权限
- 行号为 1-based，`end_line=start_line-1` 表示插入
- 拒绝路径穿越、跨文件 edit、重叠范围和同一位置的多个插入
- 模型不输出 unified diff；host 在临时 repository 提交 before 后运行
  `git diff --binary`
- 默认最多 32 轮
- 每题保存完整 action/tool trace
- `--resume` 会复用成功结果，并审计历史 trace 是否符合当前 edit 约束
- 未完成任务可恢复当前文件、tool transcript 和已用轮数，从下一轮继续，不重新采样
- 含失效跨文件 action 的旧 trace 不续接，而是按修正后的协议从头运行
- 轮次预算耗尽但已有有效编辑时，host 直接用最后 worktree 生成 patch；`finish`
  只用于提前停止，不是候选有效性的额外格式门

测试：`tests/test_generate_agentic_patches.py`

### 5.3 `judge_release_patches.py`

职责：对候选 diff 做隐藏模型身份的统一维护性判断。

通过要求：

- `addressed=yes`
- `quality_1_to_5>=4`
- `behavior_preserved=true`
- `focused=true`

候选按设计不能改测试，因此裁判不因缺少测试文件改动而拒绝生产代码修法。测试由独立 correctness oracle 负责。

测试：`tests/test_judge_release_patches.py`

### 5.4 `summarize_release_evaluation.py`

职责：合并：

- generation
- scope
- heterogeneous correctness results
- blind judgments

输出：

- 300 条 task-model 结果
- 总榜
- hard/medium
- language
- category
- pairwise wins

异构 correctness 执行器历史上产生过多种 schema。该脚本把 flat、nested、mapping 和 direct-v1 格式统一成：

- `patch_apply`
- `correctness_ok`
- `pass_to_pass_total`
- `pass_to_pass_passed`
- `failed_tests`
- `oracle_replayed`

测试：`tests/test_summarize_release_evaluation.py`

### 5.5 `attach_evaluation_results.py`

职责：把 `results.jsonl` 的归一化四门结果写回 renderer 使用的 review items。

关键约束：

- 按 `task.id + model` 连接，不依赖物理行号
- 要求 items 与 results 的任务集合完全一致
- 整体替换旧 `model_results`，避免 agentic 页面混入 non-agentic baseline
- 保留 generation protocol、失败门和维护性裁判理由

测试：`tests/test_attach_evaluation_results.py`

### 5.6 `package_agentic_evaluation.py`

职责：把 session 中的完整 agentic 评测打包为可发布目录。

输出包括：

- portable `generation.jsonl`
- 统一 schema 的 `correctness.jsonl`
- `judgments.jsonl`
- `results.jsonl` 与 `report.json`
- 每模型 patch、agent trace 和 judge raw output
- `manifest.json`
- 全文件 `SHA256SUMS`

脚本只保留相对 artifact 路径，不把本机 worktree、cache 或绝对路径写入发布。

测试：`tests/test_package_agentic_evaluation.py`

### 5.7 `package_hard_release.py`

职责：把已发布 hard slice 和多个新 hard wave 合并成单一可移植发布。

输入：

- 475 题累计 task metadata；
- calibrated cumulative results；
- v1 和两个 reserve 的 Oracle；
- 一个或多个已经完成 generation/correctness/judgment/summary 的 hard wave。

硬校验：

- task ID 全局唯一；
- Oracle ID 与 task ID 集完全一致；
- 每题恰好三个模型结果；
- published 与新 wave 不重叠；
- 模型集合固定；
- 所有 machine-specific root 都被占位符替换。

输出：

- `tasks.jsonl`
- `oracles.jsonl`
- `results.jsonl`
- `published-hard-results.jsonl`
- `result-overrides.jsonl`
- `report.json`
- `README.md`
- `manifest.json`
- `SHA256SUMS`

测试：`tests/test_package_hard_release.py`

### 5.8 Hard-wave orchestration

`run_hard_wave_generation.sh`：

- Claude 使用已验证的 direct chat endpoint；
- Claude 与 GPT 并行，Gemini 在共享 direct endpoint 上顺序执行；
- 默认 `--resume` 保留成功和失败终态，不做 success-only resampling；
- 只有审计确认的基础设施失败才通过
  `REVIEWBENCH_RESTART_FAILED=1` 或显式 task ID fresh-retry；
- endpoint 和 worker 数可以通过环境变量覆盖。

`run_hard_wave_summary_when_ready.sh`：

- 只接受与当前文件 hash 一致的 generation、correctness 和 judgment
  completion marker；
- 调用 `validate_hard_wave.py ready` 检查 task-model key 集和 artifact identity；
- 支持轮询间隔和 timeout。

`validate_hard_wave.py correctness`：

- 从 `oracles.jsonl` 自动推导 task → lane；
- 验证七条 lane 的精确 `(task_id, model)` 集；
- 核对 correctness 的 `start_sha` 和 candidate patch hash；
- 生成只含七个权威 JSONL 的 `correctness-canonical/`；
- 把 generation hash 写入 correctness marker，防止 generation 重跑后误用旧结果。

`validate_hard_wave.py legacy-attest/summary`：

- generation 绑定 canonical task fingerprint 和 patch bytes；
- judgment 绑定同一 task/generation fingerprint、rubric text、judge model 和
  context window；
- summary 重算 resolved gate conjunction、leaderboard 和 pairwise；
- `summary.complete.json` 绑定全部上游 marker 与最终 results/report。

历史 wave 使用显式 `legacy-attest`：它证明当前冻结 artifact 之间连续一致，但不
宣称能事后证明原始 API 调用当时的 prompt/patch bytes。新 wave 由 runtime
marker 在原始执行时写 fingerprint。

## 6. 测试

当前完整测试为 **158 项，分布在 28 个测试文件**。

| 测试文件 | 覆盖面 |
|---|---|
| `test_build_dataset.py` | GitHub 候选构造 |
| `test_heuristics.py` | comment 分类 |
| `test_static_metrics.py` | 静态质量指标 |
| `test_evaluate.py` | 通用 patch evaluator |
| `test_curate_pool.py` | 候选过滤、排序、去重 |
| `test_evidence.py` | blob diff、局部 hunk、证据完整性 |
| `test_quality_gate.py` | verdict schema 和双模型聚合 |
| `test_recovery.py` | recovery 分类和覆盖 |
| `test_release.py` | 最终门、P2P、coverage、语义去重 |
| `test_tracka_filter.py` | Python 环境、项目根、coverage |
| `test_tracka_go.py` | Go module、Docker、coverage |
| `test_validate_oracles.py` | 三次稳定 oracle |
| `test_generate_release_patches.py` | source excerpt、SEARCH/REPLACE、patch |
| `test_generate_agentic_patches.py` | JSON action、line edit、trace resume、Git diff |
| `test_judge_release_patches.py` | 盲裁 schema 和 prompt |
| `test_summarize_release_evaluation.py` | 异构 correctness schema 和排行榜 |
| `test_attach_evaluation_results.py` | 评测结果与 review items 的严格连接 |
| `test_package_agentic_evaluation.py` | patch/trace 可移植路径与发布复制 |
| `test_package_hard_release.py` | hard task/Oracle/result 合并、路径替换、manifest 和 checksums |
| `test_validate_hard_wave.py` | generation/correctness/judgment marker、精确 pair 集和 stale artifact 拒绝 |

运行：

```bash
python -m pytest -q
git diff --check
```

## 7. 数据放置规则

`.gitignore` 默认忽略整个 `data/`，只放行冻结发布：

```text
data/releases/v1/**
data/releases/reserves/20260721/**
data/releases/reserves/20260728/**
data/releases/hard/20260808/**
```

项目中允许保留：

- 最终任务
- 最终 oracle metadata
- task provenance
- 最终评测结果
- 最终模型补丁
- manifest、README 和 hash

项目中不保留：

- mining batch
- git cache
- worktree
- Docker 临时文件
- 环境目录
- 原始 API response
- 中间 judgment shard
- 临时日志

这些中间产物放在 Copilot session files 或被忽略的 `data/runs/`。

## 8. 当前限制

1. correctness 环境高度异构，compact release 不包含数百 GB 的 worktree、
   container layer 和 dependency cache。
2. `oracles.jsonl` 保存命令、工具链、行为断言和 recovery 元数据，但恢复历史
   环境仍可能需要专用镜像或 sidecar。
3. 模型 solver 当前只改锚点 production file，多文件 modularity/API 题可能
   被系统性低估。
4. 维护性统一裁判是 Claude Opus 4.8，Claude 候选存在无法完全排除的自裁偏差。
5. 新 374 hard 与既有 40 hard 使用不同的历史 difficulty 合并规则；manifest
   和进展文档显式保留该差异。
6. hard release 是严格可执行子集，不是全部真实 review comment 的随机样本。
