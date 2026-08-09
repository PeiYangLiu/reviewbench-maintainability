# ReviewBench-Maintainability 踩坑记录（PITFALLS）

> 构建这个 benchmark 过程中踩到的坑、根因、解决办法。**每碰到一个新坑就往这里追加一条**，方便以后不重复犯错、也方便写进 paper 的 methodology/limitations。
>
> 每条格式：**现象 → 根因 → 解决 → 影响 → 代码位置**。

---

## 数据挖掘 / diff 提取

### P1. GitHub 三点 compare 在被 rebase 的 PR 上产生「幻觉新增」
- **现象**：某条修法显示成 `+N/-0` 全是新增，但那些代码在 `start_sha` 其实早就存在；难度被虚高。
- **根因**：`GET /repos/{repo}/compare/{start}...{target}` 用的是**三点 diff**（`merge-base(start,target)..target`）。如果 PR 被 rebase/force-push，`start_sha` 不是 `target_sha` 的祖先，merge-base 会退到很早的提交，把"早已存在的代码"算成新增。
- **检测**：compare 返回 `status: "diverged"`、`behind_by > 0`。
- **解决**：**不要用 compare 的 patch**。直接拉 `start_sha` 和 `target_sha` 两个版本的文件 blob，本地 `difflib.unified_diff` 算**真实两点 diff**（rebase 免疫）。
- **影响**：hard 计数 184 → 164。
- **代码**：`scripts/enrich_cases.py` `fetch_fix()` / `two_dot_file_diff()`。

### P2. `start_sha` 这个 commit 可能整个不存在（被 force-push 删掉）
- **现象**：`dead_code 删参数`却显示 `+65/-0` 全新增；`before` 上下文抓取失败。
- **根因**：`GET /contents/{path}?ref={start_sha}` 返回 404 `No commit found for the ref`。如果把它当成"空文件"，difflib 会把整文件算成新增（又一类幻觉）。
- **解决**：在 `fetch_file_text` 里**区分两种 404**：
  - `No commit found for the ref` → commit 没了 → 抛 `MissingRefError` → 该 case **判无效、剔除**（连 start_sha 都 checkout 不了，根本没法做任务）。
  - `Not Found`（commit 有效但文件不在）→ 返回 None（合法的新增/删除文件）。
- **影响**：剔除 46 条无效 case；hard 164 → 127。
- **代码**：`scripts/enrich_cases.py` `MissingRefError` / `fetch_file_text()`。

### P3. 单条评论的「参考修法」会混入 PR 的无关改动（bundled changes）
- **现象**：文件级 `start→target` diff 里既有评论要的修法，又有 PR 顺手做的别的事（例：日志改 dict 的同时加了 `unavailable` 状态功能；django 改名 PR 顺手加了一堆新测试）。
- **根因**：一个 PR 通常解决多件事，但 benchmark 任务锚定的是**单条评论**。文件级 diff 把它们混在一起。
- **影响**：难度被撑高、参考答案不纯、oracle 不干净。
- **缓解（待加强）**：difficulty/参考修法应**聚焦到评论锚定行附近的 hunk**（`focus_patch` 已做部分），未来需要更强的"按 concern 隔离"。

---

## 难度评分

### P4. 难度打分器高估「以新增为主 / 捆绑改动」的 case
- **现象**：一个本质是机械改名的 case（django naming）被打成 hard（+71 行、5 个新定义）。
- **根因**：那 71 行大多是 PR **顺手加的新测试**，不是改名本身；打分按 size/new_defs 给高分。
- **关联**：本质同 P1/P3——diff 里混进了无关/幻觉内容。修了 P1/P2 后很多虚高被纠正。
- **代码**：`scripts/score_difficulty.py`。

### P5. 标注 split 之间有子集关系，直接相加会重复计数
- **现象**：`strict + relaxed = 503`，但去重后其实只有 370。
- **根因**：`verified_strict ⊂ review_pool_relaxed`（relaxed 池已包含全部 strict）。
- **解决**：合并任何池子时**一律按 `id` 去重**。
- **代码**：`apply_annotations.py` 的 split 逻辑；分析时 `dict(byid)`。

---

## 评测 / 正确性 oracle（Track A）

### P6. 「start_sha 的测试必须全过」这个朴素 oracle 是错的
- **现象**：在 start_sha 跑测试全绿（13/13），但打上**正确的 gold 源码修法**后 2 条挂了。
- **根因**：那 2 条测试**和实现耦合**（断言旧的 f-string 日志格式 / 旧 status 值），而正确修法本来就会改变这些细节——PR 自己也把这 2 条测试一起改了。
- **解决**：用 **SWE-bench 式 PASS_TO_PASS**：`= 在 start_sha 绿 ∩ 打完「完整 gold（含测试改动）」后还绿`。被 PR 改过的测试**排除**出 oracle。构造它需要逐 task 跑一遍完整 gold 来确定稳定集。
- **验证**：本例 PASS_TO_PASS = 13 − 2 = 11 条稳定测试；打完整 gold（源码+测试）后 19/19 绿（PR 还新增 6 条）。
- **案例**：`ansible/metrics-service#175`。

### P7. 不能用 PR「改后的测试」当评分标准
- **现象**：想直接用 target_sha 的测试给模型打分。
- **根因**：改后的测试是**照着作者的具体实现**写的（断言特定的 dict 结构/键名），甚至包含 PR 捆绑的无关功能测试。模型用**另一种合法写法**解决同一个 concern 时会被误判失败。
- **结论**：maintainability 有"多种合法解"，绑定单一实现的测试会冤枉正确答案。
  - 改后测试 ❌（绑实现）
  - 改前全部测试 ❌（见 P6，会冤枉正确修法）
  - **前后都过的稳定测试 ✅（当回归网）+ AI judge ✅（判是否解决 concern）**

### P8. 正确性门对「被改的那段代码」是弱守卫
- **现象**：稳定 PASS_TO_PASS 里**不包含**直接测试被改代码的那条（它正是被排除的耦合测试）。
- **根因**：maintainability 里 reviewer 让你改的地方，往往正是实现耦合测试盯着的地方，重构必然动它 → 它被排除 → 剩下的稳定测试测不到这段改动。
- **影响**：正确性门只能保证"没改坏无关功能"，**保证不了"这段重构本身行为对不对"**。
- **结论**：**语义 AI judge 不是可选项，是核心信号**；正确性门只是"别搞坏"的安全网。

### P9. `label_correctness_oracle_usable` 只是人工肉眼猜，不等于真能跑
- **现象**：标注 `yes/partial` 不代表环境搭得起来、测试真覆盖目标代码。
- **结论**：真正的 Track A 资格 = **实际 clone + 搭环境 + 跑通 + 验证覆盖**，这是最贵的一公里。名义 oracle=yes 的 hard 有 25 条，但"真验证过可跑"的目前还是 0。

### P14. 把多个无关测试文件一起跑会「跨文件污染」
- **现象**：单独跑 `test_send_anonymized_to_segment.py` → 13 全过；和另外 3 个测试文件一起跑 → **72 个全 ERROR**（连单独能过的也挂）。
- **根因**：不相关的测试文件共享 DB / session 级 fixture，一个文件的 DB 初始化失败会污染整个 session。
- **解决**：`trackA_filter.py` 里**每个测试文件单独跑、再合并结果**；coverage 也只跑稳定测试的 node id（分文件分组），避免顺带跑到耦合测试。
- **代码**：`scripts/trackA_filter.py` `run_pytest()`（逐文件）。

