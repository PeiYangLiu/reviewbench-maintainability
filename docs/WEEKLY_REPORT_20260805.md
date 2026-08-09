# ReviewBench-Maintainability 周报：面向 ICLR 2027

> 历史周报（状态截至 2026-08-05）。414-task hard release 和修正后的模型结果见
> [`PROGRESS_20260808.md`](PROGRESS_20260808.md)。

**周期：2026.07.29 至 2026.08.05**  
**状态截点：2026-08-05 00:35 PDT**

## 一、本周结论

本周完成了 ReviewBench-Maintainability 从“475 道严格任务集”到“可用于论文主实验和效度分析的 benchmark”的关键推进：

1. 完成累计 **475 道任务、3 个模型、1,425 个 task-model pair** 的 agentic 评测汇总。
2. 发现并修复维护性裁判对真实人类补丁的系统性偏差，将 human-golden 通过率从不合理的 28.8% 校准到 **96.6%**。
3. 完成存量 **67,939** 个候选的 evidence 抽取和 GPT-5.6 Sol 严格质量判断。
4. 从存量池提取 **792 道新 hard 候选**，语义去重后形成 **739 道独立 hard Oracle 工程池**。
5. hard Oracle 已确认严格通过 **98 道**；其余任务按语言进入环境恢复、session behavior test 和 changed-old coverage 工程。
6. 将 Oracle 工程从单机迁移到 4 台临时计算节点，解决本机过载和磁盘压力。
7. 当前完整测试为 **131 passed**。

项目已经具备 ICLR 2027 benchmark paper 的主体数据、主结果和关键效度实验；当前主要瓶颈从“候选量”转为“严格 Oracle 工程、旧任务审计和多裁判稳健性”。

---

## 二、拟投稿论文的核心问题

论文拟回答：

> 当前代码模型能否根据真实 code review concern，生成行为保持、范围聚焦且实质提升可维护性的代码修改？

与 bug-fix benchmark 不同，本项目的任务来源和评价目标是：

- 任务来自真实 merged PR 的 reviewer comment，而非 benchmark 作者合成；
- start 与 golden 通常应保持公开行为一致；
- correctness 是必要回归门，但不是主要优化目标；
- maintainability improvement 是主评价维度；
- reference fix 必须真实执行、覆盖 changed-old production line，并且 Oracle 不绑定作者的私有实现。

当前论文的潜在贡献点包括：

1. **真实人类 review signal 数据源**：以 reviewer concern 定义任务，而非从最终 diff 反推任务。
2. **严格可执行的维护性 Oracle**：start/source-only golden 双侧稳定、非空 P2P、changed-old production coverage。
3. **受限 agentic edit protocol**：模型操作真实 start worktree，由 Git 生成补丁，避免 SEARCH/REPLACE 格式失败。
4. **人类 golden 校准的 LLM judge**：用真实合入修法校准维护性裁判，显式分离 correctness 与 maintainability。
5. **多语言、大规模模型比较**：475 道任务、410 个仓库、3 个前沿模型。
6. **Oracle authenticity 与 solution-coupling 审计**：系统检查 toy reimplementation、私有 helper、AST/source-shape 等失效 Oracle。

---

## 三、当前冻结 benchmark

累计任务集：

| 数据版本 | 任务 | Hard | Medium |
|---|---:|---:|---:|
| v1 | 100 | 11 | 89 |
| 20260721 reserve | 206 | 15 | 191 |
| 20260728 reserve | 169 | 14 | 155 |
| **累计** | **475** | **40** | **435** |

累计覆盖：

| 指标 | 数量 |
|---|---:|
| Tasks | 475 |
| Repositories | 410 |
| Task-model pairs | 1,425 |
| Languages | 14+ |
| Hard | 40 |
| Medium | 435 |

每道正式题要求：

