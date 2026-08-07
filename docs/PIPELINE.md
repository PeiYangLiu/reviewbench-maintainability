# ReviewBench-Maintainability 数据管线与筛选漏斗（通俗版）

> **口径更新（quality-improvement/v1）**：本文记录的 28 道 `CLEAN`
> 是旧版 reviewer-concern 口径的历史结果。`CLEAN` 只表示 golden 通过测试且
> judge 认为评论被回应，不能证明题目本身是明确、实质的代码质量提升。
> 新发布集还必须通过双模型一致的 quality-improvement gate、本质难度
> medium/hard、稳定 PASS_TO_PASS、改动行 coverage 和语义去重。旧 28 不能直接
> 计入新目标 100 道。

## v1 最终严格发布（2026-07-14）

目标已经达到。最终权威文件是：

- `data/releases/v1/tasks.jsonl`
- `data/releases/v1/oracles.jsonl`
- `data/releases/v1/report.json`
- `data/releases/v1/manifest.json`
- `data/releases/v1/SHA256SUMS`

`reviewbench-release --target 100` 对这组 tasks 和 oracles 返回 0。发布报告为
`complete=true`、`eligible=100`、`selected=100`，没有失败原因。以下所有旧的
6/100、12、21、60、79、81 数字都是按时间记录的中间状态，不再代表当前规模。

### 最终组成

| 维度 | 数量 |
|---|---:|
| 最终任务 | **100** |
| 仓库 | 86 |
| PR | 97 |
| hard | 11 |
| medium | 89 |
| Go | 52 |
| Python | 36 |
| TypeScript | 5 |
| PHP | 2 |
| C、C++、C/C++、Java、Rust | 各 1 |

类别分布为 modularity 24、api_design 19、complexity 16、duplication 16、
testability 11、readability 8、dead_code 4、naming 2。

难度采用保守合并。两个模型都判断 hard 才记 hard，因此最终有 11 道
hard。另有 14 道至少一个模型判断 hard、另一个判断 medium，发布层仍将它们
记为 medium。这个口径避免用单个模型的高难判断放大 hard 数量。

### 每道题实际通过的门

最终 100 道全部满足：

1. 精确 `start_sha` 和 `target_sha` 可达。
2. 原代码、锚点局部 golden、完整同文件 diff 证据齐全且未截断。
3. GPT-5.6 Sol 与 Claude Opus 4.8 独立审阅。
4. 两个模型都确认有明确旧 code smell、golden 有实质改善、修法可归因。
5. 维护性是主要目标，相关修法本身没有混入功能或行为变化。
6. 两个模型的质量分都至少为 4，置信度都至少为 0.8。
7. 两个模型都判断本质难度至少为 medium。
8. start 和 source-only golden 各稳定运行 3 次。
9. PASS_TO_PASS 非空。
10. 测试真实命中至少一条锚点修法涉及的 changed-old source line。
11. golden correctness 通过。
12. 相同局部参考修法通过语义指纹去重。
13. 每个仓库最多保留 5 道。

最终 PASS_TO_PASS 数量最少为 1，中位数为 3，最多为 742，合计为 3,297。
这只是稳定回归测试数量，不用于抬高难度分。

### 从 81 到 100 的最后三轮漏斗

在此前 81 道严格池基础上，继续挖掘和回收：

| 阶段 | 第二增量主池 | 第二增量跨语言回收 | 第三增量 |
|---|---:|---:|---:|
| mined candidates | 693 | 同一增量的语言漏审项 | 395 |
| 质量池 | 368 | 63 | 254 |
| 完整证据 | 322 | 49 | 198 |
| 第一模型通过 | 30 | 5 | 12 |
| 双模型通过 | 14 | 2 | 6 |
| strict oracle 通过 | 13 | 2 | **6** |

第二增量主池的 13 道 oracle 成功题中，有一对任务共享同一语义参考修法，发布
层只保留一题，净增 12，道数从 81 到 93。跨语言回收的两道 TypeScript 题都
完成历史 Node 环境验证，道数到 95。

第三增量的 6 道双模型通过题全部完成 oracle，没有把任何环境失败当成题目失败：

