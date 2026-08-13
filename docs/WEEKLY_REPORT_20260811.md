# ReviewBench-Maintainability 周报：414-hard 最新模型评测

**周期：2026.08.06 至 2026.08.11**  
**状态截点：2026-08-11 17:40 PDT**

> 本文记录当前仍在运行的最新模型实验。所有未完成数字均明确标为
> preliminary；在 inference、correctness 和 legacy replay 全部闭合前，不发布
> 新模型 Final Resolved `/414` 排名。

## 一、本周结论

本周完成了 ReviewBench-Maintainability 从 414-hard 冻结基准到最新一代模型
大规模评测的主体工程：

1. 冻结并持续验证 **414 道 strict hard 任务**，覆盖 **340 个仓库、385 个 PR**。
2. 保留 GPT-5.5、Claude Opus 4.8、Gemini 3.5 Flash 的完整 414-hard 基线，
   Final Resolved 分别为 **212、177、143**。
3. 完成 11 个最新模型的首轮 414-task generation：
   - 闭源：Claude Opus/Sonnet 5、GPT-5.6 Luna/Sol/Terra、Gemini 3.6 Flash；
   - 开源：Qwen3.6-35B、Qwen3-Coder-Next、Devstral-2、GPT-OSS-120B、
     MiniMax-M2.1。
4. Gemini 3.6 的旧 131/414 结果被证明是网关/schema 失效结果并完全排除；
   native-tools v2 的有效结果提升到 **375/414 generated**。
5. 对开源模型的 75 个基础设施/harness 失败执行精确 ID 重跑，不重采样任何
   成功样本或真实模型失败；当前 canonical generation 已恢复：
   - Qwen3.6-35B：314 → **345**
   - Qwen3-Coder-Next：305 → **327**
   - Devstral-2：380 → **387**
   - MiniMax-M2.1：374 → **378**
6. 完成 11 模型 rubric-v2 盲裁及增量 overlay；全部 base judge error 已清零。
7. Correctness 七条 lane 已全面启动；open-five Rust 已写满 **355/355**，
   其中 19 条基础设施记录已隔离待恢复；
   hosted-five 已完成 Go/Python、Scripted、TS/JS，Gemini 已完成除 Rust 外的
   六条 modern lane。
8. 为 40 个 published legacy hard 题新增 fail-closed replay adapter：
   - 20260728 的 14 题中 **7 题可运行、7 题缺历史资产**；
   - 11 模型的可运行 slice 已完成，共 **79 条 raw/normalized 结果**。
9. 修复并固化多类会污染 benchmark 分数的基础设施问题：CRLF patch、
   root-owned Rust scratch、历史 Go/Python runtime、native plan env、
   Git submodule、Conan、IREE CMake 检查和 judge schema repair。

当前最大的剩余瓶颈是 **Kimi K2.5 inference 吞吐**，其次是 Qwen27 的
pause/requeue checkpoint 稳定性和 33 道尚未恢复 replay contract 的 legacy 题。

---

## 二、当前冻结 benchmark

项目当前有两套互补数据：

| 数据 | Hard | Medium | 说明 |
|---|---:|---:|---|
| General release | 40 | 435 | 既有 475 道 strict 任务 |
| 20260808 hard release 新增 | 374 | 0 | 新 hard Oracle |
| **唯一 strict tasks** | **414 hard** | **435 medium** | **849 total** |

414-hard release：

| 指标 | 数量 |
|---|---:|
| Tasks | **414** |
| Repositories | **340** |
| Pull requests | **385** |
| Languages | **16** |
| Categories | **8** |
| Generation protocol | `agentic-line-edit/v1` |
| Maximum tool rounds | 32 |

权威目录：

```text
data/releases/hard/20260808/
```

每道题均要求：

- 精确 `start_sha`；
- source-only golden；
- start 与 golden 各 3 次稳定；
- 非空 PASS_TO_PASS；
- changed-old production line coverage；
- 语义去重；
- 行为保持、维护性为主要目标。