- 真实 GitHub merged PR 和 reviewer comment；
- 至少两个不同模型确认 reference fix 是实质维护性提升；
- exact start 和 source-only golden；
- 两侧各 3 次稳定；
- 非空 PASS_TO_PASS；
- 覆盖 changed-old production line；
- Oracle 不检查私有 helper、AST、MRO、调用次数或源码字符串；
- 语义去重。

权威数据目录：

```text
data/releases/v1/
data/releases/reserves/20260721/
data/releases/reserves/20260728/
```

---

## 四、475 题三模型 Agentic 评测

协议：

```text
agentic-line-edit/v1
```

模型通过 `read`、`find`、`edit`、`finish` 操作 exact-start worktree，只允许修改锚点生产文件；最终补丁由 `git diff --binary` 统一生成。

### 4.1 校准后主榜

维护性门采用 human-golden 校准后的 rubric v2。

| Model | Generated | Scope | Correctness | Maintainability | Resolved@1 |
|---|---:|---:|---:|---:|---:|
| **GPT-5.5** | 475 | 471 | 395 | 400 | **338/475 (71.2%)** |
| **Claude Opus 4.8** | 474 | 472 | 378 | 374 | **306/475 (64.4%)** |
| **Gemini 3.5 Flash** | 413 | 411 | 321 | 304 | **270/475 (56.8%)** |

Resolved@1 定义：

```text
generation/apply 成功
AND 单文件 scope 合规
AND exact-start correctness Oracle 通过
AND calibrated maintainability judge 通过
```

### 4.2 配对显著性

基于任务级 McNemar 检验：

| Pair | 分歧胜负 | p-value |
|---|---:|---:|
| Claude vs Gemini | 74 / 38 | 0.00094 |
| Claude vs GPT | 24 / 56 | 0.00053 |
| Gemini vs GPT | 19 / 87 | 7.6e-11 |

475 题全集上三个模型的排序差异均显著。

### 4.3 Hard 子集

| Model | Correctness | Maintainability | Resolved@1 |
|---|---:|---:|---:|
| GPT-5.5 | 30/40 | 30/40 | **22/40 (55%)** |
| Claude Opus 4.8 | 33/40 | 24/40 | **20/40 (50%)** |
| Gemini 3.5 Flash | 26/40 | 19/40 | **14/40 (35%)** |
| Human golden | correctness by construction | **39/40 (97.5%)** | — |

Hard 子集观察：

- human golden 在 hard 上没有明显下降；
- 模型维护性通过率相对全集明显下降；
- GPT vs Gemini 在 hard 上显著；
- GPT vs Claude 在 hard 上不显著，主要原因是当前 hard 只有 40 道。

因此，本周重点扩充 hard 题，而不是继续增加 medium 题。

### 4.4 协议观察

历史 306 题结果显示 agentic edit protocol 对 GPT 和 Claude 有提升，但 Gemini 在多轮工具循环中出现较多 generation failure。

需要注意：

- SEARCH/REPLACE baseline 与当前 agentic 结果使用过不同版本的维护性 rubric；
- 论文最终 protocol ablation 应在相同 rubric v2 下重判后再直接比较。

---

## 五、维护性裁判校准

### 5.1 发现的问题

初始 rubric v1 同时要求：

- `behavior_preserved=true`
- `focused=true`
- addressed=yes
- quality>=4

但 correctness 已由真实 Oracle 执行。让 LLM 再从 diff 文本猜测行为是否保持，既重复又不可靠；同时真实人类 commit 常包含同文件的相关伴随修改，容易被 `focused=false` 系统性惩罚。

另一个独立问题是 golden 提取方式：

- `quality_evidence.patch` 是 comment-local hunk 窗口，不是完整补丁；
- 389/475 道题会丢 hunk；
- 中位数丢 3 个，最多丢 58 个；
- 被丢的 hunk 常包含新 helper 定义或调用点，导致裁判看到悬空引用。

### 5.2 2×2 校准实验

| Human golden 输入 | Rubric v1 | Rubric v2 |
|---|---:|---:|
| review-local hunk | 137/475 = 28.8% | 347/470 = 73.8% |
| **完整同文件 golden diff** | 194/475 = 40.8% | **456/472 = 96.6%** |