- JohnStarich/go：精确 Go 环境，65 个稳定 P2P。
- Buildah：处理历史 Go、CGO/storage 依赖，覆盖 `ExtractRootfsOptions` 旧行。
- CPython：执行真实模块，覆盖 translate/join 两种模式。
- SpiceDB：精确 Go 1.21.3，覆盖 bloom、循环检测和两个调用点。
- google-cloud-python：Python 3.10 Docker，覆盖 split-repo 两条路径。
- Altair：commit-era Python 依赖，覆盖 pandas、VegaFusion 版本和 DataFrameLike 分支。

第三增量结束时共有 101 道严格合格候选。发布器按 hard 优先、质量分、仓库轮转、
每仓库上限和语义去重选择 100 道。CPython 题进入后，一道 medium Vitess 题转入
合格候选储备，不再出现在 v1 的 100 道中。

### 达标后的备用池

持续 miner 没有因达到 100 而停止。第四增量另有 251 个候选、158 道质量池、
120 道完整证据、15 道第一模型通过、3 道双模型通过。这 3 道来自同一
Optimism PR，尚未进入 v1 oracle，也不计入 100。它们保留为后续版本的去重和
oracle 储备，不能用来扩大 v1 数量。

### 发布文件的可复核性

`tasks.jsonl` 保存任务证据、两份质量判断和发布摘要。`oracles.jsonl` 按 ID
保存最终使用的最丰富成功验证记录，包括 P2P、coverage、行为断言、命令、历史
环境和 recovery 元数据。所有本机绝对路径都替换为占位符，临时 worktree、
容器缓存和日志没有复制进项目。

### v1 三模型非 agentic 基线评测

完整评测保存在
`data/releases/v1/evaluations/three-model-20260714/`。三个模型使用相同的
reviewer concern、start 源码、SEARCH/REPLACE 协议和 scope 上限。生成提示不含
golden、target、质量判断或 oracle 结果。

| 模型 | 成功生成补丁 | Correctness | 维护性盲裁 | Resolved@1 | Hard |
|---|---:|---:|---:|---:|---:|
| Gemini 3.5 Flash | 97 | 78 | 79 | **66** | 6/11 |
| Claude Opus 4.8 | 100 | 76 | 77 | **63** | 5/11 |
| GPT-5.5 | 98 | 73 | 73 | **61** | **7/11** |

Resolved@1 要求补丁可应用、scope 通过、P2P correctness 通过、维护性盲裁通过。
总体上 Gemini 第一，Claude 第二，GPT-5.5 第三。Hard 子集顺序不同，GPT-5.5
最高。统一裁判为隐藏候选身份的 Claude Opus 4.8，因此 Claude 作为候选时仍有
无法完全排除的自裁偏差；逐题结果、补丁、correctness 和 verdict 均随发布保存。

评测前额外审计了 oracle 的解法独立性。包括 CPython 在内共 13 道题移除了对
golden 私有 helper 名、签名、MRO、源码位置或内部调用次数的断言，并重新完成
start/golden 三次稳定验证。

### Agentic line-edit 协议

`scripts/generate_agentic_patches.py` 使用 `agentic-line-edit/v1`。每轮模型只返回
一个 JSON action：

- `read`：读取 `start_sha` 下任意仓库文件，锚点文件则读取当前已编辑版本；
- `find`：在文件中做 literal search；
- `edit`：按 1-based 行范围修改唯一允许的锚点 production file；
- `finish`：结束编辑，由 host 生成最终 patch。

host 会拒绝路径穿越、跨文件 edit、重叠 edits、同一插入点、无改动 finish 和
失效 checkpoint。最终 patch 不是模型输出的文本，而是在临时 Git repository
中提交 before、写入 after，再运行 `git diff --binary` 得到。旧 SEARCH/REPLACE
结果继续作为冻结的 non-agentic baseline。若轮次耗尽但 worktree 已有有效改动，
host 会直接固化最后状态，`finish` 只是提前停止动作，不是额外输出格式门。
公平比较必须对全部题、全部模型重新运行 agentic generation、correctness 和
盲裁，不能只补跑旧协议失败项。

306 题最终结果：