---

## 三、冻结的 414-hard 基线

Final Resolved 定义：

```text
generation_apply
AND scope_ok
AND correctness_ok
AND maintainability_pass
```

| Model | Generation | Scope | Correctness | Maintainability | Final Resolved |
|---|---:|---:|---:|---:|---:|
| **GPT-5.5** | 413 | 392 | 341 | 263 | **212/414** |
| **Claude Opus 4.8** | 411 | 401 | 357 | 216 | **177/414** |
| **Gemini 3.5 Flash** | 340 | 323 | 242 | 179 | **143/414** |

这三组结果已经冻结，可以作为最新模型的正式比较基线。最新模型在四门全部完成前
只报告单门进展，不提前拼接 Final Resolved。

---

## 四、最新模型 Generation

### 4.1 已形成 canonical 414 结果的模型

| Model | Generated | Failed | 状态 |
|---|---:|---:|---|
| Claude Sonnet 5 | **414** | 0 | complete |
| Claude Opus 5 | **411** | 3 | complete |
| GPT-5.6 Luna | **414** | 0 | complete |
| GPT-5.6 Sol | **412** | 2 | complete |
| GPT-5.6 Terra | **414** | 0 | complete |
| Gemini 3.6 Flash | **375** | 39 | complete, native-tools v2 |
| Qwen3.6-35B-A3B | **345** | 69 | canonical after retry |
| Qwen3-Coder-Next-FP8 | **327** | 87 | canonical after two retry layers |
| Qwen3.6-27B | **363** | 51 | canonical after bounded remainder |
| Devstral-2-123B | **387** | 27 | canonical after retry |
| GPT-OSS-120B | **413** | 1 | complete |
| MiniMax-M2.1 | **378** | 36 | canonical after retry |

### 4.2 仍在运行的模型

| Model | 当前有效 generated | 剩余 | 状态 |
|---|---:|---:|---|
| Kimi K2.5 | **16 unique** | 1 failed + 397 unprocessed | v13 accepted，queued |

Qwen27 已于 `2026-08-12T11:25:39Z` 完成 bounded remainder：

- v4 partial：234 条，其中 179 generated、55 failed；
- v5 remainder：235 条，其中 184 generated、51 failed；
- 两者只在 v4 的 55 个失败 ID 上重叠，并集严格等于 414；
- canonical：**363 generated / 51 failed**。

最终使用 terminal tar，而不是曾在 pause/requeue 后缩短的 live JSONL。v5
archive SHA-256 为
`39405ecdb694b3653a7ed549ebafc65d6a0592f79f3e2dc153f806724b4d8f7e`；
canonical results SHA-256 为
`cc6658363311fbccbfb649d19210cd6d815f4c55a5635c3fdfabb0e43de49c68`。
Qwen27 的 rubric-v2 已完成：414/414 records、无 judge error，
Maintainability **174/414**。除 Native 外六条 correctness lane 已完整落盘，
不等待 Kimi。

Kimi v11 和 v12 的去重 checkpoint 已冻结为 16 generated / 1 failed。v12
容器重启后 live JSONL 从 9 条回退到 5 条并重复旧 ID，因此已终止。v13 使用
精确 397-ID remainder，并在启动时从 Blob `$AMLT_OUTPUT_DIR` 恢复
generation/patch/trace，再进入现有 `--resume`，避免下次容器重启重采样成功项。
Kimi 仍是完整 13 模型总工期的决定性瓶颈，但不阻塞其他模型发布 partial
Final Resolved。

---

## 五、Gemini 3.6 异常与修复

旧 Gemini 3.6 结果为 131 generated / 283 failed，但该结果无效：

- 149 个失败任务连续 32 轮返回空 content；
- 77 个任务只有一次非空响应；
- reasoning budget、source window、output budget 均无法修复；
- 同一任务切换到 OpenAI native tools 后立即返回合法 tool call。