### P15. 构造 PASS_TO_PASS 必须只打「源码 gold」，不能打完整 gold
- **现象**：脚本一开始 `git checkout target -- <所有改动文件>`（含测试文件）→ p2p=13、排除 0 条，把耦合测试**掩盖**了。
- **根因**：完整 gold 把测试文件也换成 target 版（耦合测试被重写后同名通过），按测试名取交集就误判为"前后都过"。
- **解决**：只 checkout **源码改动文件**的 target 版（`is_test_path` 过滤掉测试文件 + `test_change_paths`），测试文件保持 start 版 → 耦合测试正确地挂掉、被排除。
- **注意**：SWE-bench 的"始终注入 test patch"**不适用**于 maintainability（见 P7：test patch 绑死作者实现）。

### P16. `coverage json` 在项目设了 `fail-under` 时退出码非 0
- **现象**：覆盖率 JSON 其实写出来了（"Wrote JSON report"），但命令退出码非 0（`total of 39 is less than fail-under=80`），脚本误判为失败。
- **解决**：**别看退出码**，直接判断 JSON 文件是否生成、能否解析。
- **代码**：`scripts/trackA_filter.py` `coverage_of_changed()`。

### P17. 很多 oracle=yes 的 hard 其实是「改测试代码」任务，没有行为 oracle
- **现象**：候选里不少 task 的目标文件本身就是测试文件（如 `tests/.../test_ball_tree.py`、`test_validation.py`、`test_cli.py`）。
- **根因**：testability 类评论常是"把这个测试写好点"——改的是测试代码本身，**没有"被测行为"可回归**。
- **影响**：这类不进 Track A（`stage=test_only_task`）。Track A 真正要的是**源码**可维护性任务。

### P18. `git clone` 默认会因 `start_sha` 不在历史里而无法 checkout（同 P10）
- **解决**：clone 后 `git fetch origin <start_sha>` 和 `<target_sha>` 各拉一次（按 sha）；`git reset --hard <sha>` 而非 checkout 分支。环境目录（`.venv`）要在 `git clean` 时 `--exclude`，否则每次重建。

---

## 工具 / 基础设施

### P10. `git clone`（默认）里没有 `start_sha`
- **现象**：`git checkout <start_sha>` 报 `reference is not a tree`。
- **根因**：start_sha 是 PR 分支 / rebase 前的 commit，默认 clone 不含；`pull/N/head` 也可能不含（若已 rebase）。
- **解决**：`git fetch origin <start_sha>` 按 SHA 直接拉单个 commit（GitHub 允许，即使它不在任何分支上）。

### P11. TRAPI GPT-5.5 限流严重（429）+ 脚本只在最后写文件会丢进度
- **现象**：summarize 跑到一半超时被杀，10 条一条没存下来。
- **根因**：① TRAPI 共享部署经常 429；② 脚本在循环结束后才一次性写出。
- **解决**：① summarizer **每条 checkpoint**（临时文件 + rename），超时/被杀不丢、可 `resume`；② 加 `--backend responses` 走**本地 Copilot `/v1/responses`**（claude-opus-4.7）做 fallback，限流时秒出。
- **代码**：`scripts/summarize_cases.py`。

### P12. snap chromium 截图写不进 `/tmp`
- **现象**：`--screenshot=/tmp/x.png` 提示写了 N 字节，但 `/tmp/x.png` 不存在。
- **根因**：snap 沙箱限制 `/tmp` 访问。
- **解决**：截图输出写到 `$HOME` 下。

### P13. 包没 `pip install -e .` 就跑测试 → ModuleNotFoundError
- **解决**：跑 `pytest` 前先 `python -m pip install -e .`（本仓库测试前提）。

---

## 待办 / 已知未解决
- P3：参考修法 / 难度的"按单条 concern 隔离"还不够强（仍可能混入同文件无关 hunk）。
- P8/P9：Track A 正确性 oracle 还没真正规模化验证（环境 + 覆盖）。
- judge 还需在更大带人工标注的样本上做 judge-人一致率校准。

### P19. Track A 的真正瓶颈是「环境搭建」，通用安装只对纯 Python 项目有效
- **实测**：10 条 oracle=yes 的 Python hard 跑漏斗 → **只有 1 条 survives**。5 条死在 `env`（构建失败），4 条死在 `baseline_no_pass`（环境装了但 0 测试通过）。
- **根因**：`uv sync` / `pip install -e .` 只能搞定**纯 Python app/lib**（如 ansible）。科学计算/基础设施仓库需要 bespoke 构建：
  - scikit-learn / scipy：Cython / C / Fortran 编译，`pip install -e .` metadata-generation-failed
  - diffusers：要 torch（多 GB，可能要 GPU）
  - conda：自身构建依赖（pycosat、menuinst）装不上
  - dask：旧 setuptools 在 py3.12 下 `SafeConfigParser` 报错
- **结论 / 岔路**：Track A 要规模化，**必须像 SWE-bench 那样逐 repo 做 Docker 镜像**（钉死依赖+构建配方），这是大工程；否则只能**主动挑环境友好的仓库**（纯 Python、pip 可装、无编译扩展），用广度换可行性。
- **数据**：funnel 见 `data/trackA/funnel.jsonl`，脚本 `scripts/trackA_filter.py`。

### P20. 静态「环境友好度」判定会高估，需 curated 黑名单 + 实跑验证
- **现象**：只按 GitHub languages（纯 Python 占比）判，会把 conda（25 题）、diffusers 误判为"可跑"——它们代码是纯 Python，但**依赖**要重型构建（pycosat/menuinst 编译）或 torch（多 GB）。
- **解决**：加 curated 重型仓库/依赖黑名单（conda/torch/tensorflow/ray/airflow/sklearn/scipy/numpy/matplotlib...）+ 检 `meson.build`/`CMakeLists.txt`/`ext_modules`/Cython。改完后 PoC 6/6 ground-truth 全对。
- **重要**：静态判定只是**上界 + 优先级**，真实"可跑"仍要 `trackA_filter` 逐 repo 实跑确认。
- **战略结论**：Python 在我们池子里恰是**最不友好**的生态（热门 Python 仓库都是科学计算/编译型）；**Go(109 题/31 hard)是最大最干净的 Track A 池**，其次 TS/Rust。建 Go harness 的性价比 > 死磕 Python。
- **代码/数据**：`scripts/env_friendliness.py`、`data/trackA/repo_env.json`、`data/trackA/tasks_env.jsonl`。

### P21. 实测:静态 Python-ready 66 → 实跑只 9 条可跑（最大损失可回收）
- **实测漏斗**（66 个 Python track_a_ready 任务跑 `trackA_filter`）：survives=**9**（hard 4/medium 1/easy 4），来自 4 个仓库（ansible/metrics-service、conan-io/conan、django、pytest-bdd）。
- **最大损失 = `no_test_file_at_start`（25 条）**：这些 PR 只**新增**测试文件，start_sha 没有对应测试当 baseline。但这是**筛子局限**——只跑了 PR 改过的测试文件，没去跑仓库里**已有的相关测试模块**。
- **可回收**：改进 `trackA_filter`，对改动的源码文件去找/跑**已有对应测试模块**（如 `test_<name>.py` 或同目录 tests/），而不仅是 PR 触碰的测试 → 预计能救回相当一部分这 25 条。
- **次要损失**：env 13（友好仓库仍有 pip 装失败，静态筛不完美）、baseline_no_pass 12、complete 但 coverage 没覆盖 5。
- **结论**：可跑 Track A 极稀缺，且高度集中在少数干净仓库；**把 ansible/conan 这类金矿仓库配好** + 回收 no_test_file，是 Python 侧最划算的扩量方式。Go(109) 仍是更大的池。
- **数据**：`data/trackA/py_ready_funnel.jsonl`。