| 模型 | Generated | Correctness | Maintainability | Resolved@1 | Hard |
|---|---:|---:|---:|---:|---:|
| GPT-5.5 | 306 | 249 | 233 | 198 | 10/26 |
| Claude Opus 4.8 | 305 | 240 | 221 | 187 | 11/26 |
| Gemini 3.5 Flash | 250 | 194 | 172 | 157 | 7/26 |

完整产物：

```text
data/releases/reserves/20260721/evaluations/agentic-three-model-20260725/
```

复核命令：

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

## quality-improvement/v1 严格审计快照（2026-07-13）

对现有 Go/Python 优先池执行完整新门后的实测漏斗：

| 阶段 | 数量 | 说明 |
|---|---:|---|
| before + 局部 golden + 完整同文件 diff 证据齐全并完成审阅 | 1,763 | 不含 commit 不可达、空 diff、证据截断 |
| 两个不同模型一致确认“实质质量提升 + 本质 medium/hard” | 46 | 通过率 2.61% |
| golden correctness、稳定 PASS_TO_PASS、改动行 coverage 全通过 | 7 | 其余主要损失是 setup、无 P2P 或 coverage 不相关 |
| 相同局部修法语义去重后 | **6** | 1 hard + 5 medium，Go 5 + Python 1 |

当前严格发布集因此是 **6/100，未完成**。发布 CLI 会以非零状态退出，不能把
46 道质量候选、旧 28 道 CLEAN 或 21,140 道 mined candidates 当成发布规模。

按本轮观测产率估算：

- 双模型质量通过率：46 / 1,763 = 2.61%；
- 严格 covered oracle 通过率：6 / 46 = 13.04%；
- 端到端产率：6 / 1,763 = 0.34%；
- 若产率不变，100 道约需 29,384 道完整证据候选，或约 767 道双模型质量题。

所以达到 100 的正确路径不是降低门槛，而是同时扩大挖掘池，并为高频仓库建设
精确历史环境。当前 `scale-big` 只完成了计划 2,700 个 batch 中的 413 个；
恢复该 run 并接近其 200k candidate 目标，才与严格 100 道所需规模相匹配。

### 历史 159 道 medium/hard 池的全量复核

旧 `data/difficulty/hp_strict_scored_v2.jsonl` 中有 79 hard + 80 medium，
共 159 道旧口径 `keep=yes` 任务。对该 cohort 重新执行完整严格漏斗后：

| 阶段 | 数量 |
|---|---:|
| 原始 medium/hard cohort | 159 |
| commit/file 证据可取得 | 137 |
| 证据完整、进入双模型判断 | 135 |
| 第一模型严格通过 | 24 |
| 两个不同模型一致通过 | 19 |
| 实际尝试 oracle | 110 |
| 质量、P2P、coverage 全通过 | **6** |

最终 6 道由 3 hard + 3 medium 构成：TypeScript 2、C++ 1、C 1、
C/C++ 1、PHP 1。Go/Python 100 道虽然全部尝试了 oracle，分别有 6 和 3
道 covered oracle，但与双模型质量通过集合没有交集，因此最终为 0。

历史池新增的 6 道与前述严格 6 道无重复。当前合并严格池为
**12 道唯一任务**，其中 4 hard + 8 medium，覆盖 11 个仓库。

### 专用环境回收进展

环境、baseline、P2P 或 coverage 失败的双模型质量题不会被丢弃，而是进入
`reviewbench-recovery` 队列。第一轮专用 Docker/工具链回收成功新增 9 道：

- scikit-learn、pandas：匹配历史 Python/NumPy/Cython/Meson 环境；
- Ceph、Conda、Ray：定位真实子项目并安装最小历史依赖；
- ceph-csi、grpc-go 两题：精确 Go toolchain、build tags 与非耦合测试；
- Vercel：增加 session-only 行为回归测试，补齐真实 coverage。

每道恢复题仍要求 start/golden 各稳定运行 3 次、非空 PASS_TO_PASS、行为断言及
changed-old-line coverage。第一轮后严格池增至 21 道，恢复队列剩 40 道。
第二轮将除 test-anchor 外的 39 道按 environment、baseline、P2P、
coverage/oracle 分成 5 条专用回收线，最终 **39/39 全部恢复成功**：