最终修复包括：

1. 文本内嵌 JSON action schema 改为 native OpenAI tools；
2. tool history 使用标准 assistant/tool message，不再文本拼接；
3. 保留 `reasoning_opaque` 和完整 raw API envelope；
4. 第 24 轮仍未编辑时要求模型给出具体 edit；
5. tool transcript 设置字符上限，避免上下文增长到模型边界。

有效 native-tools v2 结果：

| 指标 | 数量 |
|---|---:|
| Generated | **375** |
| Failed | **39** |
| Base rubric-v2 maintainability pass | 223 |
| Delta overlay 后 maintainability pass | **224** |

39 个失败中，37 个在探索后持续返回空 `edits`，2 个最终没有形成有效 patch；
该轮不存在旧 run 的 gateway 空响应问题。

---

## 六、Maintainability 盲裁

统一 judge：

```text
claude-opus-4.8
rubric v2
pass = addressed=yes AND quality>=4 AND introduces_new_debt=false
```

当前结果是 base judgment 加 generation/CRLF delta overlay 后的单门分数：

| Model | Maintainability pass /414 |
|---|---:|
| GPT-5.6 Sol | **251** |
| GPT-5.6 Terra | **225** |
| Claude Opus 5 | **224** |
| Gemini 3.6 Flash | **224** |
| Qwen3.6-27B | **174** |
| Claude Sonnet 5 | **165** |
| GPT-5.6 Luna | **165** |
| Qwen3.6-35B-A3B | **99** |
| MiniMax-M2.1 | **77** |
| GPT-OSS-120B | **51** |
| Devstral-2-123B | **46** |
| Qwen3-Coder-Next-FP8 | **32** |

注意：

- 这是 maintainability 单门，不是 Final Resolved；
- Kimi 尚未完成，因此暂未进入该表；
- correctness 未通过的 patch 即使 maintainability 通过，也不会进入最终分数。

base 盲裁共 4,554 个 task-model pair；schema-invalid 响应通过显式 repair prompt
重试，最终无 `judge_error`。Raw judge bundle：

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-evaluation-20260811/judging/
  rubric-v2-judgments-20260811.tar.gz
SHA-256 d1c032a1566678701f308779676a5e53247120f3660360b30fc8e8605bd0f123
```

Qwen27 增量 judge bundle：

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-evaluation-20260812/judging/
  qwen27-rubric-v2.tar.gz
SHA-256 c9979833098d6d73748571fbc065e63d5064726a6955b98bd068d1547db199ed
```

---

## 七、Correctness 进展

七条 modern lane 的任务规模：

| Lane | Tasks/model |
|---|---:|
| Go/Python | 73 |
| Scripted | 53 |
| TS/JS | 42 |
| C# | 18 |
| JVM | 52 |
| Rust | 71 |
| Native | 65 |
| **Total** | **374** |

### 7.1 Open-five

| Lane | 已完成 / 目标 pairs |
|---|---:|
| Go/Python | 365/365 |
| Scripted | 265/265 |
| TS/JS | 210/210 |
| C# | 90/90 |
| JVM | 260/260 |
| Rust | **355/355**（104 pass，19 infra 待恢复） |
| Native | **233/325**（restarted；66 pass） |

Open Rust 最初因 Docker root-owned Cargo state 占满 `/data`。当前 runner 将
Cargo state 放入 attempt 内，并使用 cleanup container 验证删除，已从 40 条恢复到
355/355。当前 19 条仍标记为 infra，主要来自 stale worktree、Oracle setup
metadata 和 exact-start baseline 环境；这些行不会提前当作模型 fail。

### 7.2 Hosted-five