正确的人类天花板配置是：

```text
完整同文件 human golden diff
+ rubric v2
```

### 5.3 Rubric v2

Rubric v2 的原则：

1. 行为保持交给 executable correctness Oracle，不由 LLM 从文本猜测。
2. 不因真实 commit 包含额外改动而自动拒绝。
3. 只有 concern 未解决，或引入同等严重的新维护债时才拒绝。
4. 仍要求 addressed=yes、quality>=4、无 comparable new debt。

校准结果：

- Human golden：96.6%
- GPT-5.5：约 84% maintainability pass（新 169 题）
- Claude：约 80%
- Gemini：约 72%

该结果恢复了合理的人机关系，同时保留模型间区分度。

### 5.4 置信度门问题

本周还发现固定 `confidence >= 0.8` 不是跨模型校准的：

| 指标 | GPT | Claude |
|---|---:|---:|
| 平均 confidence | 0.894 | 0.719 |
| GPT-hard 配对中 confidence<0.8 | 0 | 236/347 |

在 GPT 判 hard 的配对样本中：

- Claude 也判 hard：169；
- 加上 strict quality 和 confidence 门后只剩 46；
- 忽略未校准 confidence 门后可保留 118。

因此，论文中将 difficulty、quality pass 和 self-reported confidence 分开报告；不能把 46 直接解释为“只有 46 道真正 hard”。

---

## 六、存量池处理

原存量 quality-eligible 池：

```text
70,939
− 已处理优先 3,000
= 剩余 67,939
```

本周完成：

| 阶段 | 数量 |
|---|---:|
| Backlog candidates | 67,939 |
| Evidence 完成 | 67,939 |
| Evidence ready + complete | 56,508 |
| GPT strict pass | 5,892 |
| Claude 有效复核 | 2,945 |
| 已确认 dual strict | 869 |

Claude 复核中途遇到模型访问和网关输出截断：

- 已保留 2,945 条有效判决；
- API error 和截断结果未当作真实 reject；
- 后续按当前研究优先级不再补判全部 medium 候选；
- 先抽取任一模型判 hard 的候选进入 Oracle。

---

## 七、Hard 候选扩充

候选口径：

| Pool | 数量 | 定义 |
|---|---:|---|
| 任一模型 verdict=hard | 2,544 | 不要求质量通过 |
| GPT strict + 任一模型 hard | 797 | 当前工作池 |
| 至少一个模型 strict+hard | 762 | 更保守 |
| 双模型 strict+hard | 46 | 受未校准 confidence 强烈影响 |

当前采用：

```text
GPT strict pass
AND 任一模型判断 hard
```

流程：

```text
797 hard candidates
→ 排除 5 个已发布 ID
→ 792 novel
→ 完整同文件 reference-fix 语义去重
→ 739 independent hard Oracle tasks
```

739 道语言分布：

| Lane | Tasks |
|---|---:|
| JVM | 168 |
| Go/Python | 150 |
| Native C/C++ | 132 |
| Rust | 115 |
| PHP/Ruby/Other | 81 |
| TypeScript/JavaScript | 58 |
| C# | 35 |

---

## 八、Hard Oracle 工程

截至状态截点，原始主机合并检查点已确认严格通过 **98 道**：

| Lane | 严格通过 |
|---|---:|
| Go/Python | 42 |
| Scripted | 17 |
| Native | 13 |
| TypeScript/JavaScript | 8 |
| C# | 8 |
| JVM | 6 |
| Rust | 4 |
| **Total** | **98** |

其余状态：

| 状态 | 数量 |
|---|---:|
| PASS_TO_PASS | 98 |
| Recoverable | 549 |
| 尚未处理 | 51 |
| Structural reject | 36 |
| Blocked | 5 |

“Recoverable”不等于任务失败，主要包括：