- environment 11/11；
- baseline 10/10；
- P2P 10/10；
- coverage/oracle 8/8。

权威发布重算后，当前合并严格池为 **60 道唯一任务**：9 hard + 51 medium，
覆盖 53 个仓库；语言为 Go 27、Python 23、TypeScript 3、C/C++ 3、PHP 2、
Java 1、Rust 1。62 道双模型质量任务中有 61 道具备严格 oracle，其中 1 组
语义重复被发布层去重；仅剩 1 道 test-anchor 被标记为结构性不合格。
距离 100 仍有 40 道，发布命令继续以未完成状态退出。

### 持续挖掘第一批增量

miner 持续运行期间，对新增候选做了不中断挖掘的增量快照：

| 阶段 | 数量 |
|---|---:|
| 新增 mined candidates | 1,833 |
| Go/Python 源码质量池 | 799 |
| 完整 before/local/full golden 证据 | 687 |
| GPT 严格通过 | 78 |
| Claude 独立复核后双模型通过 | 19 |
| 直接 oracle 通过 | 1 |
| 专用 recovery 成功 | 18 |
| 新增严格任务 | **19** |

这 19 道全部具备 start/golden 各 3 次稳定 P2P、行为断言和 changed-line
coverage。当前权威严格池因此达到 **79 道唯一任务**：11 hard + 68 medium，
覆盖 68 个仓库。距离 100 还差 21 道；miner 与下一批增量快照继续运行。

> 这份文档用尽量通俗的语言，解释一道题从「GitHub 上一条 PR 评论」一路被筛成
> 「benchmark 里一道可评测的 hard 题」中间经历了哪些阶段、每个阶段按什么标准筛、
> 为什么会漏掉那么多。读完你应该能回答：**最终这些题是怎么来的、代表性如何、瓶颈在哪。**

---

## 0. 这个 benchmark 想测什么

一句话：**LLM 能不能修好"真实代码评审里，reviewer 指出的可维护性问题"。**

数据来源是 GitHub 上**已经合并**的 Pull Request。我们专挑这样的场景：

- 有人在 PR 里留了一条 review 评论，说"这段代码可读性差 / 有重复 / 命名不好 / 太复杂……"（**可维护性意见**，不是功能 bug）；
- 作者**真的照着改了**，而且**这个 PR 最终被合并**了。

于是我们就有了一道天然的题：
- **起点（start）** = 被评论时的旧代码（待修）
- **终点（target）** = 人类按意见改完、被合并的代码（标准答案，我们叫 **golden / 人类参考**）

让模型从起点出发去修，再看它改得对不对、好不好。

---

## 1. 一图看懂漏斗

```
  GitHub 搜索挖掘
        │  28,394 条候选（含重复）
        ▼
  去重
        │  21,140 条唯一候选
        ▼
  ② Prefilter 分桶（5 个硬性证据门，规则确定性，非随机）
        │  其中 high_priority ≈ 只有证据最硬的那一档
        ▼
  ③ 难度打分（只给 high_priority 打，全量不抽样）
        │  已打分 1,398 道  →  其中 hard 362 道
        ▼
  ④ 可跑筛选（只留 Go/Python、且锚点不是测试文件）
        │  181 道可跑 hard（Go 93 + Python 88）
        ▼
  ⑤ Harness 执行（clone→build→跑测试，建不起来就挂）
        │  目前只跑了约 136 道 → 52 道成功评测
        ▼
  ⑥ 有效性双门（golden 必须自己过测试 + 能被判定"已解决"）
        │
        ▼
   ✅ 28 道「干净有效」hard 题进榜
```

**关键认知：每一层都是"挑"出来的，不是"随机抽"出来的。** 详见第 9 节。

---

## 2. 阶段①：挖掘（Mining）

**做什么**：用 GitHub 搜索 API，按"关键词 × 编程语言 × 时间窗"的组合，去搜**已合并的 PR**，
再抓里面的 review 评论，初步判断哪些是"可维护性意见"。