| Lane | 已完成 / 目标 pairs | 已知 pass |
|---|---:|---:|
| Go/Python | 365/365 | 274 |
| Scripted | 265/265 | 180 |
| TS/JS | 210/210 | 189 |
| C# | **90/90** | 61 pass，6 infra 待恢复 |
| JVM | **260/260** | 175 pass，35 infra 待恢复 |
| Rust | **355/355** | 253 pass，14 infra 待恢复 |
| Native | **207/325** | restarted；151 pass |

### 7.3 Gemini 3.6

| Lane | 已完成 / 目标 pairs | 已知 pass |
|---|---:|---:|
| Go/Python | 73/73 | 48（infra overlay 单独保存） |
| Scripted | 53/53 | 39 |
| TS/JS | 42/42 | 33 |
| C# | 18/18 | 8 |
| JVM | 52/52 | 34（7 个 infra pair 待 overlay/recovery） |
| Native | 65/65 | 44；IREE infra 已 overlay 为真实 fail |
| Rust | **71/71** | 38 pass，3 infra 待恢复 |

Gemini Native 唯一的 IREE infra pair 已通过完整 Git/submodule cache 重跑：
candidate fail 且 exact-start baseline pass，因此最终归类为真实 correctness fail，
不再存在该 pair 的 infra 状态。

### 7.4 Qwen3.6-27B

Qwen27 的 414-task canonical 已在独立输出中完成 prepare：

```text
/data/v-peiyangliu/reviewbench-openmodels/
  correctness-20260812-qwen27-v1/
```

七条 modern correctness lane 均已完整落盘。当前可运行的 4 条 published
legacy 也已独立重放；36 条 legacy 仍因历史资产缺失而 blocked。

Qwen27 当前 378-task partial：

| Gate | Pass |
|---|---:|
| Generation/apply | 332/378 |
| Scope | 327/378 |
| Correctness | 179/378 |
| Maintainability | 160/378 |
| **Final Resolved** | **93/378** |

其中 19 个 correctness pair 仍为 infra/unresolved，因此 93 同时也是当前证据集
上的 fail-closed lower bound。Portable release：

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-evaluation-20260813/qwen27/
  qwen27-partial378-v1.tar.gz
SHA-256 e5b58bd948ced81803f8e40667f30446fc7ce0e6891896c54d7bcb911e727eb1
```

### 7.5 管理员重启恢复

本地评测机于 `2026-08-12T17:14Z` 重启。重启前 checkpoint 均为原子 JSONL，
没有丢记录：

- Open Native：233/325；
- Hosted Native：207/325；
- Qwen 其余六 lane：309/309；
- Qwen Native：尚未开始。

清理已完成 Qwen lane 的可重建 cache 后释放约 61 GiB，未删除 patch、raw log
或 evidence。随后磁盘可用空间增长到 600+ GiB，Open、Hosted、Qwen 三条
Native 均从 1 worker 提升到 3 workers，且移除了 correctness 对 Kimi monitor
的依赖。Qwen Native 已完成；本段更新时 Open/Hosted Native 分别为 311/325
和 287/325。4141 judge 服务随重启停止，但当前没有未完成 judge 请求；Kimi
generation 完成前会重新启动。

### 7.6 Correctness 基础设施恢复

本周额外完成：

- 六个 Go/Python 历史 runtime 问题的 11 模型重放：66 rows，
  54 pass / 10 fail / 2 candidate-missing，0 infra；
- Poktroll full Git object cache 重放：5/5 pass；
- BSMPT 与 IREE 的 Coder pair：
  - BSMPT 已确认真实 fail；
  - IREE 已确认真实 fail；
  - 两者 exact-start baseline 均通过，0 infra；
- CRLF source patch 从保存的 tool trace 重新生成，不调用模型，避免 patch apply
  被错误计为 generation/correctness fail。

---

## 八、Published legacy replay

414-hard 中有 40 道来自既有 published hard：

- 374 道新 hard 已具备现代七-lane replay contract；
- 40 道 published hard 的 compact release 缺现代 contract。

本周新增 `run_legacy_reserve_correctness.py`，对 20260728 的 14 道 published hard
执行 fail-closed preflight：

| 状态 | Tasks |
|---|---:|
| 可运行 | **7** |
| 缺历史资产 | **7** |

11 个已完成模型的 7-task slice 已全部重放：

| 结果 | Rows |
|---|---:|
| Pass | **33** |
| Fail | **38** |
| Candidate missing | **8** |
| **Total** | **79** |

79 比 7 x 11 多 2，是因为两个模型在 blocked JVM 题上本身 generation missing；
这些行只记录 candidate missing，没有伪造 correctness verdict。

Legacy bundle：

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
  reviewbench-hard414-evaluation-20260811/correctness/
  correctness-20260811-legacy-20260728-eleven-v1.tar.gz
SHA-256 8cfa350847d4a86a612b4be4f278ac7c6b4fe64a5f453439a81f4f059598acfd
```