- 历史工具链和依赖恢复；
- exact-start baseline 暂未通过；
- 无现成测试，需要 session behavior test；
- P2P 为空，需要选择或编写公开行为断言；
- 现有测试未覆盖 changed-old line；
- 编译型项目需要专用 Docker 和 incremental build。

严格通过仍要求：

```text
exact start 3x stable
AND source-only golden 3x stable
AND nonempty P2P
AND real changed-old production coverage
AND implementation-independent behavior Oracle
```

### 8.1 分布式迁移

本周单机同时运行多语言 Oracle 时，负载峰值超过 1,200，产生 timeout、cache lock 和资源争用风险。随后将工作迁移到临时计算节点：

- Go/Python + Scripted；
- TS/JS + C#；
- Rust；
- Native；
- JVM 原节点负载异常，已迁移到健康节点。

临时节点单机配置约为：

- 96 CPU；
- 约 1.5 TB RAM；
- 2.9–15.7 TB `/data` 可用空间；
- Docker、sudo 和 GitHub 网络可用。

迁移后：

- 原始主机 hard Oracle 容器清零；
- 临时节点负载约 12–30；
- lane state、精确 repo cache、Docker image 和工具链按需迁移；
- 远端结果使用独立文件名回传，通过 task ID 和 evidence quality 合并，避免旧快照覆盖本机新结果。

---

## 九、Mining 状态

原 2,700 batch manifest 已完成：

| 指标 | 数量 |
|---|---:|
| Completed batches | 2,700/2,700 |
| Unique candidates | 115,467 |

扩容 manifest：

| 指标 | 数量 |
|---|---:|
| Planned batches | 5,220 |
| Completed before pause | 669 |
| Raw records | 37,545 |
| Unique candidates | 32,825 |

扩容覆盖：

- 现有 12 门语言补 2019–2021；
- 新增 Swift、Dart、Elixir、C、Shell、Haskell、Lua、Clojure、Erlang、R；
- Mining 当前按优先级暂停，资源转向 hard Oracle。

本周修复的 mining 工程问题：

1. resume 时在检查 done marker 前重复扫描全部候选，形成 O(n²) 开销；
2. manifest 静默覆盖命令行 workers 和 batch size；
3. 250-PR 大 batch 在一次 rate-limit guard 后耗尽 GitHub core quota；
4. 单个超大 PR 的 HTTP 422 会拖垮整个 batch 并无限重试；
5. 修复后扩容阶段曾达到 0 batch failure。

---

## 十、Oracle 与评测审计

本周发现并修复/标记：

- gopy schema 缺少 `oracle_replayed`，曾导致 Python/Go 被全部误算为 correctness fail；
- dpnp 使用 `sh -lc source`，导致 3 条空日志 exit 127 假失败；
- mqt-syrec Oracle 漏传 PLA 参数，baseline 自身 abort；
- Ruby DFE-Digital 因 Redis sidecar 未连接产生假失败；
- rawls 的 worktree `.git` 指向迁移前路径；
- LibreSign 镜像缺 `sockets`；
- Amazon Pay 挂载路径与 WordPress bootstrap 不一致；
- OpossumUI、Docusaurus、DataDog、Catroid、VotingWorks 等环境或命令偏差；
- kielipankki Oracle 断言 golden 的 dict 返回类型，在 exact start 上也失败，已标为 `oracle_defect`。

这些问题表明：

> 多模型同时失败是环境/Oracle 审计的高价值触发信号，不能直接解释为模型能力不足。

旧 306 题中仍有 28 道“三模型同时失败”需要系统重放审计，是论文前必须完成的风险项。

---

## 十一、工程与代码状态

本周新增/增强：

- human-golden baseline 构造；
- calibrated maintainability rubric v2；
- cumulative leaderboard merge；
- 多 schema correctness normalizer；
- hard pool freeze 和语义去重；
- hard Oracle lane collection/merge；
- model identity verification；
- GPT/Claude chat fallback 与 token truncation detection；
- continuous mining supervisor；
- backlog evidence→GPT→Claude pipeline；
- 分布式 Oracle 迁移和结果合并。