### P22. 「只 9 条可跑」是筛子地板，不是真实上限（三个可修硬伤）
- **现象**：66 Python-ready 实跑只 9 survives，但逐条看死因，多数可回收。
- **硬伤1：只跑 PR 改过的测试文件** → 25 条死于 `no_test_file_at_start`（PR 只新增测试）。应改成：对改动的源码文件，去跑仓库里**已有的对应测试模块**（`test_<name>.py`/同级 tests/）。
- **硬伤2：venv 用了过新的 Python（本机 `python3`=3.14.3）** → 一批包 `requires <3.x` / `build_ext` 崩。**走 uv 的 ansible 用 3.12 就成功**。修法：venv 也强制用 3.11/3.12，或全程用 uv。
- **硬伤3：coverage 偶发 `json not produced` 被当成"没覆盖"** → 4 条有 p2p=26/16/6 稳定测试的本可用 case 被毙。修法：coverage 失败时不要一票否决（可降级为"有 PASS_TO_PASS 即算可跑、coverage 仅作加分"）。
- **结论**：保守修完三点，Python 可跑数预计 9 → 25~40+。报"可跑池大小"前必须先修这些，否则严重低估。

### P22 续：修复后 9 → 19 可跑（已验证）
- 三个修复（跑已有测试模块 / uv py3.12 / coverage 改加分项）实测把 Python 可跑数 **9 → 19**（hard 7、medium 1、easy 11），其中 11 条改动行被稳定测试覆盖（更强）。横跨 10 个干净仓库（metrics-service 6、conan 3、pytest 2、pytest-bdd 2…）。
- **剩余 env-fail（15）是新一类**：① setuptools `package discovery` 失败（不该 `pip install -e .`，应"装依赖 + 从源码跑测试"）；② monorepo 子目录（包不在根，aws-*/kubeflow/langchain-aws）。可回收但边际递减。
- **里程碑**：已有 19 条真·端到端可跑的 Python Track A 任务（7 hard）。下一步价值最高的是用它们跑通完整评测闭环（模型 patch → PASS_TO_PASS 正确性门 + AI judge）。
- **数据**：`data/trackA/py_ready_funnel_v2.jsonl`。

### P23. 端到端评测闭环跑通（里程碑）+ solver「重写整文件」会截断
- **里程碑**：`scripts/evaluate_e2e.py` 在 2 条 runnable hard 上跑通完整闭环（gate0 apply → gate1 PASS_TO_PASS 正确性 → gate2 scope → gate3 judge → Resolved@1）。
  - **Sanity 全过**：golden→Resolved=True、empty→Resolved=False（两条都对），证明评测器可信。
  - **正确性门抓到真实回归**：conan 模型 patch 改坏 5 个 PASS_TO_PASS 测试 → Resolved=False；judge 也独立识破。
  - **judge 给真活打分**：metrics-service 模型正确改成 dict+`extra=` → Resolved=True。claude Resolved@1=1/2。
- **坑：让模型"输出整个新文件"在大文件上会截断**（conan `environment.py` 被 claude 截断、删了无关函数）。
  - **修法**：solver 协议应改成**面向区域的 diff/patch** 或函数级编辑，并校验"未删无关代码"；正确性门当前能兜底（截断必挂测试），但会把"输出格式问题"和"真不会做"混在一起。
- **结论**：ReviewBench-Maintainability 全链路（挖掘→分类→难度→环境验证→PASS_TO_PASS oracle→模型求解→correctness+scope+judge→Resolved@1）已端到端验证可行。
- **数据**：`data/trackA/e2e_results.json`。

### P24. Leaderboard：golden 锚点的两个坑（必须修，否则排行不公平）
- **坑1（崩溃）**：写候选文件前没 `mkdir parents` → golden 应用含**新建文件**（如 `.claude/commands/task-review.md`，start_sha 下父目录不存在）时 `write_text` 崩溃 → golden 整条 gate0 失败。模型只改已存在的锚定文件不受影响 → **排行假性地让模型赢过 golden**。修法：写前 `(repo_dir/f).parent.mkdir(parents=True, exist_ok=True)`。
- **坑2（scope 不公平）**：golden = 整个 PR 的源码 delta（常多文件/大改），会撞 scope 行数上限 → golden 误判 not resolved。但 scope 门是用来**防模型乱改/过度编辑**的。修法：**golden 豁免 scope 门**（只过 correctness+judge），模型才受 scope 约束。
- **教训**：跑 leaderboard 必须先确认 **golden≈满分、empty≈0**；否则评测器本身有 bug。
- **数据**：`data/trackA/leaderboard.json`。

### P26. 测试文件类任务必须剔除出 Track A（否则 golden 假性失败、模型假性反超）
- **现象**：首个 leaderboard 里模型(84%)反超人类 golden(74%)——异常。
- **根因**：5 条任务的 concern 是关于**测试文件**的（删测试里 unused import、去重 fixture、加 `@needs_pydanticv2`）。我的 golden 用 `source_changed`（**排除测试文件**），于是只应用了 PR 捆绑的非测试改动（CI/docs），**没应用真正在测试文件里的修法** → golden 被 judge 判 not addressed；而模型直接编辑锚定的测试文件反而对了。
- **更深**：测试文件类任务里，"被改的测试"既是**任务对象**又是**正确性 oracle**，循环依赖，本就不适合 Track A（P17 的延伸）。
- **修法**：选 Track A 任务时**剔除 `is_test_path(locality.path)` 的任务**。
- **效果**：剔除后 **golden=14/14(100%)、empty=0/14(0%)**，模型落在 71-86%，排行干净可信。
- **首个干净 leaderboard（14 源码类任务，judge=claude-opus-4.7）**：golden 100% / claude-opus-4.8 86% / gpt-5.5 79% / gemini-3.1-pro 71% / empty 0%。hard(5)：gpt-5.5 与 gemini 4/5、claude 3/5、golden 5/5。
- **注意**：N 小(14/5 hard)仅供方向性参考；judge=claude 对 claude solver 可能有偏置，需换 judge 验证。
- **数据**：`data/trackA/leaderboard.json`。

### P27. 「bench 太简单」诊断：是可跑样本 easy 偏多 + 太小，不是判定放水
- **数据**：14 条干净任务里 8 easy/1 med/5 hard。严格判定(只算 `yes`)几乎不变（golden 13、gpt-5.5 10、claude-4.8 10、gemini 9），partial 放水每模型仅 1-2 条 → **不是 judge 太松**。
- **hard 子集(5,严格)有区分度**：golden 5/5、gpt-5.5 4/5、claude-4.8 3/5、gemini 4/5。
- **根因（流水线系统性偏易）**：① 单评论+单区域 locality 排除了架构级重构；② 要干净 PASS_TO_PASS → 偏好小的行为保持改动；③ env-friendly 选了简单仓库。真正难的 127 hard 恰恰最难做成可跑任务（固有矛盾）。
- **让它变难**：① headline 改 **hard-only**；② 把可跑 hard 扩到 30-50+（Go harness=31 hard、编译仓库上 Docker、回收 env）；③ 放开 locality 收多文件架构任务；④ 判定收紧（strict yes + rubric：完全解决/不引入新问题/不过度编辑）。