完整 `/414` 仍有 **33 道 published legacy** 未恢复：

- 20260728：7 道缺 Gradle/Cargo cache 或未证明 harness；
- v1 + 20260721：26 道尚未迁移到当前 replay contract。

因此，在这些资产恢复前，最新模型只能发布明确标注的 partial score，不能把
缺失 correctness 当作 fail 或 pass。

---

## 九、本周关键工程修复

### 9.1 Inference

- GPT-5.6 从错误的 Chat Completions route 改为 Copilot Responses API；
- Gemini 3.6 改为 native tools + structured history；
- 非 JSON ellipsis 在 transcript 序列化前 fail-fast；
- Git BlobFuse read timeout 从 60 秒提高到 300 秒；
- tool transcript 设置字符上限；
- targeted retry 支持精确 IDs 和 per-job output budget；
- canonical merger 支持 partial base 与 remainder 的完整并集，同时继续禁止
  retry 覆盖成功样本，并 fail-closed 校验最终 414-task coverage；
- AMLT generation 启动时从持久 Blob output 恢复 checkpoint，避免
  `$AMLT_DIRSYNC_DIR` 随容器重启丢失。

### 9.2 Open-model 服务

- NumPy/SciPy 版本固定；
- vLLM custom all-reduce 切换 NCCL fallback；
- MiniMax 启用 expert parallel；
- Kimi 使用 H100 x8、FP8 KV、32K context、concurrency 2；
- minimal Git cache 写入真实 durable ref，解决 Blob 不保留空 refs。

### 9.3 Correctness

- Rust root-owned scratch 使用容器清理并强校验；
- 历史 Go toolchain、Python venv 和 `PYTHONPATH` 隔离恢复；
- multiprocessing AF_UNIX 路径缩短；
- CRLF source line ending 保留；
- Native sanitized path、plan env、Conan profile、submodule reset 和 IREE CMake
  gate 恢复；
- partial manifest 的多-lane missing pair 全局排序修复。

### 9.4 Maintainability

- judge schema-invalid response 使用显式 repair prompt；
- 每次 raw response 均保留；
- generation/judge/error 不允许被 success-shaped fallback 掩盖。

---

## 十、产物与可复现性

主要本地工作根：

```text
/data/v-peiyangliu/reviewbench-openmodels/
```

主要 BlobFuse 产物根：

```text
/home/v-peiyangliu/conversationhub/liupeiyang/code_quality/
```

权威 failure audit：

```text
reviewbench-openmodels/analysis/20260811/failure-analysis-v2.json
```

Canonical generation：

```text
/data/v-peiyangliu/reviewbench-openmodels/canonical-20260811/
```

Qwen27 final inference archive：

```text
/data/v-peiyangliu/reviewbench-openmodels/completed-20260812/
  qwen27-v5-final/
SHA-256 39405ecdb694b3653a7ed549ebafc65d6a0592f79f3e2dc153f806724b4d8f7e
```