**怎么挑**：
- 关键词矩阵：`refactor / cleanup / simplify / readability / duplicate / dead code / maintainability /
  modularity / rename / "code smell" / "technical debt"` 等几十个**可维护性词**。
- 语言：go / python / javascript / typescript / java / rust / cpp / ruby / php / c# / kotlin / scala。
- 时间窗：2022~2026 各年。

**为什么会有重复**：同一条评论可能被多个查询（比如同时含 "refactor" 和 "cleanup"）命中，所以
**28,394 条原始命中去重后 = 21,140 条唯一候选**。

**这一步的偏向**：只会挖到**措辞里带这些关键词**的评论。措辞含蓄的（"this feels off"）会被漏掉。

---

## 3. 阶段②：Prefilter 分桶（21,140 → 高优 1 档）

这是**纯规则、确定性**的一步——每条候选按下面的布尔条件，确定性地落进 4 个桶之一，**没有任何随机**。

先解释几个"证据"字段（都来自 GitHub，用来判断"这条评论事后是不是真被处理了"）：
- **thread_resolved**：该评论的讨论线程被标记为"已解决"。
- **region_touched**：评论锚定的那几行代码，**在评论之后真的被改动过**。
- **file_touched**：评论所在文件，评论之后被改过。
- **test_paths**：这个 PR 是否**也改了测试文件**（有测试 = 后面才有自动判对错的可能）。
- **contains_suggestion**：评论里是否直接贴了"建议代码块"（如果有，等于**答案泄露**，要小心）。

### 四个桶的规则（代码原文翻译）

| 桶 | 进入条件（全部满足） | 含义 |
|---|---|---|
| **rejected**（淘汰） | 命中任一拒绝原因 | 直接不要 |
| **high_priority**（高优） | 非 suggestion **且** thread_resolved **且** region_touched **且** 有测试改动 **且** 改动文件 ≤ 80 | 证据最硬：评论被解决、锚定区域被改、有测试、PR 不臃肿 |
| **oracle_candidate**（候选） | 非 suggestion **且** 有测试改动 **且**（thread_resolved **或** region_touched） | 有测试 + 部分证据，但不如高优硬 |
| **needs_review**（待审） | 以上都不满足 | 有维护性信号，但证据弱 / 没测试 |

**拒绝原因（rejected）top**：
- `commented_file_not_touched`（评论的文件事后没被改 → 没法验证）≈ 最大头
- `bot_or_automation_author`（机器人留的评论）
- `instruction_too_short`（评论太短，构不成任务）
- `generated_or_vendor_locality`（改的是自动生成 / 第三方目录）

**这一步漏了什么**：~19,700 条候选**没进 high_priority**，绝大多数是卡在
"缺 thread_resolved 或 region_touched 或 没有测试改动"。它们**不是被随机丢掉**，而是
**证据不够硬**。其中 **oracle_candidate 是最大的"未开采"储量**——质量其实不差，只是证据没高优强。

---

## 4. 阶段③：难度打分（只给高优档打，全量不抽样）

**做什么**：为每道高优题，去 GitHub 抓"人类真实修复的 diff"（start→target 两点 diff），
按这个 diff 的**形状**算一个难度分，分成 easy / medium / hard。

**为什么只打了 1,398 道**：打分要**逐题调 GitHub API 抓 diff，很贵**，所以**只对 high_priority 桶打**
（原始池 610 + 新一轮 788 = 1,398），且对该桶是**全量打分、不抽样**（788 进、788 出，一个不差）。
oracle_candidate / needs_review 那 ~19,700 条**还没打分**。

**打分公式（代码原文）**：分数 = `base_size + structure + category + spec`，再裁剪到 0~7：

- **base_size（改动规模）**：总改动行数 ≤3 记 0；≤10 记 1；≤30 记 2；更多记 3。
- **structure（结构复杂度）**：新增函数/类定义数（最多记 2）+（hunk≥3 加 1）+（净增≥8 行加 1）。
- **category（类别调整）**：`complexity / modularity / api_design / testability` 这类"硬"类别 +1；
  `dead_code / duplication` 这类"机械"类别 −1。
- **spec（规格调整）**：评论自带建议代码块 −1（太好抄）；纯删除 −1（太简单）。