测试状态：

```text
131 passed
```

当前工作树仍含大量未提交代码、文档和数据，不应在论文 artifact 截止前继续长期保持单一大工作树。

---

## 十二、ICLR 2027 论文就绪度

### 已具备

- 475 道严格任务；
- 3 模型完整 agentic leaderboard；
- human-golden ceiling；
- calibrated judge rubric；
- 真实 reviewer-signal 构造方法；
- 多语言 strict Oracle；
- protocol failure 和 correctness failure 的分离；
- hard 子集与统计检验；
- 1,385 条历史盲标可用于数据构造效度分析。

### 仍需完成

1. **Hard Oracle 扩充完成**：将 739 道工程池尽可能转化为稳定可执行任务。
2. **新 hard 三模型全量评测**：Oracle pass 后统一跑 Claude/Gemini/GPT。
3. **旧 306 题 correctness 审计**：重放 28 道三模型同时失败任务。
4. **多裁判稳健性**：至少加入一个非 Claude judge，报告 majority/unanimous/leave-own-family-out。
5. **负对照**：reverse golden、无关 patch、style-only patch，测量 judge FPR/FNR。
6. **统一 rubric 的 protocol ablation**：SEARCH/REPLACE 与 agentic 都用 v2 重判。
7. **可复现 artifact**：容器、缓存、runner 和 task-level provenance。
8. **污染分析**：按 PR 时间和模型训练窗口分层。
9. **置信区间与统计功效**：尤其是 hard 子集。

### 当前论文主结论的安全表述

可以支持：

- 前沿模型在真实维护性 review task 上仍明显低于 human golden；
- correctness 与 maintainability 是相互独立的失败门；
- agentic edit protocol 能减少补丁格式失败，但工具循环稳定性因模型而异；
- hard task 上的人机差距扩大；
- 严格 Oracle authenticity 审计会显著改变模型评测结论。

暂时不应声称：

- GPT 在 hard 上显著优于 Claude；
- 当前 40 道 hard 足以稳定排名所有模型；
- LLM judge 在未校准时可直接替代人类；
- 739 道 hard 候选均可端到端评测。

---

## 十三、下周计划

按优先级：

1. 继续分布式 hard Oracle recovery，定期合并远端结果。
2. 对 Oracle pass 任务冻结任务/Oracle manifest 和 SHA-256。
3. 启动 Claude Opus 4.8、Gemini 3.5 Flash、GPT-5.5 三模型 agentic generation。
4. 对三模型候选重放相同 Oracle 并执行 rubric v2 维护性判断。
5. 生成扩充 hard leaderboard、置信区间和 McNemar 检验。
6. 重放旧 306 题的 28 道可疑 correctness 失败。
7. 完成 negative controls 和 multi-judge 实验。
8. 开始论文 Methods、Dataset Construction、Evaluation Protocol 和 Validity 章节初稿。
9. 将当前大工作树拆分提交，冻结 paper artifact 版本。

---

## 十四、关键产物路径

正式数据：

```text
data/releases/v1/
data/releases/reserves/20260721/
data/releases/reserves/20260728/
```

475 题校准后评测：

```text
/data/v-peiyangliu/
  reviewbench-session-9a9f50f0-cb30-462e-942e-1e9def267f6d/
  artifacts/rubric_v2_20260730/
```

Human golden 校准：

```text
.../artifacts/golden_ceiling_20260730/
```

存量池：

```text
.../backlog_20260731/
```

Hard Oracle 工程：

```text
.../hard_oracle_20260804/
```

其中：

- `tasks.jsonl`：739 道语义唯一 hard Oracle 输入；
- `semantic_exclusions.json`：53 条排除记录；
- `lane_inventory.json`：语言分线；
- `model_endpoints.json`：已验证三模型端点；
- 各 lane 的 `results.jsonl`、`checkpoint.json` 和 task artifacts：Oracle 进度与证据。