本周新增/重点代码：

- `scripts/generate_agentic_patches.py`
- `scripts/run_open_model_agentic.py`
- `scripts/run_open_model_correctness.py`
- `scripts/run_legacy_reserve_correctness.py`
- `scripts/merge_generation_retries.py`
- `scripts/stage_generation_subset.py`
- `scripts/judge_release_patches.py`

本周代码变更均已拆分提交；本周报作为独立文档提交。

---

## 十一、当前风险与汇报边界

### 可以对外陈述

1. 414-hard benchmark 已冻结且已有三模型正式基线。
2. 最新 12 模型已完成 generation，并已完成或进入 maintainability/correctness；
   Kimi 仍在独立 generation。
3. Gemini 3.6 的异常已定位为 API/schema 适配问题，修复后有效 generation 为
   375/414。
4. Open-model retry 只覆盖确认的基础设施/harness 失败，没有重采样成功样本。
5. Raw inference、judge、correctness evidence、manifest 和 SHA-256 均保存。

### 暂时不能对外陈述

1. 不能发布最新模型 Final Resolved `/414` 排名；Qwen27 仅可发布明确标注的
   partial **93/378**；
2. 不能把缺 replay contract 的 33 道题当作 correctness fail；
3. Qwen 必须以已冻结 terminal tar 和 canonical manifest 为准，不能回退到
   历史 live JSONL；
4. 不能把 Kimi 当前 17 条已处理结果外推为模型最终水平；
5. 不能把 maintainability 单门排名称为 benchmark 总榜。

### 主要风险

| 风险 | 影响 | 当前处理 |
|---|---|---|
| Kimi inference 极慢 | 决定最终交付日期 | 保留成功样本，固定剩余 ID，持续运行 |
| Qwen pause/requeue checkpoint 缩短 | 远端 live 文件不完整 | terminal tar 已冻结，v4/v5 并集和 canonical 414 已校验 |
| 36 道当前不可运行 legacy contract | 阻塞 Qwen 完整 `/414` correctness | fail-closed，逐来源恢复 |
| Native/Rust 重型构建 | 时间和磁盘压力 | 3-worker 独立流水线、attempt cleanup、增量 overlay |

---

## 十二、下一阶段计划

1. 恢复 Qwen27 的 19 条 correctness infra 和 36 条 blocked legacy；partial
   结果不等待这些恢复。
2. Kimi 剩余 397 个 ID 独立运行，不作为其他模型评分或发布的依赖。
3. 恢复 open-five Rust 的 19 条 infra，并完成正在运行的 Native lane。
4. 恢复 hosted C# 的 6 条、JVM 的 35 条 infra，并完成 Rust、Native。
5. 恢复 Gemini Rust 的 3 条 infra，并合并已完成的 IREE overlay。
6. 合并所有 canonical generation、correctness recovery、legacy 和 judge delta。
7. 恢复 33 道剩余 published legacy contract；在此之前只发布 partial。
8. 生成每模型四门表：

```text
Generation
Scope
Correctness
Maintainability
Final Resolved / 414
```

9. 打包 raw artifacts、manifest、SHA256SUMS 并上传 Blob。
10. 更新论文表格、方法说明和最终 leaderboard。

---

## 十三、汇报摘要

> 本周完成了 ReviewBench 414-hard 最新模型评测的主体工程。11 个最新模型已有
> canonical generation，Gemini 3.6 的 API 失效问题已彻底定位并修复，开源模型
> targeted retry 恢复了 100 余个原本被基础设施误伤的 generation。统一
> maintainability 盲裁已经完成，correctness 正在按七条语言 lane 并行推进。
> 同时恢复了 7 道 published legacy Oracle，并完成 11 模型重放。当前最终榜尚未
> 发布，主要剩余瓶颈是 Kimi 的低吞吐、Qwen checkpoint 稳定性，以及 33 道历史
> replay contract 的资产恢复。
