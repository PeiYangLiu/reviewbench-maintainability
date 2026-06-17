# ReviewBench-Maintainability 数据管线与筛选漏斗（通俗版）

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
| **① apply** | 改动能否落地 | 模型给的 SEARCH/REPLACE 能套到文件上（套不上重试最多 3 次） |
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