### P28. Go harness 成功：Track A 可跑 hard 从 5 → 18（Go 是最佳生态）
- **结果**：22 条 Go 源码类 hard → **13 可跑（57%，10 覆盖）**，跨 grpc-go(4)、telegraf(2)、prometheus、cilium、kubernetes、terraform-provider-aws 等。
- **关键优势**：`go test ./pkg/` 只编译**改动文件所在的那个包**，所以连 **kubernetes/cilium/terraform-provider-aws 这种巨型仓库都跑通**（Python 在科学计算重仓库上全军覆没，Go 没有这个问题）。无 pip 地狱、go mod 自动解析依赖。
- **组合可跑 hard**：Python 5 + Go 13 = **18**（向 hard-only benchmark 迈进一大步）。
- **注意**：Go `_test.go` 锚定任务用 `is_go_test()` 单独剔除（P26 的 Go 版；通用 `is_test_path` 只认 Python 的 `_test.py`/`tests/`）。
- **代码/数据**：`scripts/trackA_go.py`、`data/trackA/go_funnel.jsonl`。

### P29. golden 不是保证的 100% 天花板：人类有时没按 reviewer 字面建议改
- **现象**：grpc-go 某 hard 任务 golden 被 judge 判 not addressed——reviewer 问"为什么不合并成一个锁?",而真实 PR 重构成了 `oobState` struct 但**仍保留两个锁**(用了不同方案/只部分采纳)。
- **结论**：golden = 真实 PR 的修法,**未必字面满足评论的建议**(tokio 现象的重现)。所以 golden 在严格 judge 下 < 100% 是**真实的**,不是 bug。
- **影响**：① golden 不能当作保证满分锚点;② judge rubric 需区分"字面照做 vs 解决了本质 concern"——当前是严格字面;③ 这类"评论建议没被采纳"的任务本身可能是弱题。
- **已修(让 golden 更公平)**：judge 只看**锚定文件的 diff**(`git diff -- <path>`),避免 golden 的多文件 PR 改动淹没相关变更。但上面这条是真实分歧,改了仍 not addressed。
- **leaderboard 读法**：empty=0 是关键 sanity;golden 作参考(可能 <100%);模型间相对排名才是主信号。

### P30. Hard leaderboard 暴露两个必须先修的有效性 blocker（flaky 测试 + judge 严格）
- **现象**：Go hard 上 golden 只 2/7,模型 3-5/7（模型"反超人类"再现）。逐条拆 golden 失败：
  - **~3 条 correctness=False 但 maint=yes** → **flaky 测试**。这些是 grpc-go 等**并发重仓库**,锁/OOB 负载均衡测试有时序抖动;golden 的源码 = p2p 基线本身,却在复跑时挂了个测试 → 正确性门不可靠。
  - **~3 条 maint=no/partial** → **judge 严格**:人类 PR 没字面照做 reviewer 的建议（grpc-go 保留两个锁、telegraf 保留指针、prometheus 用闭包而非独立函数）。
- **结论**：当前 hard leaderboard **数字不可信**,不能当真排行。"模型反超 golden"主要是 flaky + judge 假象。
- **必修**：① **flaky 检测**——baseline 跑 2-3 次,丢掉不稳定的测试出 PASS_TO_PASS;correctness 也复跑取稳定结果。② **judge rubric**——区分"字面照做 vs 解决本质 concern",并剔除"评论建议根本没被采纳"的弱题。
- **价值**：宁可现在发现这两个 blocker,也不要发一个被 flaky/judge 污染的噪声榜。
- **数据**：`data/trackA/leaderboard_go_hard.json`。

### P31. 修 flaky：稳定测试过滤 + 「golden 必须过自己的正确性门」做任务有效性筛
- **做法**：① PASS_TO_PASS 现在要求测试在 start 和 gold 各跑 **3 次都 PASS**(排除 flaky);② 正确性门对失败测试**复跑最多 3 次**容错;③ 修了 `git_show` 经 `ta.run` 多带 `\n+stderr` 的隐患。
- **但 grpc-go 病态 flaky**:并发/OOB 测试抖动率高,3 次过滤 + 3 次容错仍然让 golden 正确性挂 → 重试治不了。
- **principled 修法**:**一个任务只有当 golden 自己能过正确性门才算有效**(golden 就是 target,过不了说明 oracle 坏了/太flaky)→ 自动剔除 grpc-go 这类。leaderboard 只在"golden 正确性通过"的 valid 任务上算。
- **意义**:这是 SWE-bench 式的"逐任务验证 golden"——比逐测试斗 flaky 更稳、更可辩护。