**分档**：`score ≤1 = easy`，`≤3 = medium`，`≥4 = hard`。

**结果（最新一轮 788 高优）**：hard 235 / medium 253 / easy 247 / unknown 53（diff 抓取失败或太杂）。
**两池合计 hard = 127（原） + 235（新） = 362。**

---

## 5. 阶段④：可跑筛选（362 hard → 181 可跑）

要让题"能自动判对错"，必须能在我们的执行环境里跑该仓库的测试。于是：

- **只留 Go 和 Python**：这两个生态我们有正确性 harness。TS / JS / Rust / Java / C++ 等
  **没有 harness，直接丢**（235 新 hard 里有 27 个属于这类）。
- **去掉 test-anchor**：如果 reviewer 评论锚定的文件**本身就是测试文件**，那这道题
  "要改的东西"和"判分用的东西"是同一个文件 → 无效，丢掉（Go 砍 17、Python 砍 10）。

**结果**：181 道可跑 hard（Go 93 + Python 88）。

---

## 6. 阶段⑤：Harness 执行（建得起来才算数）

对每道可跑题，自动做这些事（这一步**流失最严重**）：

1. **clone** 仓库；
2. **checkout 到 start_sha**（评论时的旧代码）；
3. **build / 装依赖**（Python 用 uv 建虚拟环境；编译型仓库用 Docker 镜像）；
4. **跑测试拿基线**：同一批测试连跑 3 次，只保留**每次都过**的（排除 flaky 抖动）；
5. 应用人类的源码改动（golden），再跑 3 次，得到**两边都稳定通过的测试集 = PASS_TO_PASS**。

**常见的挂法（setup failed）**：
- `baseline_no_pass`（最多）：仓库测试在起点就跑不过（重仓库、环境不对、缺依赖）；
- `env`：Python 虚拟环境装依赖失败；
- `no_pass_to_pass`：起点和 golden 之间找不到"共同稳定通过"的测试（常因 start/target 相隔太远）。

**现状**：181 道里**目前只跑了约 136 道**，其中**约 52 道成功评测**，其余 setup 挂。
重仓库（etcd / vitess / temporal、编译型 Cython Python）是流失重灾区。

---

## 7. 阶段⑥：评测四道门 + 有效性双门（52 → 28）

每个候选解法（人类 golden、各个模型、空解 empty）都要过**四道门**才算 Resolved：

| 门 | 判什么 | 怎么判 |
|---|---|---|
| **① generation/apply** | 改动能否落地 | non-agentic 基线检查 SEARCH/REPLACE 能否生成 patch；agentic 协议由 host 编辑 worktree 并让 Git 生成 patch |
| **② correctness** | 没改坏 | PASS_TO_PASS 测试全部仍然通过 |
| **③ scope** | 改得聚焦 | 只动了允许的源文件、改动行数不超限（防止"重写整个文件"作弊） |
| **④ maintainability** | 真解决了评论 | 一个 LLM judge 只看"锚定文件的 diff"，判断 reviewer 的关切是否被 address |

**Resolved@1 = ② 且 ③ 且 ④ 都过。**（① 不过直接判负。）

### 有效性双门（决定一道题算不算数）

我们用**人类参考 golden** 给题目做体检：

- **VALID**：golden 自己能过 correctness（说明测试 oracle 是好的，不是坏题）。
- **CLEAN**：golden 还能进一步 **RESOLVE**（连人类的真实修复都过了 judge）。

**只有 CLEAN 的题才进最终榜。** 为什么？如果连人类真实的修复都过不了我们的 judge，
说明这题要么 oracle 坏了、要么 judge 标准不对、要么"人类当年根本没照评论字面改"——
这种题留着只会添乱，自动剔除。

**结果**：52 成功评测 → 31 VALID → **28 CLEAN**（最终 hard 题）。

---

## 8. 真实数字漏斗（一张表看全）