### P31 续：flaky 修复后的干净 hard-only leaderboard
- **有效性筛生效**：16 个可跑 hard(5 Py + ~11 Go）中，golden-必须过正确性门 筛掉 8 个 flaky/坏 oracle（绝大多数是 grpc-go 并发抖动）→ 剩 **9 个有效 hard**（5 Py + 4 Go）。
- **干净 hard-only 排行（judge=claude-opus-4.7）**：golden 8/9(89%) / gpt-5.5 7/9(78%) / gemini-3.1-pro 7/9(78%) / claude-opus-4.8 6/9(67%) / empty 0/9(0%)。
- **健康信号**：golden≈天花板（唯一失分是"人类没字面照做评论建议"的题）、empty=地板、模型 67-78% 有区分度。
- **代价/教训**：grpc-go 这类并发重仓库**几乎全军覆没于 flaky**——Go 选题要避开并发/时序敏感包，否则有效率很低。
- **数据**：`data/trackA/leaderboard_go_hard.json`、`leaderboard_py_hard.json`。

### P33. Docker 化（SWE-bench 式）：能解锁重仓库,但历史 commit 需要 per-instance 环境锁
- **建成**：`scripts/docker_harness.py`——按生态生成 Dockerfile（python-slim + 科学计算构建依赖 / golang）、clone、装依赖、编译一次。
- **关键验证**：**scikit-learn 在容器内成功 build + 跑测试**（`test_calibration.py` 在镜像 HEAD 上 103 passed）——这正是直接 harness 完全建不起来的重仓库。**证明 Docker 解锁了 sklearn/scipy 这类（含 13 条 sklearn hard）。**
- **但**：checkout 到具体 `start_sha`(历史 commit)后重建失败——老 commit 的 `setup.py` 要 `pkg_resources`(setuptools≥81 已删)、且要**该 commit 的 build-system requires**（特定 numpy/cython/meson 版本）。单个 per-repo 镜像只对接近构建点的 commit 有效。
- **结论/下一步**：这正是 SWE-bench 用 **per-instance 环境 spec**（每个任务按其 commit 的 `pyproject [build-system] requires` 钉死构建依赖）解决的——是已知但工作量大的工程。路径 = per-repo 基础镜像 + per-task 读 commit build-requires 装精确版本。
- **磁盘注意**：重镜像 1GB+/个,本机 `/` 已 94%；批量构建前要清理。

### P34. Docker per-instance oracle 跑通（近期 commit）：完整 PASS_TO_PASS 在容器内端到端成功
- **建成**：`scripts/docker_harness.py` 的 `container_oracle_py()`——在 per-repo 镜像里 checkout 精确 `start_sha` → meson editable 重建（`pip install -e . --no-build-isolation`）→ flaky-aware 3× baseline → 应用 source-gold → 重建 → gold 跑 → PASS_TO_PASS = start 稳定且 gold 通过。
- **验证**：`sklearn/tree/_export.py` 任务全程跑通：start 10 稳定 / gold 收集 10 / **PASS_TO_PASS = 10**。`sklearn/linear_model/_logistic.py` 的 start 侧也正确解析出 267 稳定测试。
- **关键结论**：P33 担心的"老 commit 装不上"对**近期 commit（meson 时代、py3.12 兼容）不成立**——绝大多数 hard 任务来自 2024-2026 的 PR，用一个 py3.12 per-repo 镜像 + 精确 checkout + 重建即可。只有 pre-meson/老 numpy 的少数老 commit 才需要 era 匹配的 Python。
- **CLI**：`python scripts/docker_harness.py --repo <r> --oracle-task <task.json>`（test 文件用记录的 `test_change_paths` 自动推导）。

### P35. pytest 输出解析两个隐形坑：ANSI 颜色码 + 长参数名 80 列换行
- **坑1（致命）**：即使管道（非 TTY），pytest 仍可能输出 `\x1b[32mPASSED\x1b[0m`；正则 `nodeid\s+(PASSED)` 因 nodeid 与 PASSED 之间夹着 ANSI 转义而**全部不匹配 → 解析出 0**。表现：测试明明 288 passed，oracle 却报 stable_pass=0。
- **坑2**：非 TTY 时 pytest 按 80 列折行，长 parametrize nodeid（如 `[csr_matrix]`）的状态被换到下一行 → 漏解析（267 vs 288 的缺口）。
- **修法**：① pytest 加 `--color=no -o console_output_style=classic`，解析前再 `re.sub(r'\x1b\[[0-9;]*m','',out)` 兜底；② 容器内 `export COLUMNS=300` 防折行。正则放宽为 `^(\S+::\S+?)\s+(PASSED|FAILED|ERROR)\b`。

### P36. gold 文件用记录的 source_change_paths，绝不用 two-dot `git diff start target`
- **坑**：当 `start_sha` 与 `target_sha` 在历史上**相距很远**（PR 基于旧 main、合并很晚 / 经过 rebase），`git diff --name-only start target`（two-dot）会包含**数百个中间 commit 的无关文件**。把它们全 checkout 到 target → Frankenstein 状态 → 测试收集 `ImportError`（如 `_logistic.py` 任务报 `cannot import name get_callback_manager`，因 target 版文件引用了中间 commit 才加的新 API）。
- **修法**：gold 只应用任务记录里的 `correctness_oracle_proxy.source_change_paths`（PR 自身的非测试源码改动，通常 1-2 个文件），不要用 two-dot diff 现算。
- **更深的有效性问题**：远距 start/target 的任务，即便只 checkout PR 的源文件，target 版仍可能引用中间 commit 的新 API → source-only gold 根本装不干净。**这类任务由"golden 必须过自己正确性门"的有效性筛自动剔除**（P31），是正确行为而非 harness bug。选 sklearn 题要优先 start/target 接近、源文件自包含（纯 Python、单文件）的。

### P37. Docker leaderboard 接入成功：sklearn hard 题进榜（含选题/环境的关键发现）
- **建成**：`scripts/evaluate_docker.py`——每个任务起**一个常驻容器**（`docker run -d ... sleep infinity`），在 `start_sha` 重建一次后，跨 solver 复用（纯 Python 文件被 meson-editable 实时加载，改一处不用重编译 → 快）。LLM（solver+judge）在宿主机，容器只跑测试。复用 `evaluate_e2e` 的 4 门 + `judge_prototype` 的 judge + 「golden 必须过正确性门」有效性筛。
- **干净 leaderboard（4 个有效近期 sklearn hard，judge=claude-opus-4.7）**：golden 3/4 / empty 0/4 / gpt-5.5 3/4 / gemini-3.1-pro 3/4 / claude-opus-4.8 1/4。其中 `decision_boundary.py`（PASS_TO_PASS=57）、`_discretization.py`（86）是强 oracle 好题。
- **关键发现 1（选题=commit 年代）**：sklearn 约 **2024 年中**才切 meson-python；**2024 前的 commit 是 setup.py 时代**，在 py3.12 镜像里 `pip install -e .` 报 `ModuleNotFoundError: pkg_resources`（setuptools≥81 已删）→ 老题 baseline 建不起来。**Docker 解锁的是近期（2024+/meson 时代）的 hard 题**；老题要 era 匹配的 Python/setuptools。
- **关键发现 2（测试依赖要进镜像）**：plot 题需 `matplotlib`、dataframe 题需 `pandas`——基础镜像没装会 `baseline_no_pass`（测试 import 失败）。已把 `matplotlib pandas pillow` 加进 `PY_SCI_DOCKERFILE` 并给现有镜像打 overlay 层。
- **关键发现 3（弱题：fix 不在 anchored 文件）**：`_repr_html/params.py`（PASS_TO_PASS=1）golden 也 maint=no——该 PR 的维护性改动落在**别的文件**，anchored-file-only diff 自然不 address concern。**应把有效性筛从「golden 过正确性」收紧到「golden 能 RESOLVE」**，自动剔除这类（顺便也淘汰 PASS_TO_PASS 过小的题）。
- **关键发现 4（apply 门对 SEARCH/REPLACE 漂移敏感）**：claude-opus-4.8 的低分主要是**偶发 apply=False**——模型的 SEARCH 块跟文件**尾随空白/内容微漂**导致 exact 匹配失败（非确定性）。已给 `evaluate_e2e.apply_search_replace` 加 **rstrip 容差的行级回退**（类似 aider 的模糊匹配）→ 更公平、降噪，惠及所有 leaderboard。

### P38. 公平对待 apply 门：solver 重采样到能 apply（claude 的低分一半是方差）
- **现象**：claude-opus-4.8 在 SEARCH/REPLACE 协议下**偶发 apply=False**——同一题不同采样有时能精确匹配、有时把 sklearn 的长警告字符串**重排/改写**导致 exact + rstrip 容差都匹配不上（非确定性）。单样本 Resolved@1 + N=4 → 方差极大，把 capability 和 edit-protocol 运气混在一起。
- **修法**：model solver **重采样最多 3 次直到 edit 能 apply**（`evaluate_docker.make_candidate`）。这只影响"能否产出可用 edit"，不碰 correctness/scope/judge。
- **效果（apply-retry 后的公平分）**：claude 1/4 → **2/4**——`decision_boundary` 之前是 apply=False，重采样后 apply=True 且 RESOLVED。剩两个没过：`params.py`（弱题，golden 也 maint=no）+ `_discretization`（连试 3 次都 apply=False = claude 对该文件**稳定地**重排长行，是真实的指令精确度短板，不是方差）。
- **干净 3 题（剔除弱题 params.py）**：golden/gpt-5.5/gemini 3/3、**claude 2/3**、empty 0/3。
- **结论**：① apply-retry 该成为所有 leaderboard 的标配（已在 docker 评测器落地，evaluate_e2e 可同样加）；② claude 仍稍弱于 gpt-5.5/gemini，但差距远没有 1/4 vs 3/4 那么大；③ 再次印证应把弱题（golden 不 RESOLVE）筛掉。

### P39. Docker 扩仓库结果：解锁 19 个干净编译型 Python 任务（sklearn+scipy），并踩平 per-repo 构建坑
- **跨仓库 Docker 榜（CLEAN=golden 能 RESOLVE，19 题：16 sklearn + 3 scipy，judge=claude-opus-4.7）**：golden 19/19(100%) / gemini-3.1-pro 18/19(94.7%) / gpt-5.5 17/19(89.5%) / claude-opus-4.8 14/19(73.7%) / empty 0/19(0%)。golden=天花板、empty=地板、模型 74-95% 有区分度——健康。
- **per-repo（scored/valid/clean/setup_fail）**：scikit-learn 18/18/16/4、scipy 3/3/3/8、matplotlib 0/0/0/7。
- **编译型仓库的 per-repo 构建坑（全部已固化进 `PY_SCI_DOCKERFILE` / `evaluate_docker`）**：
  - **sklearn**：meson-editable，近期 commit 直接装；老 commit（<2024 setup.py 时代）装不上 → 只取近期题。
  - **scipy**：需 `pythran`+`pybind11`（meson 找不到 → 报 Program pythran not found）、**git 子模块** `array_api_compat`/`xsf`/`unuran`（blob-filtered clone 不带，需 `git submodule update --init --recursive`）、测试需 `hypothesis`。修齐后 build+test 通。
  - **matplotlib**：需 `setuptools_scm`（`--no-build-isolation` 下要预装）+ `pybind11` 头（meson 经 pkg-config 找不到 → 烤进 `CPLUS_INCLUDE_PATH`）。HEAD 能 import，但**历史 commit 的 per-task 重建仍挂**（baseline_no_pass × 7）→ 暂未产出有效题。
- **scipy 慢/掉**：`signal`/`stats`/`sparse` 测试文件巨大（test_signaltools 收集上万、flaky 上万）→ 3×baseline+3×gold+5 solver 重跑非常慢，11 题里 8 个 setup 超时/失败。重仓库要选轻量测试模块的题。
- **总账**：Docker 把直接 harness **完全建不起来**的编译型 Python 捞回 **19 个干净有效 hard/med/easy 题**（之前是 0）。加上原 9 个 Go+轻 Python = 约 28 个有效题。瓶颈从"语言/能否 build"转移到"重仓库测试太重太慢"。

### P40. 端到端扩规模跑通：新挖的 Go hard 进榜（mine→prefilter→score→run 全链路验证）
- **第一轮挖掘（30 新仓库）→ 788 high-priority → 难度打分 → 235 hard（110 Go + 98 Python）**。原 benchmark 仅 127 hard，一轮就近乎翻三倍。
- **取 30 个非测试 Go hard 过 host harness（evaluate_e2e，5 solver 一遍过）**：11 个评出、**5 个干净有效**(golden RESOLVE)，19 个 setup 挂。
- **新 Go hard 干净榜（5 题，judge=claude-opus-4.7）**：golden 5/5 / gpt-5.5 4/5 / gemini-3.1-pro 4/5 / **claude-opus-4.8 1/5** / empty 0/5。有效仓库：thanos / teleport / opa-control-plane / traefik-mesh。
- **坑（已知再确认）**：重 Go 仓库(etcd/vitess/temporal)**baseline_no_pass 占 19/30(63%)**——大仓库测试建不起来/far-apart sha/flaky。选 Go 题要避开巨型仓库,挑中小且测试轻的(opa/thanos 子模块/teleport 部分包)。
- **claude 在 Go 上持续偏低**(这批 1/5,之前 sklearn 也偏低)——已带 apply-retry,所以是真实的 correctness/judge 失分倾向,值得后续小样本核实。
- **工程基建**:Go clone + GOCACHE/GOMODCACHE 全重定向到 `/datadisk/peiyang`(根分区 95% 满,保护 `/`)。
- **结论**:整条扩规模流水线(挖掘→prefilter→难度打分→跑 harness→榜)端到端打通,新题能稳定增量进榜。

### P42. 新 Python hard 进榜：7 个干净有效；claude 在 Python 上不弱（Go 才是它的短板）
- **66 个非测试新 Python hard 过直接 host harness**：12 个评出、**7 个干净有效**(golden RESOLVE)，54 个 setup 挂(82%)。
- **新 Python hard 干净榜（7 题，judge=claude-opus-4.7）**：golden 7/7 / gpt-5.5 7/7 / gemini-3.1-pro 7/7 / **claude-opus-4.8 6/7** / empty 0/7。有效仓库：pydantic-ai / scrapy / textual / w3lib / optuna-mcp。
- **重要观察**：claude 在 Python 上 6/7,但在 Go 上只有 1/5(P40)——**claude 的短板是 Go 特定的**(可能 SEARCH/REPLACE 对 Go 代码风格更易漂移,或 Go 维护性判断更难),不是通用能力问题。gpt-5.5/gemini 两边都强。
- **直接 harness 对新 Python 仓库流失率高(82%)**：① 编译型(scikit-image/spaCy/thinc/astropy 用 Cython/C)直接装不上 → 要 Docker;② 重依赖(mlflow/optuna/plotly)uv venv 装依赖/跑测试失败。**这些编译型 Python 仓库是下一步 Docker 化的目标**(和 sklearn/scipy 一样)。
- **基建**：Python clone + uv/pip 缓存导到 `/datadisk/peiyang`,保护根盘。
- **累计 leaderboard 新增**:5 Go(P40)+ 7 Python = **12 个干净有效新 hard 题**进榜,来自全新项目。

### P43. 编译型 Python 的 Docker 化：逐 commit Cython 构建是硬骨头(17 题只捞回 2)
- **6 个编译型 Python 仓库(17 hard)过 Docker**:只有 **plotly.py 出 2 个干净有效题**(纯 Python,可直接 pip 装),其余 **15 题全 setup 挂**。
- **干净榜(2 题)**:golden / gpt-5.5 / claude-opus-4.8 / gemini 全 2/2,empty 0/2。
- **挂的原因(逐 commit 构建)**:scikit-image/astropy/thinc/spaCy/bokeh 是真 Cython/C 扩展。镜像在 HEAD 能 build+import,但 **checkout 到任务的历史 start_sha 后重建失败**——典型 `"you will need numpy"`(该 commit 的 `[build-system] requires` 钉死了特定 numpy/cython 版本,跟镜像里装的不匹配)。
- **试过的回退无效**:`--no-build-isolation`(复用镜像 deps)失败 → 隔离构建(装该 commit 的 build deps)**两种都挂**。meson-editable 的 import 时重建也用不上隔离环境。
- **真正的解法(未做,工作量大)**:SWE-bench 式**逐 commit 环境重建**——读每个 commit 的 `[build-system] requires` + 匹配的 Python 版本,per-instance 钉死。对 17 个题不划算。
- **结论/边界**:**Docker 能解锁"任务 commit 区间内工具链稳定"的仓库**(sklearn/scipy 近期 meson commit、plotly 纯 Python);**逐 commit 变化的 Cython 构建仍是硬骨头**,ROI 低。编译型 Python 的增量到此(sklearn 16 + scipy 3 + plotly 2 = 21 个干净有效)。

### P44. 新 Go hard 全量跑完(b1+b2，70 题）：9 个干净有效，claude 的 Go 短板在 N=9 上坐实
- **70 个非测试新 Go hard 全过 host harness**：18 个评出、**9 个干净有效**(golden RESOLVE)，~74% 在 setup 流失（重 Go 仓库 baseline_no_pass）。
- **合并干净榜（9 题，judge=claude-opus-4.7）**：golden 9/9 / gpt-5.5 8/9(89%) / gemini-3.1-pro 7/9(78%) / **claude-opus-4.8 4/9(44%)** / empty 0/9。有效仓库：etcd / teleport / thanos / opa-control-plane / traefik / traefik-mesh。
- **claude 的 Go 短板坐实**：Go 4/9(44%) vs Python 6/7(86%)——同一模型跨语言差 40+ 个百分点。gpt-5.5/gemini 两边都强。**这是 ReviewBench 的一个有意思的发现：维护性修复能力有显著的语言依赖性，且因模型而异。**
- **Go 选题教训（再确认）**：重仓库(etcd/vitess/temporal/nats-server)setup 慢且常挂；有效产出集中在中等大小、测试包独立的仓库(thanos 子包、opa、teleport 部分包、traefik)。
- **累计 leaderboard**：原 ~9 + Docker(sklearn16+scipy3+plotly2=21) + 新 Go 9 + 新 Python 7 = **约 46 个干净有效 hard 题**，覆盖 ~15 个仓库、4 大类生态。

### P45. `CLEAN` 不等于实质代码质量提升
- **现象**：旧 28 道 CLEAN 里会混入功能修复、纯 style、机械 import/rename，以及没有完整质量证据的题。
- **根因**：旧门只检查 golden correctness 和 reviewer concern addressed，没有强制原代码 smell、实质改善、维护性主目标、本质难度或无质量回退。
- **解决**：新增 `quality-improvement/v1`。每题必须有完整 before/local/full patch，两个不同模型一致严格通过，score≥4、confidence≥0.8、难度至少 medium，不接受 partial。
- **影响**：1,763 道完整证据题最初只有 46 道双模型通过，说明旧 CLEAN 不能作为最终发布口径。
- **代码**：`quality_gate.py`、`evidence.py`、`release.py`。

### P46. 环境失败不能当成题目失败
- **现象**：双模型质量通过题最初大量死在 env、baseline、P2P 或 coverage。
- **根因**：历史仓库需要精确 Python/Go、nested project root、SCM tags、Cython/Meson、Fortran、sidecar、build tags 或专用容器。
- **解决**：所有非结构性失败进入持久 recovery queue。第二轮 39 道 recoverable 任务最终 39/39 回收。
- **影响**：严格池从 21 增到 60。环境工程是数据构建的一部分，不是可选清理。
- **代码**：`recovery.py`、`validate_oracles.py`、`trackA_filter.py`、`trackA_go.py`。

### P47. Session-only oracle 也会锁死 golden 实现
- **现象**：CPython 模型补丁保持公开行为，但因 private `_translate` helper 参数数量不同而失败。
- **根因**：oracle 直接调用 golden 私有 helper。随后审计又发现 AST 抽取、MRO、源码字符串、内部调用次数和替代签名等 12 类同类问题。
- **解决**：13 道 oracle 改为完整模块或公开入口的可观察行为断言，不检查私有 helper 名、签名、源码位置、继承布局或调用序列。全部重新跑 start/golden 各 3 次并确认 changed-old coverage。
- **影响**：避免排行榜惩罚合法等价重构。修复记录进入 `oracles.jsonl` 的 `oracle_independence_fix`。
- **数据**：`data/releases/v1/oracles.jsonl`。

### P48. Gemini 的空 content 可能只是 reasoning 用尽输出预算
- **现象**：Gemini 3.5 Flash 返回 `content=""`，容易误判为模型没有答案。
- **根因**：`max_tokens` 同时容纳 reasoning 和正文。32 token 探针里 reasoning 用掉 27；复杂任务的 16K 调用也可能被 reasoning 全部耗尽。
- **解决**：检查 `finish_reason` 和 `reasoning_tokens`。常规评测用 16K；XRMocap 三次均因预算截断后，单题用 32K 恢复。
- **影响**：Gemini 最终可应用补丁从 96 增到 97。不能把 token-budget truncation 记成能力失败。
- **代码**：`generate_release_patches.py`。

### P49. 长时间 GPT-5.5 批量调用必须刷新 Copilot token
- **现象**：100 题生成到第 53 题后，后续 48 题全部 HTTP 401。
- **根因**：缓存的 IDE token 在长任务中到期。
- **解决**：responses 请求遇到 401/403 时重新调用本机 `/token`，只续跑失败项。
- **影响**：GPT-5.5 从 52/100 恢复到 98/100；剩余 2 道是真实 SEARCH/REPLACE 不可应用。
- **代码**：`generate_release_patches.py`。

### P50. 异构 oracle 执行器必须先归一化再汇总
- **现象**：12 个 correctness 分片产生了 `models` list、`models` mapping、`results`、`model_results`、flat per-model 等多种 schema。第一次汇总把三个完整分片误记为 `oracle_replayed=false`。
- **根因**：环境 recovery 由不同执行器完成，字段名包括 `patch_apply`、`patch_applied`、`patch_apply_ok`、`apply_ok` 以及多种 correctness 表达。
- **解决**：`summarize_release_evaluation.py` 统一解析所有已出现 schema，并用 8 个单测锁定归一化行为。汇总前必须验证 300 个唯一 task-model 对。
- **影响**：纠正后的 Resolved@1 为 Gemini 66、Claude 63、GPT-5.5 61，错误汇总会低估约 20 题。

### P51. 单文件 solver scope 会低估多文件重构
- **现象**：部分题的自然修法需要在另一个文件定义 helper 或同时改两个调用点，模型只看到锚点文件或 100K excerpt。
- **根因**：当前 solver 为防止 PR bundled change 和答案泄露，限定锚点生产文件。
- **影响**：scope 更干净，但多文件 modularity/API 题可能被系统性低估。TiDB 57553 的模型多块编辑就是典型例子。
- **后续**：v2 应提供经过 concern 隔离的多文件 context 和多文件 patch 协议，同时维持 file allowlist。

### P52. SEARCH/REPLACE 失败不是 Git patch 应用失败
- **现象**：页面显示部分模型“补丁无法应用”，容易理解成 `git apply` 无法处理模型生成的 `.patch`。
- **核对**：306 题三模型基线实际生成了 911 个 patch，correctness 阶段 911/911 均可由 Git 应用。7 个失败都发生在 patch 文件生成之前，原因是模型给出的 SEARCH 文本无法在 `start_sha` 源码中定位。
- **根因**：旧流程先用自定义 SEARCH/REPLACE 协议构造 after 文件，再由 Git 计算 diff。Git 只负责第二步，无法修复第一步的文本定位失败。
- **解决**：保留旧结果作为 non-agentic baseline，新增 `agentic-line-edit/v1`。模型通过 `read`、`find`、`edit`、`finish` 操作受限 worktree；host 按行应用编辑，最终统一运行 `git diff --binary`。
- **公平性**：协议变化会影响模型的观察和行动方式，因此必须重跑全部 306 题及三个模型，不能只回收旧协议的 7 个失败。
- **代码**：`scripts/generate_agentic_patches.py`、`tests/test_generate_agentic_patches.py`。

### P53. JSON agent 会回显 tool transcript，首个或末个 action 都不一定是下一步
- **现象**：Gemini 在 agentic smoke 中能编辑，但全量任务大量连续 32 轮只做同一批 `read/find`，没有任何 edit。
- **根因**：模型有时返回整个 transcript continuation，其中包含多个 `assistant_action/tool_result` 对。旧解析器从响应中取第一个有效 JSON action，实际取到的是最早的历史 action，而真正的下一步 action位于响应末尾。
- **二次现象**：部分响应不只回显旧 transcript，还一次生成 `edit → 伪造 tool result → finish`。单纯取最后 action 会跳过 edit，host 随后拒绝无改动 finish。
- **解决**：先接受严格的单对象响应；否则按原始文本位置提取所有平衡 JSON 对象并解包 wrapper。常规选择最后有效 action；若当前 worktree 尚无改动、最后 action 是 `finish` 且同一响应含 `edit`，优先执行最后一个 edit。多对象响应记录 object count 和 raw tail。
- **验证**：同一 DataDog 任务从旧解析器的 32 轮纯读取失败，恢复为 17 轮完成编辑并由 Git 生成 patch。
- **影响**：旧 Gemini 产物整体归档为无效 parser run，正式 Gemini 结果从头生成。后续审计又恢复 gRPC 与 Nomad 两道 hard 的 adapter failure；两道 patch 均未通过维护性盲裁，排行榜分数不变。
- **代码**：`generate_agentic_patches.py::object_candidates`、`parse_action`。

### P54. Aggregate proxy failure 不能记成模型 generation failure
- **现象**：wave-003 的 Claude 在 8787 aggregate proxy 上恰好 95/190
  generated、95/190 failed，失败率远高于相邻 wave。
- **核对**：把同一 95 道失败任务送到已验证的 4141 direct endpoint，从原始
  source fresh-run，得到 94/95 generated；只剩 Timefold 一道真实耗尽 32 轮。
- **根因**：aggregate proxy 的路由/输出问题被上层包装成正常的 agent failure。
  失败形状看似符合 schema，但不是模型能力。
- **解决**：Claude generation 默认走 direct endpoint；发布前核对 endpoint
  identity、失败率和响应形状。异常批次必须同 task fresh A/B。
- **影响**：wave-003 Claude 从 43/190 Resolved 修正为 81/190。
- **原则**：transport、routing、empty content 和 protocol failure 必须与模型
  quality failure 分开报告。

### P55. 已耗尽轮次的 failed trace 不能普通 resume
- **现象**：对 32 轮无有效 edit 的失败结果运行 `--resume`，95 道立即原样失败，
  没有发生任何新调用。
- **根因**：resume 正确恢复 prior rounds；但 prior rounds 已等于
  `max_rounds`，剩余预算为零。
- **解决**：新增 `--restart-failed`。成功项仍复用；`status=failed` 的 terminal
  记录不再传入 `resume_agent_state`，而是从 start source fresh-run。
- **边界**：`status=in_progress` 的真实 checkpoint 仍继续恢复，不能一律丢弃。
- **公平性**：默认 wrapper 不启用。只重采样失败、保留成功会产生 success-only
  selection bias；必须有独立 infrastructure 证据、冻结 recovery IDs，并发布
  correction provenance。
- **代码**：`generate_agentic_patches.py::previous_result_for_retry`。

### P56. Kill `docker run --rm` 的父进程不会保证容器退出
- **现象**：停止 Rust lane runner 后，kcl LLVM14 容器继续运行 8 小时，
  测试二进制成为孤儿，task 状态长期停在 `recoverable_running`。
- **根因**：`--rm` 只在容器退出后删除；杀掉 client/wrapper 不等于停止 daemon
  管理的容器。
- **解决**：runner 使用可预测的唯一 container name；中断时按精确 name
  `docker rm -f`；禁止 `pkill`/全局清理。checkpoint 恢复前检查进程和容器双侧。
- **原则**：后台任务的 cleanup 要覆盖 process tree 和 external daemon object。

### P57. Shared image 不能自动摊薄每个重仓库的构建成本
- **假设**：一个 deal.II、LLVM 或 ROS2 镜像可以一次解锁 6–12 道任务。
- **结果**：假设不成立。共享依赖正是该 cluster 昂贵的原因；镜像能摊薄环境
  setup，却不能消除每个历史仓库的多小时 per-repo build。
- **第二个阻塞**：重度重构经常让 tests 与 production 一起演进。start tests
  对 source-only target production 无法编译，不是装齐依赖就能解决。
- **有效杠杆**：稳定 public API + bespoke behavior harness + 既有 package
  manager warm cache。BSMPT 用 Conan warm cache 和单目标 build 是成功例子。
- **原则**：建共享镜像前先证明至少 2–3 道任务只缺同一依赖栈，并检查 target
  production 是否仍与 start tests 兼容。

### P58. Judge 空响应是 transport error，不是 maintainability reject
- **现象**：12-worker 8787 judge 产生 110 个空 raw file，解析错误统一为
  `invalid addressed value: ''`。
- **根因**：高并发网关返回空 content。若 fail-closed 直接算 reject，会系统性
  压低三个 solver，且失败与代码质量无关。
- **解决**：保留 `judge_error`，降低并发，切换 direct endpoint，使用
  `--resume` 只重试错误项。最终所有可生成 candidate 都得到合法 rubric-v2
  verdict。
- **原则**：空 response、invalid JSON、schema error 不能 success-shaped
  fallback，也不能冒充真实 reject。

### P59. Structural 任务不补录会虚增 recoverable backlog
- **现象**：Oracle agent 证明 7 道任务没有 solution-independent public route，
  但为避免“伪造测试”没有写 plan，结果仍显示
  `recoverable_pending_curation`。
- **问题**：报表会把永远不可恢复的任务解释为尚未投入工程，错误估算剩余工作量。
- **解决**：能稳定构建的任务用 `structural_if_uncovered` 让 runner 正式记录
  `structural_*`；连 build 都到不了 structural branch 的任务另存证据，不能手改
  pass。
- **额外发现**：chainflip 删除两个公开 dispatchable，属于 feature removal，
  不是 behavior-preserving maintainability refactor，应反馈给上游质量门。

### P60. Correctness 工作目录不能直接作为汇总输入
- **现象**：wave-003 汇总报 duplicate correctness key。
- **根因**：工作目录除七个权威 lane JSONL 外，还有
  `csharp-before-exact-dependency-rerun.jsonl` 等备份。汇总器按 `*.jsonl`
  全部读取。
- **解决**：建立 `correctness-canonical/`，精确复制七个权威文件；零任务 lane
  写空 JSONL。generation/judgment 写内容哈希 completion marker；
  correctness marker 额外核对 start revision 和 candidate patch hash；
  judgment 绑定 task/generation fingerprint、rubric、judge model 和 context
  window；summary 从上游四门重建每一行并重算 leaderboard/pairwise。只有整条
  attestation chain 与当前 artifact 一致时才汇总。
- **代码**：`validate_hard_wave.py`、`run_hard_wave_summary_when_ready.sh`。

### P61. Oracle evidence 不能简单 last-write-wins
- **现象**：后续全量重跑把两个此前 strict-pass 任务覆盖成瞬时环境失败，当前
  snapshot 从 374 降为 372。
- **根因**：异构历史环境有 flaky build/cache/service；一次晚到的弱失败不应撤销
  已经完成的 3x start、3x golden、非空 P2P 和 coverage 证据。
- **解决**：release union 选择 strongest validated evidence。覆盖旧 strict pass
  必须提供更强的结构性反证或确认行为回退，不能仅凭一次环境失败。
- **影响**：最终新 hard 为 374，不是最后 snapshot 的 372。

### P62. Oracle metadata 里的绝对路径会泄漏机器布局并破坏可移植性
- **现象**：414 release 初稿中有 967 个 machine-specific path，分布在 command、
  coverage header、matched source、artifact 和 lane source 字段。
- **根因**：七条 lane 保留最丰富执行记录，天然引用 worktree、cache、venv 和
  session artifact。
- **解决**：`package_hard_release.py` 对所有 nested string 递归替换最长 path
  prefix，并在写完后扫描发布文件。占位符：
  `${REVIEWBENCH_SESSION_FILES}`、`${REVIEWBENCH_REPOSITORY}`、`${HOME}`。
- **门**：任何原始 root 残留都使打包失败；`SHA256SUMS` 在替换完成后生成。
- **代码/测试**：`scripts/package_hard_release.py`、
  `tests/test_package_hard_release.py`。

### P63. 事后补 hash 只能叫 continuity audit，不能冒充 runtime provenance
- **现象**：旧 wave 完成时 generation/judgment 记录没有 task、patch、prompt
  fingerprint。事后可以核对当前 patch 与 judgment 引用一致，但无法证明原始
  API 调用当时输入从未变化。
- **错误做法**：把当前文件 hash 写进普通 completion marker，并宣称原始 run
  已被密码学绑定。
- **解决**：旧 wave 显式标记 `legacy-continuity`；它只保证当前 artifact chain
  内部一致。新 wave 的 `runtime` marker 在 generation/judgment 完成时立即写入
  task、patch、rubric、judge model、context 和 output-budget fingerprint。
- **发布**：manifest 逐 wave 保存 attestation mode，论文和复现说明不得混用两种
  provenance 强度。