| 阶段 | 剩余 | 这一步砍掉了什么 |
|---|---|---|
| ① 挖掘（含重复） | 28,394 | — |
| 去重 | 21,140 | 同一评论被多查询命中 |
| ② Prefilter 高优档 | （高优是 1 档，其余进别的桶） | ~19,700 证据不够硬（缺线程解决/区域改动/测试） |
| ③ 已难度打分 | 1,398 | **只给高优打分**，候选桶还没碰 |
| ③ 其中 hard | 362 | medium/easy/unknown 分流 |
| ④ 可跑 hard | 181 | 无 harness 语言(−27)、test-anchor(−27) |
| ⑤ 实际跑过 harness | ~136 | 还没排到的没跑 |
| ⑤ 成功评测 | 52 | setup 挂（build 不起来 / 无 PASS_TO_PASS） |
| ⑥ VALID（golden 过测试） | 31 | golden 自己没过测试的坏题 |
| ⑥ **CLEAN（最终 hard）** | **28** | golden 没能 RESOLVE 的题 |

---

## 9. ⚠️ 重要声明：这是**选择性样本**，不是随机抽样

**最终 28 道 hard 是层层"挑"出来的，不是从全体随机抽的。** 每层都有偏向：

| 层 | 偏向了什么（系统性排除了什么） |
|---|---|
| 挖掘 | 偏向**措辞含关键词**的评论 |
| Prefilter | 偏向**证据最硬**（线程解决+区域改+有测试）的题 |
| 打分 | **只给高优打分**，候选桶（oracle_candidate）整批没碰 |
| 可跑筛 | 偏向 **Go/Python**，排除其他语言 |
| Harness | 偏向**环境好搭的中小仓库**，排除重仓库 |
| 有效性 | 偏向 **oracle 干净、judge 认可**的题 |

**所以这 28 道代表的是**："措辞清晰 + 证据硬 + Go/Python + 仓库好搭 + oracle 干净"的 hard 题。
**不能**直接用它推断"LLM 在所有可维护性 hard 题上的真实水平"。

这其实是**所有可执行 benchmark（包括 SWE-bench）的共同代价**：为了有自动正确性判定，
必然牺牲代表性。**对外报告时应诚实标注为 "runnable subset"，并注明 N 较小。**

> 如果要量化"规则筛选到底偏掉了什么"，可以额外做一个**随机抽样轨道**：从落选的
> oracle_candidate 里随机抽一批，对比它们的类别/语言/仓库分布与这 28 道的差异。

---

## 10. 瓶颈与"未开采"储量

**漏得最狠的三个环节**，也是后续放大规模的三个抓手：

1. **Prefilter → 打分**：21k 候选只打分了 1,398，**oracle_candidate（每轮 ~2,463）整批没打分**
   → 按 ~30% hard 比例，潜在还有**几千个 hard**没被发现。**纯 API 活，最划算。**
2. **语言覆盖**：hard 里非 Go/Python 的直接丢。给 TS/Rust/Java 补 harness 能解锁一批。
3. **Harness setup**：评测端最大流失（`baseline_no_pass` + `env` 占 ~66%）——重仓库建不起来。
   每个重仓库都要单独搭环境（SWE-bench 式 per-repo Docker）。

**结论**：28 不是天花板，是"已打分 ∩ 可跑 ∩ oracle 干净"的交集。题根本不缺
（362 已知 hard 才跑了 ~136，21k 候选才打分 1,398），缺的是**打分预算**和**逐仓库的环境工程**。

---

## 附：相关脚本

| 阶段 | 脚本 |
|---|---|
| ① 挖掘 | `scripts/mine_scale.py` → `reviewbench_maintainability.build_dataset` |
| ② Prefilter | `scripts/prefilter_candidates.py` |
| ③ 难度打分 | `scripts/score_difficulty.py`（复用 `enrich_cases.py` 抓 diff） |
| ④⑤⑥ 可跑/评测（Go/轻 Python） | `scripts/evaluate_e2e.py`（复用 `trackA_filter.py` / `trackA_go.py` / `judge_prototype.py`） |
| ④⑤⑥ 可跑/评测（编译型 Python） | `scripts/evaluate_docker.py`（复用 `docker_harness.py`） |
| 案例可视化（HTML） | `build_review_items.py` → `enrich_cases.py` → `summarize_cases.py` → `render_review_page.py` |

工程踩坑实录见 `docs/PITFALLS.md`（P1–P44）。
