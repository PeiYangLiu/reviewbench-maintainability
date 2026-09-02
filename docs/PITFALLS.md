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

### P64. Harness 只按字符截断，会撞死在服务窗口上并烧光轮次
- **现象**：BSMPT 一题 32 轮里有 24 轮被 vLLM 拒绝，prompt 61,441 token +
  4,096 输出 = 65,537，**只超出 1 个 token**。全榜共 85 轮这样被浪费，涉及
  12 个 model-task pair，且这 12 对**全部只在第 32 轮才勉强完成**。
- **根因**：prompt 由 `initial_context_chars`(30,000) 与
  `max_transcript_chars`(120,000) 两个**字符**上限拼装，与 `max_model_len` 的
  **token** 预算无关，可以合法地超出窗口。超限被记成一轮错误后继续，下一轮
  transcript 只会更长，必然再撞。
- **只打自建模型**：85 轮全部属于 65,536 窗口下自服务的 4 个开源模型，8 个 API
  模型 0 轮。看起来像模型能力差异，实为部署窗口差异。
- **解决**：`invoke_within_context_window()` 按服务端报回的超出量裁剪 transcript
  并重试该轮。裁剪量由这次拒绝本身标定（用报回的 token 数算该 prompt 的
  chars/token 比），实测只裁 **0.5%**，而人工设定的 retry 预算裁掉 **33%**。
- **代码/测试**：`scripts/generate_agentic_patches.py`、
  `tests/test_generate_agentic_patches.py`（含 BSMPT 真实报错文本回归）。

### P65. 收缩预算必须锚定「实际渲染长度」，不是名义预算
- **现象**：P64 修复后，Kimi 一题仍连烧 29 轮，报 `did not fit after 6 trims`。
- **根因**：收缩时从**名义预算** 120,000 里减，但实际渲染的 transcript 往往远
  小于该值（如 60,000）。把预算从 120,000 降到 106,860 **完全不改变 prompt**
  ——渲染结果逐字节相同。6 次尝试全在空转，刚降到接近实际长度就用完次数。且
  失败轮次不保留已降预算，下一轮从头再空转。
- **解决**：锚定到 `min(budget, rendered_chars)`，保证每次裁剪真正缩短 prompt；
  失败轮次也持久化已降预算；耗尽错误带上裁剪区间与原始拒绝内容。
- **验证**：同一题修复后 **1 步**裁到位（120,000 → 68,044），零耗尽、零拒绝。
- **教训**：修复本身也要拿真实数据验证。第一版 8 个单测全绿，仍在生产空转 29 轮。

### P66. 补跑必须与原榜**同服务配置**，否则产出不可互换
- **现象**：Coder 补跑改用 `40G2-A100`/TP=2（原榜 `40G4-A100`/TP=4）后 OOM：
  A100 无原生 FP8，vLLM 回退 Marlin 内核重打包 MoE 权重，显存翻倍。
- **更深的问题**：即使塞得下也不能用。TP 数改变归约顺序进而改变数值，产出的行
  与它要替换的榜上行不可互换。
- **不变量**：榜内**允许**并发度（`MAX_NUM_SEQS`/`GENERATION_WORKERS`）不同——
  既有 retry wave 本就混用 10/4/2，且生成未固定种子；但 **sku 与 TP 从未变过**。
- **解决**：`tests/test_amlt_serving_parity.py` 逐项校验
  `MODEL_REVISION / TENSOR_PARALLEL_SIZE / MAX_MODEL_LEN / sku`，并故意不锁并发度。
- **附带**：尺寸阶梯另有一条——各档必须**同窗口**。32B 在 2 卡上 KV cache 只剩
  4.22 GiB（需 5.0 GiB），vLLM 建议「把 `max_model_len` 降到 34560」**不可采纳**：
  只给一档缩窗口会把尺寸效应与上下文效应混在一起，且恰好混在支撑趋势线的那档。
  正解是加卡到 TP=4，窗口不动。

### P67. 判分脚本默认端点与实际可用网关不一致
- **现象**：55 个 pair 判分失败，三个裁判齐报 `Connection refused`。我 curl
  `8787` 得到 200，据此判断「网关已恢复」，重试后毫无变化。
- **根因**：`judge_release_patches.py` 默认 `--chat-endpoint` 是
  `127.0.0.1:**4399**`，该端口已停；`8787` 是另一个可用网关。我拿 8787 的 200
  去证明 4399 可用，白费两轮重试。
- **解决**：显式传 `--chat-endpoint http://127.0.0.1:8787/v1/chat/completions`。
- **fail-closed 的价值**：这 55 个 pair 全部集中在 4 个开源模型上。若被当作
  「未通过」计分会系统性压低它们的分数；记为 `judge_error` 使其无法进入榜单。
- **教训**：验证服务可用性必须打**脚本实际使用的**那个地址。

### P68. 同一 lane 并发跑在同一输出目录会互删 worktree
- **现象**：`native` lane 出现大量 `infra_retry`（阶梯 143 条、SFT 31 条），报
  `fatal: cannot change to '.../native-worktrees/...'`。lane 只要还有未解决的
  infra 就拒绝报成功，driver 因此永远收敛不了。
- **根因**：我先后用三种方式拉起 lane，都指向同一 `--output`。证据是
  `lanes.done` 里 tsjs/jvm/rust 各出现两次。两个进程共用同一套 worktree 命名，
  互相删除对方的目录。
- **解决**：driver 按 lane 加 `flock`，同一输出目录同一 lane 只允许一个进程；
  自造的 infra 行必须**清掉重跑**，不能继承。
- **另一半**：各 lane 有**不同的并发硬上限**（scripted 6 / csharp 4 / native 4 /
  rust 8；gopy·tsjs·jvm ≥10）。统一传 10 会被参数校验直接拒绝——快速失败，无害，
  但要读 lane 自己的日志才看得到，外层 traceback 只说 `lane runners failed`。
- **教训**：并行的前提是各任务**真正独立**。load 冲到 940（256 核的近 4 倍）时，
  争抢会把构建失败伪装成 infra 失败。

### P69. SFT 截断丢弃 prompt 头部 = 丢掉系统提示，静默训废两轮
- **现象**：27B 两个 adapter 微调后与基座**几乎完全一致**（round-1 `edit` 占比
  17%→16%），在一个 75% 都是 edit 的数据集上训了 2 个 epoch 却纹丝不动。
  Qwen3 4B–32B 则行为**坍缩**：`edit` 从 77% 掉到 1%，`finish` 涨到 92%，陷入
  `read → finish → read → finish` 死循环，出补丁率从 400/402 跌到 114/402。
- **根因**：`train_sft.py` 超长时从 token 序列**尾部**切
  （`prompt_ids[-budget:]`），而 chat 模板的 `<|im_start|>system` 与整个动作协议
  规范都在**开头**。实际训练用 `MAX_LENGTH=8192`（配置里写死，非脚本默认 32768），
  全量 2,921 样本中 **1,372 条（47.0%）**丢失系统提示——近一半训练信号来自
  推理时永不出现的无头输入。
- **排查中被否定的三个假设**（都有反证，勿重复走）：
  1. 「数据教了不改就交卷」→ 893 个 finish 目标 **100% 都在 edit 之后**；
  2. 「训练/推理 prompt 格式错位」→ 四个格式标记 100% 一致；
  3. 「系统提示不同」→ 逐字节相同，都是 1022 字符。
- **解决**：截断改为**保留头部、压缩正文中段**（与评测 harness 一致）；
  `MAX_LENGTH` 提到 **24576**（实测 p100=42,384；24576 下仅 1.1% 需压缩）；
  并加 `_assert_prompt_survived()`：样本若丢失系统提示直接抛异常。
- **为什么必须报错**：这个 bug 静默了两轮训练、两个模型家族，无任何迹象。宁可
  任务失败，也不要产出一个看起来正常、实际学错的 adapter。
- **代码**：`reviewbench-sft/scripts/train_sft.py`、`reviewbench-sft/configs/*.yaml`。

### P70. 「作业 failed」与「lane 退出码非 0」都不能直接当失败
- **作业层**：vLLM 生成作业退出码非 0 往往只是**失败计数**导致。SFT 阶梯 10 个
  作业全报 failed，实际每个都跑完了 201 题；Kimi v14 四分片全报 failed，实际
  377 题全部完成。必须查 `generation_summary.json` 或数 trace，不能看状态。
- **lane 层**：`gopy exit=1` 的真实含义常是「还有 `INFRA_RETRY_REQUIRED` 未解决」
  ——fail-closed 拒绝在基础设施重试未清零时报成功，需要再跑一轮而非判定失败。
- **反向也成立**：`amlt results list` 里有产出不等于跑完。Coder 首轮补跑有
  124.7 kB 产出，实为 vLLM 启动失败，`open_model_manifest.json` 里
  `models_response: {}` 才是真相。
- **推论**：「剩余题清单」若按*假定*已完成推算，会漏题。Kimi v14 的 377 题
  remainder 就是这么算的，导致 **8 道审计题从未被任何一轮生成过**，直到逐题
  核对实际产出才发现。

### P71. 线性注意力模型缺融合内核 → 慢 16 倍且不报错
- **现象**：Qwen3.6-27B 单 epoch 估算 **231 小时**，同规模稠密模型只要 24 小时。
- **根因**：该模型 `layer_types` 里 48/64 层是 `linear_attention`。transformers
  的分发是 `chunk_gated_delta_rule or torch_chunk_gated_delta_rule`，镜像没装
  `fla` 就走纯 PyTorch 回退：**强制 fp32** + 每层 `seq_len/64 = 384` 次串行
  Python 循环，每次前向 48×384 ≈ 1.8 万次串行迭代，梯度检查点还要重算一遍。
- **解决**：`image_setup` 装 `flash-linear-attention==0.5.2`（纯 Triton wheel，
  只依赖 einops）。1634.9 s/it → **166.7 s/it**。`causal-conv1d` 故意不装——
  它的回退是普通 cuDNN 卷积，不慢，但要现场编译 nvcc。
- **门**：`assert_linear_attention_kernel` 检测到 linear_attention 层却没有 fla
  内核就抛异常。慢 16 倍不会自己报错，只会看起来「在跑」。

### P72. LoRA target_modules 按后缀匹配，混合架构会静默漏挂
- **现象**：Qwen3.6-27B 的 LoRA 只挂上了 16/64 层的注意力。
- **根因**：线性注意力层 `Qwen3_5GatedDeltaNet` 的投影层叫 `in_proj_qkv` /
  `in_proj_z` / `out_proj`，而 target_modules 写的是 `q_proj/k_proj/v_proj/o_proj`。
  PEFT 按后缀匹配，**一个都匹配不上**，那 48 层只有 MLP 挂了 LoRA。PEFT 不会
  因为某些层没匹配上而报错。
- **解决**：补上线性注意力的投影名。修复后可训练参数占比与稠密模型对齐
  （27B 217.6M / 0.8025% vs 32B 268.4M / 0.8127%），这个比值就是最好的校验。
- **注意**：`in_proj_a` / `in_proj_b` 不要加——输出维度只有 `num_v_heads`，
  是每头一个标量门。视觉塔用 `linear_fc1/fc2`、`proj`、`qkv`，不会被误伤。

### P73. 可抢占算力上的长任务必须按步存档
- **现象**：14B 在 **503/517 步（97%）**被抢占，14 小时全部报废，产出目录只剩
  一个日志文件。32B 卡死在第 33 步，5 小时后才被发现。
- **根因**：`save_strategy="epoch"` + `EPOCHS=1` 等于**只在最末尾存一次**，且
  没有任何续跑逻辑。
- **解决**：`save_strategy="steps"` + `save_total_limit=2` + 启动时
  `get_last_checkpoint` 自动续跑，SAVE_STEPS 按各模型速度设成 35–45 分钟一次。
- **配套**：PyTorch 2.6 把 `torch.load` 的 `weights_only` 默认改成 `True`，而
  DeepSpeed 的 checkpoint engine 没有覆盖它，导致**存档能写、永远读不回来**
  （`UnpicklingError: ZeroStageEnum was not an allowed global`）。需要
  `allow_deepspeed_checkpoint_globals()` 注册 `ZeroStageEnum` 与 `LossScaler`。

### P74. 「续跑失败就隔离检查点」会误杀好档
- **现象**：一次 OOM 导致 checkpoint-180（35% 进度）被当成损坏档隔离，实际那个
  检查点完全正常，错的是 GPU 规格。
- **根因**：把「加载失败」一律当成「文件损坏」。OOM、Triton 编译失败、NCCL
  错误都会让加载失败，但检查点本身是好的。
- **解决**：只有 `UnpicklingError` / `EOFError` / `KeyError` 这类**真正的反序列化
  损坏**才隔离；环境类错误保留检查点并提示换 SKU 重跑。顺带修了 8 个 rank
  同时改名的竞态。

### P75. `amlt rerun` 会把 series 限定的 SKU 降级
- **现象**：32B 报 `total capacity of 15.77 GiB`（被调度到 V100 16G）；
  27B-nothink 报 `PassManager::run failed`（fla 的 Triton 内核编译不出 sm_70）。
- **根因**：`NDAMv4:G8` 这类写法**首次提交解析正确**，但存储后退化成 `G8`，
  rerun 时就没有机型约束了。

  | 提交时写的 | 存储后 | 后果 |
  | --- | --- | --- |
  | `NDAMv4:G8` | `G8` | 调度到 V100 16G |
  | `NDH100v5:G8` | `G8` | 同上 |
  | `80G8-A100-IB-NvLink` | 不变 | ✅ |

- **解决**：一律用显式 SKU 形式，不要用 `Series:G8`。
- **相关**：这与 P70 是同一族问题——**作业面板不可轻信**。被抢占（SIGTERM）后
  容器已死，`amlt status` 仍显示 `running`；32B 卡死 5 小时才被发现，是靠对比
  两次读数发现**步数一模一样**才察觉的。监控要看日志里的步数推进。

### P76. `accepts_loss_kwargs` 会让两个模型族的有效学习率不同
- **现象**：Qwen3 族与 Qwen3.6-27B 在完全相同的超参下，梯度尺度差 `grad_accum`
  倍（本例 8 倍）。
- **根因**：自定义 Trainer 的 `compute_loss` 忽略 `num_items_in_batch` 时，HF
  是否代为除以 grad_accum 取决于模型的 `accepts_loss_kwargs`：

  | 模型族 | `accepts_loss_kwargs` | Trainer 是否除以 grad_accum |
  | --- | --- | --- |
  | Qwen3（4B–32B） | True（forward 有 `**kwargs`） | **否** |
  | Qwen3.6-27B | False（类属性显式关闭） | **是** |

  DeepSpeed 侧 Trainer 传了 `scale_wrt_gas=False` 也不缩放，净效果是 Qwen3 族
  按 8 个 micro-batch **求和**而非平均。
- **影响边界**：同族内部一致，所以「每个模型对比自己的基线」这个口径仍然成立；
  但**跨族的绝对数值不可直接比**。
- **修法**：`self.model_accepts_loss_kwargs = False`（HF 文档建议）。注意修完
  之后新旧结果不可直接比较，要么整批重训，要么明确标注口径切换点。

### P77. correctness lane 的 worktree 会撑爆磁盘，且失败看起来像 infra
- **现象**：4B 的 correctness 跑到 53/402 后停滞，load 掉到 1.36，lane 全停但
  产出远未完成，10 分钟无写入。外层只报 `lane runners failed`。
- **根因**：`/data` **100% 满**（3.5T 用掉 3.3T，仅剩 8.6G）。lane 日志里才有
  真正的错误：`OSError: [Errno 28] No space left on device`。
- **体积来源**：每条 correctness lane 会为「每个模型 × 每道题」创建 git worktree
  并跑真实构建，产物是 `*-worktrees` / `*-artifacts` / `*-cache` /
  `*-shared-cache` / `*-worker-state`。单次运行动辄上百 GB：

  | 运行 | 清理前 | 清理后 |
  | --- | ---: | ---: |
  | infra-rerun406 | 211G | 29G |
  | sftmix (8B) | 143G | 5.3G |
  | ladder | 106G | 7.2G |
  | sft27b | 74G | 7.3G |

  合计回收 **300+ GB**，可用空间 8.6G → 621G。
- **关键区分**：这些目录是**可再生的中间产物**，结果文件（`*.jsonl`、
  `correctness-normalized/`、`judging/`）很小且已并入 release，删缓存不损失结论。
  清理前务必确认结果已落到正式 release（本例：release 5,628 条 + merged overlay
  5,628 条），清理后复查行数。
- **两个操作细节**：
  1. 构建产物常带只读位，`rm -rf` 会报 `Permission denied`，需先
     `chmod -R u+w` 再删；
  2. **磁盘满时写入的 jsonl 可能被截断**，重启前要逐行 `json.loads` 校验并丢弃
     损坏行，否则后续统计会带着半条记录跑。
- **与 P68/P70 同族**：磁盘满、worktree 互删、被抢占——三者都会让 lane 以
  「看起来像 infra 失败」的方式停下。**必须读 lane 自己的日志**
  （`run/modern/correctness/logs/<lane>.log`），外层 traceback 只说「失败了」。

### P78. 上游用 HTTP 200 报告失败，网关把它当成功透传
- **现象**：v2 冒烟跑，`gemini-3.6-flash` 一道题连续 12 轮
  `finish_reason: error` + `content: ""`，与周报 2026-08-11 记录的
  「149 题连续 32 轮空 content」完全同形。同批 `claude-opus-5` 2/2 正常。
- **第一反应是错的**：以为是 v2 历史格式的问题。但**第 1 轮根本没有历史**
  （`messages` 只有 `[system, user]`），照样空响应——与历史格式无关。
- **根因（分两层）**：
  1. **上游**用 HTTP **200** 承载一次内部失败：`finish_reason: "error"`、
     content 为空，`completion_tokens: 23`、`reasoning_tokens: 208`
     ——模型生成了 token，但没能返回。少数情况下响应里还带回
     `tool_calls`，而**请求根本没有声明 `tools`**（网关注入）。
  2. **llm-gateway 只看 HTTP 状态码**（`if resp.status_code < 400: return`），
     于是把一次上游故障当作正常补全交给调用方，既不重试也不故障转移。
- **为什么危险**：调用方无法区分「后端挂了」和「模型没话说」。榜单会把一次
  部署故障记成模型能力不足——与 P54、P64 同族。
- **解决（两侧都改）**：
  - `llm-gateway`：新增 `content_free_failure()`，识别 2xx 里的无补全响应
    （chat 的 `finish_reason` 不在 OpenAI 合法取值内、responses 的
    `status=failed`、body 里带 `error` 对象、`choices` 为空）。命中后**同路由
    重试**，再失败则**换路由**，全部失败返回 503。
  - **刻意不 trip 熔断**：路由是通的、也没超配额，问题是每请求抖动。
    实测把它 park 掉会让仅有的两条 gemini 路由同时出局，把间歇故障放大成
    彻底不可用（第一版就是这么写的，12 次里 6 次直接 503）。改为独立的
    `LLMGW_EMPTY_RETRIES` 预算，与参数协商预算分开，不占用后者。
  - harness：v2 路径调用 `reject_undeclared_tool_calls()`，把这类轮次记为
    `deployment reported finish_reason=error...`，而不是
    「模型没返回合法 JSON」。
- **残留是单个部署的问题，不是协议、不是模型族**：修完之后 gemini-3.6-flash
  仍只有 4/8 成功、单次 18.8 秒。但**同后端、同提示词、同协议**换个型号立刻正常：

  | 模型 | genaiapi 路由 | Copilot 直连 | 耗时 |
  |---|---:|---:|---:|
  | gemini-3.6-flash | **4/8** | **1/6** | 18.8s |
  | gemini-3.7-flash | **8/8** | **6/6** | 3.2s |
  | gemini-3.5-flash | **8/8** | **5/6** | 6.7s |

  harness 端到端复核：3.7-flash 与 3.5-flash 各 2/2 生成（17 轮、15 轮）。
  **结论：`gemini-3.6-flash` 这个部署本身处于降级状态**，与 v2 协议无关。
- **一次错误归因的教训**：我最初只测了 3.6-flash，就写下「Gemini 在 v2 下不可跑」。
  实际只需换一个同族型号做对照就能证伪。**判定「模型/协议不行」之前，必须先跑
  一个只换单一变量的对照组**——这里是同后端同提示词换型号。
- **对 2026-08-11 那 149 题的重读**：当时把 32 轮空 content 归给文本拼接协议，
  但本次证明 3.6-flash 部署自身就会稳定产生同样症状。那批数据**可能**同源，
  未复核前不宜再把它当作「文本拼接压制模型」的证据。
- **排查前必查**：HTTP 200 不等于成功。响应里出现请求未声明的 `tool_calls`、
  或 `finish_reason` 不在 `{stop,length,tool_calls,content_filter,function_call}`
  内，一律先怀疑上游/部署，不要从 prompt 工程开始查。

### P79. 网关抹平模型名之后，「返回名 == 请求名」不再是身份证明
- **现象**：主榜 GPT 走 Copilot Responses API，返回裸 `gpt-5.6-sol`；改走
  `--force-chat` 后网关返回 `gpt-5.6-sol-2026-07-09`，身份强校验逐轮拒绝，
  两道题 16/16 轮全废。`gpt-5.5` 同理：钉死的是 `-2026-04-23`，
  chat 路由现在给的是 `-2026-04-24`。
- **更深的问题**：改走 llm-gateway 的 `/v1/responses` 后返回的是**规范化的裸名**
  `gpt-5.5`。网关对所有后端统一模型名，所以 `data["model"]` 只是回声，
  **既不能证明是哪个快照，也不能证明是哪个部署**。放宽或收紧这个字符串
  都不解决问题。
- **解决**：不动身份校验，改为记录网关自己的归属头
  （`x-llmgw-backend` / `x-llmgw-cluster` / `x-llmgw-upstream-model` /
  `x-llmgw-protocol`），由 `post_json()` 塞进响应的 `served_by`，逐轮落进
  trace。这比字符串比对强：它说明**哪个集群的哪个部署**真的服务了这一轮。
  实测形如
  `{'x-llmgw-backend': 'aoai', 'x-llmgw-cluster': 'ConversationhubSouthCentralUS',
    'x-llmgw-protocol': 'responses', 'x-llmgw-upstream-model': 'gpt-5.6-sol'}`。
- **已决**：`gpt-5.5-2026-04-23` 在任何路由上都已不可达——网关 `/v1/responses`
  返回裸 `gpt-5.5`、chat 路由返回 `-2026-04-24`、Copilot 直连返回裸名。
  这条 pin 的用途是**拒绝静默降级到另一个模型**，不是把某个快照永久冻住；
  三个名字都是 gpt-5.5，因此都登记为可接受，具体是哪个部署由逐轮的
  `served_by` 记录。真正的教训是：**用模型名做身份证明，会在上游改标签时
  把自己锁死**——攻击面在名字，证据应该在部署头。
- **代价**：v2 榜的 gpt-5.5 与 v1 榜不是同一快照，需在榜单里声明。
- **附带**：token endpoint 为空时不再去 mint token（本地网关自带凭据），
  否则重跑会依赖一个它根本不访问的、且已停用的 4399 服务（P67）。

### P80. 换协议的同时上游也变了——没有对照组就会把账算错
- **现象**：v2 全量重跑后 `claude-sonnet-5` 生成率从主榜的 402/402 掉到
  229/402，`claude-opus-5` 从 399 掉到 345，而 GPT 四个模型纹丝不动
  （400–402）。轨迹上，v1 主榜中位 **3 轮**交卷，v2 中位 **32 轮**、
  一直 `find`/`read` 从不 `edit`。
- **几乎又错一次**：所有证据都指向「v2 协议害了 Claude」。但同网关、同时刻、
  同 40 题跑一次 **v1 对照**，结论翻转：

  | 运行 | 生成 | 中位轮次 | 32 轮耗尽 |
  | --- | ---: | ---: | ---: |
  | 主榜 v1（2026-08-15） | **40/40** | 3.0 | 0 |
  | **v1 今天（协议未变）** | **28/40** | 17.0 | 12 |
  | v2 今天 | 25/40 | 24.5 | 17 |

  **崩溃发生在协议没变的 v1 上。** 协议只占 3/40，其余 12/40 是上游变化。
- **真正变了什么**：主榜那次 Claude 响应 `reasoning_text` 长度为 **0**——
  没有扩展思考，第 1 轮直接 `edit`、第 2 轮 `finish`。今天同一题同一模型
  `think_len` 为 956 / 1266，模型改为反复探索。**扩展思考现在默认开了。**
- **同时暴露的第二个问题**：`claude-sonnet-5` **298/402 题（74%）**的轮次被分散到
  两个不同后端且两种不同 wire protocol（`copilot/chat` 与 `genaiapi/messages`）
  ——同一道题的不同轮次跑在不同服务配置上。对照：`gpt-5.6-sol` 多后端 1 题、
  多协议 0 题。
  - **根因**：`sticky_key_for()` 用 `messages[0]` 做会话指纹，而 agent 的首条
    消息是**所有任务共用的 system 提示**，于是 402 道题塌缩成同一个 pin，
    粘性形同虚设。
  - **解决**：改用第一条**非 system** 消息（docstring 本来写的就是
    "the opening turn"），system 文本仍参与加盐以免不同 agent 撞车。
- **教训（与 P78 同族，但更贵）**：P78 是「换型号做对照」，这条是
  **「换协议时必须把旧协议也在当下跑一遍」**。缺了对照组，一次上游的默认值
  变更会被完整地记到协议头上，而且证据链看起来非常完整。
- **对榜单的影响**：v2 榜内部是自洽的（14 个模型同一时间窗、同一协议）；
  **不可比的是 v1 主榜**，它测的是一个已经不存在的 Claude 服务配置。
  v1↔v2 的差值不能解释为协议效应。

### P81. correctness 门不校验**候选补丁自己**改的行是否被测到
- **现象**：RL session 指出 `evaluate_docker.py` 的 scope 判定是
  「改动非空 ∧ 都在锚点文件内 ∧ 行数不超上限」，而题目按构造行为不变，
  于是**加一行注释就同时过 scope 和 P2P**。
- **我第一反应答错了**：回复说主榜走的是严格 lane，有 `changed_lines_covered`
  堵这个洞。**查记录才发现不成立**——`coverage_provenance.changed_old_lines`
  是**建题时 golden 补丁**的改动行，属于题目属性；候选补丁的改动行
  **没有任何一步做覆盖校验**。严格 lane 同样可以被平凡通过。
- **量化**（主榜 v1 逐题记录，5,628 对）：

  | 候选改动行数 | correctness pass | 其中过三判 | 比例 |
  | --- | ---: | ---: | ---: |
  | 1–2 行 | 249 | 96 | **38.6%** |
  | ≥6 行 | 939 | 387 | **41.2%** |

- **但不能据此加「最小改动行数」先验**：两档过判分的比例几乎相同。
  很多 review 评论要的本来就是改名 / 提常量 / 删死代码，1–2 行是正当答案。
  真正的判别器是三判面板，不是改动规模。
- **统计上还踩了一次坑**：2,223 条 pass 的 `changed_lines` 是 **lane 字段缺失**
  而非 0。第一次用 `r.get('changed_lines') or 0` 统计，得出「69.6% 的 pass 只改
  ≤2 行」的错误结论。**缺失值必须与零值分开**，否则会凭空造出一个惊人结论。
- **对 benchmark 的影响**：`Final Resolved` 的四门里，correctness 这一门的强度
  低于文档此前的表述。应在有效性威胁里明确：**correctness 证明「没改坏」，
  不证明「改到了点子上」**，后者完全由 maintainability 面板承担。
- **可选的加固方向**（未实施，会改变主榜口径）：对候选补丁也跑一次覆盖率，
  要求其改动行被 P2P 执行到。代价是每题多一次带 coverage 的构建。

### P82. 输出预算 4,096 在系统性地惩罚「先写散文再吐 JSON」的模型
- **来源**：RL session 在自己的 27B rollout 里发现 25.0% 的轮次解析失败，
  逐条看全是被截断在半句话中间，并提醒我们核对 v2 基线里那些
  「动作不可解析」的题。**拿我们自己的数据一查，属实，而且比他们报的更广。**
- **证据**：`completion_tokens` 的 p99 与 max **同时压在 4,096**（我们设的上限），
  失败轮响应中位 **6,223 字符**、成功轮 **838 字符**——不是模型不会输出动作，
  是动作还没轮到就被砍断了。
- **分布极不均匀**（`finish_reason=length` 占该模型全部轮次的比例）：

  | 模型 | 轮次 | 被截断 | 占比 | 其中记为错误 |
  | --- | ---: | ---: | ---: | ---: |
  | Qwen3.6-35B-A3B | 3,032 | 1,075 | **35.5%** | 859 |
  | claude-opus-4.8 | 5,380 | 1,350 | **25.1%** | 1,069 |
  | **claude-opus-5（榜首）** | 7,580 | 1,104 | **14.6%** | 994 |
  | Qwen3.6-27B | 5,788 | 574 | 9.9% | 490 |
  | claude-sonnet-5 | 8,667 | 857 | 9.9% | 847 |
  | GPT-5.x 四个 | 3,008–3,961 | 12–78 | **0.3–2.1%** | 3–33 |

- **对失败题的占比更刺眼**（失败题中至少有一轮被截断的比例）：
  claude-opus-4.8 **94.7%**、Qwen3.6-35B-A3B **100%**、claude-opus-5 **75.4%**、
  claude-sonnet-5 43.9%；而 GPT 系是 0%。
- **机制**：这些 checkpoint 会先写一段**普通散文**推理再输出 JSON 动作
  （注意不是 `<think>` 标签，所以 thinking 统计里看不到）。预算不够时
  动作被砍掉，harness 记为「模型没返回合法 JSON」。
- **这是部署侧预算，不是模型能力**。与 P64（窗口）、P78（上游 200 报错）、
  P80（上游默认值变更）同族：**归因给模型之前先排除预算**。
- **对已发布 v2 榜的影响**：`claude-opus-5` 以 Final 120 居首，而它 75.4% 的失败题
  含截断轮；`Qwen3.6-35B-A3B` 是 100%。**榜单低估了这些模型，且低估幅度与模型
  的输出风格相关，不是随机噪声。** 已在榜单文档里显著标注。
- **修法**：把 `--max-output-tokens` 提到覆盖观测最大值（RL 侧观测到 10,871 字符，
  建议 ≥8,192），重跑受影响模型。**必须整榜同一预算重跑**，只补跑受影响的几个
  会造成榜内口径分裂（P66）。
- **v1 榜受影响更重**：`configs/amlt_openmodel_retries.yaml` 的 retry wave 钉的是
  **`MAX_OUTPUT_TOKENS=1024` 与 `2048`**，比造成本次问题的 4,096 还要狠一倍到四倍。
  该配置是冻结的历史记录、不修改，但**用 v1 榜做任何跨协议对比时必须把这条计入**。
- **已修**：`run_open_model_job.sh` 与 `run_open_model_agentic.py` 的默认输出预算
  提到 **8,192**；`tests/test_amlt_serving_parity.py` 加了两条守卫，任何现役配置
  再把它压回 8,192 以下都会红。冻结的 v1 配置显式豁免并注明原因。
- **为什么是 8,192 而不是拉满**：输出预算与输入窗口共用 `max_model_len`。
  实测自建模型 prompt_tokens p99=47,618、max=61,425，在 65,536 窗口下最长 prompt
  只剩 **4,111** 余量——拉大输出预算等于放大 P64 那类「超窗口 1 个 token 整轮被拒」
  的爆炸半径。而实际需求：output p50 仅 **108**、p90 1,138，已观测最长 content
  12,555 字符（约 3–4k token）。8,192 覆盖全部观测值且留约 2 倍余量，
  再往上加收益为零。**预算要按观测需求定，不是按上限定。**
- **修复效果已全量验证**（同题、同服务配置，只改预算）：

| 模型 | 4096 生成 | **8192 生成** | Δ | 4096 截断率 |
|---|---:|---:|---:|---:|
| Qwen3.6-35B-A3B | 375 | **396** | **+21** | 35.5% |
| Qwen3.6-27B | 374 | **386** | **+12** | 9.9% |
| Qwen3-Coder-Next-FP8 | 348 | **357** | +9 | 1.1% |
| MiniMax-M2.1 | 375 | **379** | +4 | 0.9% |
| gpt-oss-120b | 397 | **398** | +1 | 0.8% |
| Devstral-2-123B | 369 | **369** | 0 | 0.4% |
| claude-opus-4.8 *(同 397 题)* | 361 | **385** | **+24** | 25.1% |
| claude-sonnet-5 *(同 401 题)* | 228 | **236** | +8 | 9.9% |

  **增益与截断率高度相关**：35.5% 截断的 35B-A3B 涨 21 题，25.1% 的 opus-4.8 涨 24 题，
  而 0.4% 的 Devstral 一题不涨。因果链闭合——这确认了那些「失败」确实是预算，
  不是能力。`Qwen3.6-35B-A3B` 的失败从 27 降到 **6**，即 **77% 的「模型失败」
  是假的**。
- **一条方法性收获**：这个缺陷是**另一个 session 从自己的实验里发现并主动
  提醒我们核对的**。跨 session 交换「失败形态」比交换「结论」更有价值——
  他们看到的 25% 解析失败，我们这边表现为若干道「模型不会做」的题。

### P83. 按轮切分的多轮 SFT 会丢掉「何时收手」，表现为生成率腰斩
- **现象**：`reviewbench-sft` 的多轮 adapter 在 v2 下生成率相对基座腰斩
  （Qwen3-4B 373→193、8B 394→206、14B 398→157、32B 399→199），
  中位轮次从 **4 涨到 32**，`edit` 占比从基座的 40–63% 塌到 **2.9–4.8%**。
- **第一反应是错的**：像是「数据配方教坏了」。但**只看交了卷的题**，
  adapter 的维护性通过率是基座的 **2–8 倍**（`Maint|Generated` 2.5% vs 0.3–1.0%）。
  **配方教对了东西。**
- **失败形态很干净**：失败题里 edit 动作的数量是
  **209/209、196/196、244/245、201/203 为零**。不是「改坏了」，也不是
  「改到一半没交卷」（harness 在轮次耗尽时会固化已有改动，所以
  「edit 过但没成补丁」只有 0–2 个）。**是从头到尾只在 read/find。**
- **根因**：训练把一条 R 轮轨迹拆成 R 个样本、每个只监督当前轮。
  模型学到「这一轮该做什么」，**样本里没有任何东西表达「探索到第几轮该收手」**。
  推理时每轮的局部最优都是「再看一眼」，于是永不收敛。
- **验证（可证伪，已做）**：给 v2 加 `--edit-deadline`（**默认关闭**，
  不影响已发布的榜）——第 N 轮仍未 edit 时在**尾部 user 轮**追加一句
  「探索结束，现在返回 edit」。不碰 system prompt（它与训练数据逐字节绑定）。

  | | 无 deadline | **deadline=24** | 基座 |
  | --- | ---: | ---: | ---: |
  | qwen3-8b-mtsft 生成 | 206 | **341** | 394 |
  | qwen3-32b-mtsft 生成 | 199 | **338** | 399 |
  | `edit` 占比 | 4.2–4.8% | **8.1–9.0%** | 56–63% |
  | 32 轮耗尽 | 225–229 | **156–169** | 5–18 |

  **一步补回相对基座缺口的七成**，未改 adapter、未重训、未改数据。
- **但补产量并没有补回分数——我的外推错了，如实记录**：
  按 `Maint|Generated≈2.5%` 外推，341 道生成应换来 8–9 道维护性通过。
  实测 32B 是 **9**（吻合），8B 是 **1**（完全不吻合）。
  按「deadline 是否触发」拆开看，原因清楚：

  | | 触发 deadline 的题 | 未触发的题 |
  | --- | --- | --- |
  | qwen3-32b | 143 道，通过 **1**（0.7%） | 195 道，通过 **8**（4.1%） |
  | qwen3-8b | 181 道，通过 **0**（0.0%） | 160 道，通过 **1**（0.6%） |

  **被逼出来的补丁几乎全部过不了维护性判分**（0.0–0.7%，对比自发交卷的
  0.6–4.1%），改动行数中位仅 24。也就是说：**停止信号把「没交卷」变成了
  「交了张白卷」，产量是真的，分数不是。**
- **两条结论要分开讲**：
  1. **根因判断成立**——失败确实是「不动手」而非「改坏」，
     补上停止信号后产量可恢复七成，这一点被实验证实；
  2. **但外推不成立**——不能假设「补回来的题」和「本来就会做的题」质量相同。
     模型在第 24 轮被迫动手时，它并不知道该改什么。
- **对训练侧的修正建议**：因此不能只在推理时加 deadline 收工。
  真正要补的是**让模型自己学会何时收手**（例如把轮次预算写进样本 prompt），
  这样它是在「想清楚了所以动手」，而不是「被喊停所以随便改」。
- **教训**：诊断「模型变差了」时，先把**产量**与**质量**拆开看——
  这里质量确实提升了 2–8 倍，被产量塌陷掩盖。但**修好产量之后必须重新测质量**，
  不能用原来的转化率外推：干预会改变被补回来那部分样本的分布。
  我先写下了 8–9 的预期，实测 8B 只有 1，正好是这个错误的代价。
- **给训练侧的建议**：把轮次预算写进每个样本的 prompt（如 `ROUND 7 OF 32`），
  让「还剩多少」成为可条件化的输入。这保住了按轮切分对 thinking 监督的全部好处
  （13.5% → 77.9%），只补上缺失的那个维度。

### P84. 给工具结果加包裹时，旧的截断逻辑会把闭合标签吃掉

训练侧要求把每个工具结果包进 `<tool_response>…</tool_response>`（Qwen 模板
靠它区分「工具回执」和「真实 user query」，不包的话只有最后一轮保得住 thinking，
实测 1/5 对 5/5）。

harness 原本对**整个 user 轮**做字符上限截断。直接套上包裹后，
一条超限的工具结果会被从中间切断，`</tool_response>` 直接没了。

**为什么危险**：Qwen 判定只看 `content` **是否以** `<tool_response>` 开头。
开头还在，于是——模板照常保留 thinking，训练侧闸门不报警，渲染照常收敛，
只有那批超长轮次的监督目标里带着一个永远不闭合的标签。**全程绿灯。**

- **修法**：截断只作用于**包裹内部的 payload**，包裹恒完整。
  上下文超限时二分求解 cap 的那个函数也要改成按**渲染后**字节计量，
  否则它低估包裹开销、少削一点、然后再被拒一次。
- **守卫**：50,000 字符的结果在 cap=500 下仍须 `startswith("<tool_response>\nROUND 4 OF 16\n")`
  且 `endswith("\n</tool_response>")`，且两个标签各只出现一次。

### P85. 改公共路径后只跑单测，会让旧协议跟着新协议一起被悄悄改掉

加包裹协议时我动了会话构建的公共代码。单测全绿，但拿 600 条**真实 v2 trace**
重建消息序列逐字节比对时出现 1 处不同：

折叠轮（模型某轮没返回可解析响应，下一个工具结果并入上一个 user 轮）上，
旧实现是「先拼接再按 cap 截断」，我写成了「逐段截断」。
对 v2 这是行为变更——而 v2 是已发布主榜的协议。

- **修法**：按协议分支。无包裹时保持旧的「先拼接再截断」，有包裹时才逐段。
- **教训**：新增协议时，**旧协议必须拿真实产物做逐字节回归**，而且要在
  **两个不同的 cap 下**各跑一遍——用默认 cap 跑，截断路径根本不会被触发，
  这处差异就永远不会暴露。这是 P80「换协议要把旧协议也跑一遍」的静态版本。

### P86. 七个 lane runner 对「进度」和「完成」的写法互不相同，共三套

排查磁盘时发现看门狗从不回收 tsjs 的缓存。原因是 lane 产物异构程度比
之前记的 status 词表更深，**共三个维度都不一致**：

| 维度 | 出现过的写法 |
| --- | --- |
| 文件名 | `<lane>-checkpoint.json`、`<lane>.checkpoint.json` |
| 进度字段 | `completed_rows`/`total`、`records`/`expected`、`records`/`expected_records` |
| 完成信号 | `complete: true`、`status: "in_progress"`（没有 `complete` 字段） |

只认一种写法的后果是**把已完成的 lane 当成未完成**——不报错，只是永远不回收，
直到磁盘撞上阈值。

- **修法**：统一走一个 `lane_state()`：文件名逐个试，进度字段逐个取，
  完成信号取「`complete is True` 或 `status ∈ {complete,…}` 或 `done >= total`」的并集。
  最后那条兜底最稳，任何 schema 都成立。
- 同类的还有 status 词表（`PASS`/`pass`/`correctness_ok`），归一化必须走 `finalize`。

### P87. 「模型答不出题」被当成「作业失败」，于是所有作业都是红的

`generate_agentic_patches.py` 原本是 `return 0 if all(failed == 0) else 1`。
在一个**专门测量模型答不出多少题**的 bench 上，这意味着：
只要有一题没做出来，作业就以非零码退出，集群把它标成 **failed**。

后果不是「多了个红点」，而是**状态列彻底失去信息量**：

- `v2-budget8k-20260829` 六个作业全 failed —— 实际全部 402/402 正常产出；
- `sft-budget8k-20260829` 五个 failed —— 同样全部正常；
- `rl-three-arm-20260830` 的 rft2 failed —— 373/402，也是正常完成。

我为此逐个去 `amlt results list` 核对 tarball 才敢采用，**每次都要人工确认**。
更糟的是：真正崩掉的作业（比如中途 OOM 只跑了一半）长得**一模一样**，
没有任何办法从状态列区分。

- **修法**：退出码只回答「有没有把交给它的题都跑完」，
  即 `attempted < requested` 才返回 1；题目本身失败一律返回 0。
- **不能用 `generated + failed != tasks` 做判据**——这两个数是按同一个字典算出来的，
  恒等，永远不触发。要跟**请求的题数**比。
- **教训**：退出码表达的是「作业是否履行了职责」，不是「结果是否好看」。
  在测量失败率的系统里，把被测量的失败混进退出码，等于把报警器关掉。

### P88. 「把输出预算拉到 8192」不是普适建议——截断率低的模型只会更差

P82 的修法在截断率高的模型上是对的（35B-A3B +21、opus-4.8 +24）。
但我把它当成了普适建议发给了训练侧和 RL 侧，**这是错的**。

输出预算和输入窗口**共用 `max_model_len`**。把预算从 4,096 提到 8,192，
等于把可用 prompt 空间砍掉 4,096（Qwen3-* 阶梯的 `MAX_MODEL_LEN=40960`，
36,864 → 32,768，**少了 11%**）。

拿六个 mt-SFT adapter 做单变量对照（只改 `MAX_OUTPUT_TOKENS`）：

| adapter | 生成@4096 | 生成@8192 | Δ | 「放不下」轮次@4096 | @8192 | Δ |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| qwen3-4b | 193 | 185 | **−8** | 59 | 175 | **+116** |
| qwen3-8b | 206 | 195 | **−11** | 160 | 342 | **+182** |
| qwen3-8b-nothink | 111 | 104 | **−7** | 32 | 81 | +49 |
| qwen3-14b | 157 | 178 | +21 | 146 | 186 | +40 |
| qwen3-32b | 199 | 223 | +24 | 108 | 152 | +44 |

**这些 adapter 的截断率两边都是 0.0–0.1%**——预算根本不是它们的瓶颈，
于是提预算只剩下成本：**「上下文放不下」的轮次每一个都涨了**，三个模型净变差。

**但这个成本是有条件的,我第一次也把它说过头了。**补了 4096 对照臂之后:

| arm | 生成@8192 | 生成@4096 | Δ | 「放不下」@8192 | @4096 |
| --- | ---: | ---: | ---: | ---: | ---: |
| RL-SFT | 377 | 377 | 0 | **0** | **0** |
| RL-RFT1 | 379 | 379 | 0 | **0** | **0** |
| RL-RFT2 | 373 | 371 | −2 | **0** | **0** |

**RL 三臂完全不受影响**——同样是 Qwen3-8B、同样 `MAX_MODEL_LEN=40960`。
区别在**对话长度**:

| | 轮数中位 | 触到 32 轮上限 |
| --- | ---: | ---: |
| RL 三臂 | **2** | 6–7% |
| mt-SFT adapter | **32** | 50–60% |

RL 的臂两轮就交卷,对话根本长不到会挤上下文;mt-SFT 的 adapter 有一半以上
把 32 轮全部烧光,对话累积到几万 token,这时候少 4,096 token 的余量才会咬人。

- **完整判据**(两个条件都要看):
  1. `finish_reason=="length"` 占比高(≥5%)→ 提预算有收益;
  2. 对话长度接近窗口(轮数中位接近上限)→ 提预算有成本。
  两者都低 → **提不提都一样**,别浪费一轮重跑去验它。
- **注意这个成本项本身是 P83 的症状**:对话之所以长,是因为模型不知道该收手。
  修停止策略会顺带消掉预算的成本项。
- **别用 `completion_tokens >= 上限` 统计截断**：服务端截断时可能不返回 `usage`，
  那些轮次会被静默算成「没截断」。35B-A3B 在 8192 下按这个口径是 4.5%，
  按 `finish_reason` 是 **12.7%**，差的 254 轮全是缺 `usage` 的。
  （同一个「字段缺失≠0」的坑在 P81 已经踩过一次。）

### P89. 生成是随机采样,但这条线上所有 delta 都是拿单次跑报出来的

请求 payload 里**没有 `temperature` 也没有 `seed`**,用的是部署端默认
(vLLM 的 OpenAI 兼容层默认 `temperature=0.7`)。也就是说**同一个模型、
同一套配置跑两次,结果不一样**——而这条线上的所有结论都是单次跑的差值:

- 「SFT 零效果」= 374 vs 378
- 「P82 修复 +9」= 348 vs 357
- 「RFT 门控通过率单调下降」= 94.6% → 93.6% → 91.9%

**这些差值的分母(噪声地板)从来没有被测量过。**

RL 三臂在 402 题上的配对检验是第一个警告:

| 对比 | 只有 A 做出 | 只有 B 做出 | 不一致题数 | McNemar p |
| --- | ---: | ---: | ---: | ---: |
| SFT vs RFT1 | 19 | 21 | 40 | 0.875 |
| SFT vs RFT2 | 25 | 21 | 46 | 0.659 |
| RFT1 vs RFT2 | 26 | 20 | 46 | 0.461 |

**三臂两两之间都不显著**,但**每一对都有 40–46 题(约 11%)在两边翻来覆去**。
边际计数(377/379/373)看起来很接近,掩盖了底下大量的逐题翻转。

- **教训**:比较边际计数会同时高估和低估——
  高估是因为把噪声当效应,低估是因为看不见相互抵消的翻转。**必须做配对检验。**
**噪声地板测出来了**(同一个 adapter、同一套配置、跑三遍):

| 对比 | 只有 A | 只有 B | 不一致题数 | p |
| --- | ---: | ---: | ---: | ---: |
| rep1 vs rep2 | 22 | 24 | **46** | 0.883 |
| rep1 vs rep3 | 21 | 20 | **41** | 1.000 |
| rep2 vs rep3 | 24 | 21 | **45** | 0.766 |

边际计数:**374 / 376 / 373**。

- **噪声地板 = 41–46 题不一致,边际 ±3 题。**
- **三臂之间是 40–46 题——落在噪声地板正中间。**
  也就是说 RFT 两轮在生成这一门上的差异**全部是采样噪声**,一点效应都没有。
- **推论**:这条线上任何**边际差值 ≤3 题**的结论都是空的,包括:
  「SFT 零效果 374 vs 378」(差 4,勉强在边缘)、
  「P82 给 Qwen3-Coder +9」「给 MiniMax +4」(前者勉强、后者纯噪声)。
  只有 +21 / +24 这一档才稳稳超出噪声。
- **11% 的题会在两次跑之间翻转**,所以边际计数会同时掩盖大量相互抵消的翻转。
  **报 delta 必须同时报配对不一致数**,只报边际计数等于没报。

### P90. 在串行 lane 循环还没跑到的时候手工并行启动同一个 lane,会开出两个 runner

我最初用一个 `for L in gopy scripted tsjs csharp jvm rust native` 的串行循环跑 7 个 lane。
跑到一半嫌慢,又手工并行起了 rust 和 native。

问题是**那个串行循环还活着**。它跑完 jvm 之后照着列表继续,
于是在 rust/native 已经跑了 7 个多小时的时候,**又起了一个 native runner**,
两个进程写同一个 `native.jsonl`。

这次没出事(runner 有 `repo-process-locks`,`native.jsonl` 283 行零重复零损坏),
**但那是运气**——我并不是因为知道有锁才这么干的。

- **做法**:手工并行接管某个 lane 之前,**先把串行驱动杀掉**,
  或者一开始就不要用「循环 + 中途插手」这种混合模式。
- **查法**:`ps -eo pid,etime,cmd | grep "[n]ative_runner.py"`,
  看到两个 `etime` 差很多的同名进程就是这个问题。
  杀**新**的那个(旧的进度更多),再核对结果文件有没有重复的 `(id, model)`。
- 顺带:并行跑多个 lane 会让磁盘消耗成倍上升。
  三个 lane 并行时 55 分钟吃掉 128G,离看门狗阈值只剩 25 分钟。
  现在有一个守卫在 `<100G` 时 `SIGSTOP` 掉最耗盘的 native lane、`>=140G` 再 `SIGCONT`——
  用暂停而不是杀,checkpoint 和在飞的 worktree 都不用重做。

### P91. 「相对上一版 +180」不等于有增益——分母必须是基座,否则修复会被读成提升

原生多轮 ckpt 相对上一版按轮切分的 adapter,生成从 195 涨到 375(+180)。
我第一份报告就是这么写的,**读起来像一个巨大的胜利**。

基座对照跑出来之后:

| | 生成 | 轮数中位 | 与基座配对 p |
| --- | ---: | ---: | ---: |
| **基座 Qwen3-8B(没训过)** | **385** | **3** | — |
| 按轮切分 SFT | 195 | 32 | <0.0001 |
| 原生多轮 SFT | 375 | 2 | **0.14(不显著)** |

**未微调的基座比两版 SFT 都强。**基座本来就知道什么时候停(中位 3 轮)。

真实的因果链是:**按轮切分把基座本来就有的停止能力训坏了(385 → 195)**,
原生多轮**把它修回来了(195 → 375)**,相对基座是打平。

- **教训**:报 delta 时,分母选「上一版」还是「基座」会给出**方向相反的故事**。
  修复量和增益量必须分开说。凡是「相对上一版」的数字,
  **必须同时给出「相对基座」**,否则读者无法区分「变好了」和「不那么坏了」。
- **推论**:这条线上此前几轮 SFT 全是负收益,原因现在清楚了——
  不是配方没学到东西,是**训练管线在破坏一个本来就会做这件事的模型**。
  找基座对照比调配方更早暴露问题。
- 同样地,不能因为「基座和 SFT 都用了新格式跑」就省掉基座那一跑:
  基座数字是几天前测的话,中间上游一变就不可比(P80)。这次四个 job
  在同一时间窗、除 `MODEL_ID`/`MODEL_REVISION` 外零差异,有守卫测试断言。

### P92. 修好截断确实多产出补丁,但**一分都没多拿**——产量侧干预救回来的是模型本来就做不出的题

P82 说输出预算 4,096 在系统性截断,提到 8,192 后生成从 374 涨到 386、375 涨到 396。
**那部分是真的。**但四门跑完之后:

| 模型 | Final@4096 | Final@8192 | Δ | 4096 截断率 |
| --- | ---: | ---: | ---: | ---: |
| gpt-5.6-sol | 110 | 109 | −1 | ~0% |
| gpt-5.5 | 107 | 108 | +1 | ~0% |
| Qwen3.6-27B | 52 | 52 | **0** | 9.9% |
| **Qwen3.6-35B-A3B** | 33 | **25** | **−8** | **35.5%** |
| MiniMax-M2.1 | 21 | 28 | +7 | 0.9% |
| Devstral-2-123B | 11 | 16 | +5 | 0.4% |
| gpt-oss-120b | 8 | 8 | 0 | 0.8% |

**截断最严重的那个模型反而掉了 8 分**,尽管它多生成了 21 个补丁。
全部 Δ 都在噪声内(P89)。

直接检验那些「被预算救回来的」补丁:

| 模型 | 8192 新增补丁 | **其中四门全过** | 两边都生成的题四门通过率 |
| --- | ---: | ---: | ---: |
| Qwen3.6-35B-A3B | 24 | **1 (4%)** | 6% |
| Qwen3.6-27B | 19 | **0 (0%)** | 14% |

**被截断的恰恰是模型做不出的那些题。**截断是模型在长篇大论地乱撞的**症状**,
不是它失败的**原因**。给它更多空间,它只是把同一份做不对的答案写完。

- **这个形状现在有三组独立数据**:

| 干预 | 多出的产量 | **新增产出的通过率** | 原有产出的通过率 |
| --- | ---: | ---: | ---: |
| deadline 强制交卷(P83) | +135 题 | **0.0–0.7%** | 4.1% |
| 输出预算 4096→8192(本条) | +19/+24 题 | **0–4%** | 6–14% |
| 原生多轮 SFT(P97 那批) | +14/+8 题 | **0–7%** | 16–18% |

  三次不同的干预、不同的机制,**同一个结果**。
  可以当成这条线上的一条定律:**产量侧的改变把「没交卷」变成「交了张不对的卷」,
  不会把「不会做」变成「会做」。**
- **一般规律**:**产量侧的干预(更大预算、强制交卷、更多轮数)会把
  「没交卷」变成「交了白卷」,不会把「不会做」变成「会做」。**
  报这类干预的收益时,必须报**新增产出的四门通过率**,
  只报产量涨了多少是误导——它看起来像效果,实际是把失败换了个形态。
- **截断率仍然是有用的诊断**(它指出模型在哪些题上失控),
  但**不要把「修好截断」当成提分手段**。

### P93. 我在写「必须单变量」的同一天,自己做了个双变量消融

为了判断原生多轮 ckpt 的停止策略是在提示里还是在权重里,我跑了一个消融:
把训练格式(`<tool_response>` 包裹 + `ROUND n OF 16` 横幅)换回裸 JSON。

**顺手把 `--max-rounds` 从 16 也改回了 32**——因为裸 JSON 是上一代 adapter 的
服务方式,我"顺理成章"地把那一代的轮数上限也带了过来。

结果 14B 从 379 涨到 395(p=0.005)。看起来是「横幅有害」。

**但轮数直方图立刻说明了问题**:16 轮时 p90 恰好压在 16(上限绑定),
32 轮时 p90 只有 6。多出来的 16 轮完全可能是真正的原因。

补了第三个臂(裸 JSON + 16 轮)才拆开:

| 对比 | 结果 |
| --- | --- |
| 有横幅 vs 无横幅(都 16 轮) | 379 → **400**,p<0.0001 |
| 16 轮 vs 32 轮(都无横幅) | 400 vs 395,**p=0.18** |

**是横幅,不是轮数。**结论方向没变,但那是运气——如果反过来,
我会把「多给 16 轮」的效果写成「横幅有害」发出去。

- **教训**:P80 说「换协议要把旧协议也跑一遍」,但**跑旧协议时容易顺手把
  旧协议的所有配套设置一起带回来**。对照组要单变量,不是「回到旧的那一套」。
- **可操作的检查**:消融提交前,把两个配置的 env 做集合差,
  断言差集恰好是想测的那一项。我已有这样的守卫测试,但那次是先跑了才补的测试。
  **守卫要写在提交之前,不是在结果出来之后。**

### P94. 磁盘看门狗的滞回区间开太宽,把 lane 停了 90 分钟没人发现

为了防 P77(磁盘写满会污染 correctness 结果),我写了个守卫:
`<100G` 就 `SIGSTOP` 掉最耗盘的 native lane,`>=140G` 再 `SIGCONT`。
用暂停而不是杀,是为了保住 checkpoint 和在飞的 worktree。

它确实触发了(日志:`10:44:29 free=99G -> PAUSED native`),
**然后就再也没恢复**。因为其余 lane 仍在跑,磁盘一直在 100–113G 之间浮动,
**够不到 140G 这条恢复线**。native 停了 90 分钟,进度死死卡在 55/520。

我是因为「计数三次都没变」才去查的,`ps` 里状态是 `Tl`(T = stopped)才发现。

- **根因**:恢复线比暂停线高出 40G,而**这个差额比其余 lane 会释放的空间还大**。
  滞回是为了防抖动,但区间必须落在系统真实能腾出的幅度之内。改成 90/105 就正常了。
- **更普遍的教训**:**任何会「暂停」而不是「失败」的守护,都必须自己会喊。**
  它当时唯一的痕迹是一行日志和一个 `T` 状态——
  进度停滞看起来和「这个 lane 本来就慢」一模一样。
  真失败会有非零退出码和报错;被暂停什么都没有。
- **查法**:`ps -eo pid,stat,cmd | grep runner`,状态里带 `T` 就是被暂停了。
  监控脚本应当直接把这个状态打出来,而不是只打进度数字。

### P95. native lane 的并发数受**单个仓库的构建体积**约束,不是受 CPU 约束

native lane(C/C++)以 4 并发跑时,磁盘被 4 个 worktree 吃掉 **108G**:

| worktree | 大小 |
| --- | ---: |
| iree-org/iree | **67G** |
| openvinotoolkit/openvino | 23G |
| openvinotoolkit/openvino | 13G |
| openvinotoolkit/openvino | 5.6G |

**这四个都是在跑的,不是残留**——所以看门狗把 lane 暂停之后,
它占的 108G 一点也不会释放,而恢复所需的空间又只能靠它自己跑完来腾。
这是个死结,和 P94 的滞回缺陷叠加在一起就是 90 分钟原地不动。

- **修法**:把 native 降到 **2 并发**。lane 有 checkpoint,
  杀掉重启会从已完成的那条继续(实测 187/520 完整保留),
  只损失 4 个在飞任务,重跑即可。
- **判据**:native lane 的并发上限应当按**最大单仓库构建体积**估,
  不是按 CPU 核数。这批里 iree 单个就要 67G,4 并发的最坏情况超过 200G。
- 同类风险的还有 openvino、llvm 这种巨型 C++ 仓库。
  gopy/tsjs/jvm 的 worktree 都在 GB 级,只有 native 是几十 GB 级。

### P96. 两批 correctness 同时跑会把盘打穿,且残留是 root 所有、本机删不掉

眼看 6/7 个 lane 跑完、盘还有 203G,我又起了第二批 correctness(RL 三臂 + 噪声重复臂)。
**35 分钟后剩 26G,再过一会儿只剩 1.1G。**

两批各自的看门狗都只看总剩余空间,互相不知道对方存在,于是**两边同时暂停**、
谁也腾不出空间给谁。

- **修法**:同一时间只跑一批 correctness。第二批的 `prepare` 很便宜,
  可以先备好、等第一批结束再 `run`。
- **删残留时会撞到第二个坑**:worktree 里有一部分文件是**容器内以 root 建的**
  (`ls -ln` 显示 uid 0),本机用户 `chmod`/`rm` 一律 Permission denied,
  `rm -rf` 会删掉一半然后卡住,看起来像"删不干净"。

  **解法**:用同一个 oracle 镜像以 root 进去删。
  ```
  docker run --rm --user 0:0 -v /data:/data <oracle-image> \
    bash -lc "rm -rf <path>"
  ```
  这次 59G 的残留就是这么清掉的。
- **教训**:磁盘余量看着够,不代表**峰值**够。native lane 单个 worktree 就能到 67G(P95),
  两批并行的峰值是各自峰值之和,而不是平均值之和。

### P97. maintainability 门对 8B/14B 这一档没有分辨率,整档压在噪声地板上

22 个臂全部判完(66 对三判合议,全票),**maintainability 全部落在 0–7**:

| 组 | Maint 范围 | 判到的补丁 |
| --- | --- | ---: |
| **噪声重复臂(同 adapter 同配置跑三遍)** | **3 / 1 / 1** | 370–373 |
| RL 三臂(SFT / RFT-1 / RFT-2) | 0 / 5 / 6 | 370–376 |
| Qwen3-8B(基座 + 三种服务方式) | 0–5 | 371–382 |
| Qwen3-14B(基座 + 三种服务方式) | 2–7 | 376–397 |
| 按轮切分 adapter(4B–32B) | 2–6 | 103–222 |

对照主榜:`gpt-5.6-sol` 149、`Qwen3.6-27B` 85。

**噪声地板是 1–3,而组间最大差异是 7。**任何干预(SFT、RFT 两轮、
原生多轮、去掉横幅)在这一档上都测不出来——不是干预无效,
是**这门评测在这个能力区间没有刻度**。

- **推论**:8B/14B 的补丁能通过测试(correctness 有几十到一百多),
  但几乎没有一个能让三个判官一致认为「真正解决了可维护性问题且没引入新债」。
  它们做的是「能跑的改动」,不是「好的重构」。
- **做 reward 时不要用这个门的全票口径**:信号与噪声同量级,梯度里全是噪声。
  放宽到多数、或只要求 `addressed == "yes"`,分数会涨 40–70%(P92 那次误用
  多数判据就是这个幅度),信号强度才够。**reward 用不着和主榜同口径。**
- **报这一档的结果时,必须把噪声地板印在同一张表上。**
  单看「0 → 5 → 6」像是单调提升,把 `1 / 3 / 1` 放在旁边就一目了然。

### P98. 「微调没效果」先排四件事,再谈模型;这四件事有现成的查法

被问过「SFT 没效果是不是评测有 bug」。下面是当时的排查顺序,可以照抄。

**1. 权重真的换了吗**(最常见的假失败)

比较两臂在**都出了补丁的题**上的 `patch_sha256` 重合率。
静默回落到基座会给出**完全一样**的输出。实测 361 题**零重合**,
`served_as` 也分别是 HF id 和本地权重目录 —— 排除。

> 这一步必须比对**输出**,不能只看日志里加载了哪个路径:
> P79 记过网关会抹平模型名,「返回名 == 请求名」不是身份证明。

**2. 行为变了吗**(训练有没有生效)

动作分布是最灵敏的指纹:

| | read 占比 | edit 占比 | 轮数中位 |
| --- | ---: | ---: | ---: |
| 基座 8B | **1.3%** | 77.1% | 3 |
| 微调 8B | **33.3%** | 28.7% | 2 |

基座几乎不看文件就直接改,微调后学会先探索。**配方学到了**,不是训练没生效。

**3. 失败模式一样吗**(评测侧有没有偏袒某一边)

如果是评测侧的 bug,微调臂会出现基座**没有的**失败类型
(补丁 apply 失败、格式拒绝、scope 误判)。实测两边的
`failure_root_cause` 分布几乎重合,都是「测试真的没过」—— 排除。

**4. 是「交得少但交得准」吗**(产量与质量要分开看)

比「绝对通过数」更该看**交卷→通过率**:13.7% vs 15.0%(8B)、
18.3% vs 15.7%(14B)—— 也是持平。

**四条都过了,才能说是模型/任务本身的问题。**这次的结论是:
**SFT 改变了「怎么做事的流程」,没有改变「做对的能力」**,而这个 bench 卡的是后者。

### P99. 数字小的时候最容易退回边际比较,而那正是配对检验唯一有效的场合

我在 P97 里写「22 个臂的 maintainability 全在 0–7,噪声地板 1–3,同量级,
所以这一档测不出来」,并据此建议训练侧**放弃 8B/14B**。

**这个结论是错的。**用配对方向检验重算:

| | 只有基座过 | 只有训练后过 | p |
| --- | ---: | ---: | ---: |
| SFT 8B | 0 | 5 | 0.0625 |
| SFT 14B | 1 | 6 | 0.1250 |
| **SFT 合并** | **1** | **11** | **0.0064** |
| **Final 门合并** | **0** | **7** | **0.016** |
| RL:SFT→RFT-2 | 0 | 6 | 0.031 |

对照噪声臂(同一模型跑三遍):

| 门 | 方向 | p |
| --- | --- | ---: |
| Maintainability | 7 / 3 | 0.34 |
| **Final** | **2 / 2** | **1.00** |

**噪声是双向的,训练效应是单向的。**同样是「差 4 题」,
一个是随机涨落、一个是真效应,**边际计数把这个区别抹掉了**。

- **为什么会退回边际比较**:数字只有 0–7 时,「这也太小了,肯定是噪声」
  是个很自然的直觉,于是就不做检验了。**但小样本恰恰是配对检验相对
  边际比较增益最大的场合**——n=5 且全部单向时,边际看是「5 vs 0,才 5 题」,
  配对看是「零反例」。
- **我在 P89 里写过同一条**(「报 delta 必须同时报配对不一致数」),
  但只在生成门和 correctness 门上执行了。**记下一条规则不等于会在
  下一个门上想起它。**
- **操作规范**:任何两个臂的比较,输出必须是三元组
  `(只有 A 过, 只有 B 过, p)`,**边际计数只能作为附注**。
  已在 `summarize_four_gate.py` 之外补一个配对比较脚本承担这件事。

### P100. `pip install vllm` 会拉进一个和镜像 CUDA 版本不匹配的视频解码器

九个副本作业全部 failed,产物只有 4KB 的 `vllm-server.log`。根因:

```
OSError: libnvrtc.so.13: cannot open shared object file
```

vllm 启动时 `import` 链会走到 `vllm.multimodal.video` → `torchcodec`,
而 **torchcodec 当前构建链接 CUDA 13,镜像是 `cuda12.6`**。
服务在加载任何权重之前就死了。

- **难点在于它不是配置漂移**:镜像、SKU、env 与几小时前**成功**的那次
  完全一致(守卫测试能证明)。变的是 pip 在**安装时**解析到的依赖版本。
  也就是说**同一份配置在不同时间会有不同行为**,这类问题守卫测试抓不到。
- **修法**:这条线上没有任何东西解码视频,直接卸掉:
  ```yaml
  - python -m pip install "vllm==0.26.0" ...
  - python -m pip uninstall -y torchcodec || true
  ```
  比钉 CUDA 版本简单,也比钉 torchcodec 版本稳。
- **加了守卫**:任何安装 vllm 的配置都必须带这条卸载(`test_every_serving_config_drops_the_cuda_mismatched_video_decoder`)。
  它当场抓到了 5 个我漏改的配置——它们的 pip 行缩进是 2 空格而不是 4,
  所以第一轮字符串替换没命中。
- **教训**:批量改配置后**先跑守卫再提交作业**。
  我第一次用「按行插入」的脚本改那 5 个文件,把 YAML 改坏了,
  是守卫测试(9 个 FAILED)拦下来的,不是我自己看出来的。

### P101. 「测不出差异」往往只是「单次跑测不出」——重复采样把 RFT-2 的退化暴露了

我拿单次跑给 RL 三臂下过结论:SFT 377 / RFT-1 379 / RFT-2 373,
三对配对检验 p=0.46–0.88,**「三门全部测不出差异」**。

三臂**各跑三遍**之后(9 个 job,配置逐字节相同):

| 臂 | 三次 | 均值 | 组内极差 |
| --- | --- | ---: | ---: |
| SFT | 376 / 382 / 378 | 378.7 | 6 |
| RFT-1 | 383 / 380 / 379 | 380.7 | 4 |
| **RFT-2** | 367 / 368 / 368 | **367.7** | **1** |

逐题符号检验(比较「三次里成功几次」):

| 对比 | p |
| --- | ---: |
| SFT vs RFT-1 | 0.67 |
| **SFT vs RFT-2** | **0.0053** |
| **RFT-1 vs RFT-2** | **0.0011** |

**RFT 第二轮显著更差**,而单次跑时它看起来只是「低一点点」。

- **为什么单次跑看不见**:组内(同一臂跑两遍)逐题不一致中位 **38 题**,
  而真实效应只有 **11–13 题**。噪声比信号大三倍,单次比较必然读成「无差异」。
- **重复采样不是为了「更准」,是为了把每个臂的估计从 ±19 压到 ±3。**
  压下去之后,原本淹没的效应就直接显著了。
- **可操作的阈值**:这条线上单次跑的组内噪声约 38 题。
  **凡是小于 30 题的差异,单次跑一律不可信**,必须重复采样。
  一臂 17 分钟,三次不到一小时——这个成本相对于「把结论下反」是便宜的。
- 与 P99 合起来看:P99 是「小效应要用配对检验」,这条是
  「小效应还要用重复采样」。**两者缺一,都会把真效应读成噪声。**

### P102. 一次提交 12 个多卡作业会挤爆配额,症状长得像配置错误

副本实验一次提交了 12 个 2 卡作业。**10 个 failed**,其中多数
`amlt status` 里耗时是 `-`(从未启动),两个跑了 4 分钟。

有日志的那个显示:vllm **服务完全正常起来了**(权重加载、CUDA graph 捕获、
KV cache 分配全部成功),然后:

```
INFO [shm_broadcast.py:705] No available shared memory broadcast block found
in 60 seconds. This typically happens when some processes are hanging...
```

**日志到此戛然而止。**

- **容易误判成配置问题**:同一份配置在 6 个作业时完全正常。
  我第一反应是去查 CUDA、查镜像、查 env 差异——全都没问题。
  真正的区别只是**同时提交的数量**。
- **判据**:`amlt status` 里**耗时列是 `-` 的 failed 作业**基本都是没排上,
  不是跑挂了。有耗时但很短、且日志停在资源等待处的,是被挤掉的。
- **修法**:按尺寸分批提交。12 个 2 卡拆成 2×6 就正常了。
- 与 P100 的区别:那次是**所有**作业都死在同一个 import 错误(确定性),
  这次是**部分**作业没排上(资源竞争)。
  **全挂 → 查配置;部分挂且耗时为 `-` → 查配额。**

### P103. 「零反例」不是效应为真的证明——它本身也可能是运气

我在同一个问题上反复了三次,值得完整记下来:

| 时间 | 结论 | 依据 | 后来 |
| --- | --- | --- | --- |
| 09-01 早 | 「三门测不出差异」 | 边际计数 | 方法错(P99) |
| 09-01 晚 | 「质量门显著提升 p=0.0064」 | **单次跑**配对检验,零反例 | **被推翻** |
| 09-02 | 「质量门未通过验证 p=0.21」 | **三次重复** + 配对检验 | 当前 |

第二次我特别有信心,因为配对结果是 **1 vs 11**——几乎零反例,
而噪声臂是双向的(7 vs 3)。**方向的不对称看起来是决定性的。**

**但重复三次之后,同一批臂是 8 vs 15,p=0.21。**

原因在这张表:

| 臂 | 三次 maintainability | 极差 |
| --- | --- | ---: |
| base 14B | 3 / 3 / 3 | 0 |
| **native 14B** | **4 / 1 / 9** | **8** |
| base 8B | 3 / 1 / 2 | 2 |
| native 8B | 2 / 4 / 2 | 2 |

**同一份权重、同一套配置,一次拿 1 分、一次拿 9 分。**
我当初那次单跑恰好抽到 base=0、native=5,**两个方向的运气叠在一起**。

- **教训一**:配对检验解决的是「边际计数掩盖方向」(P99),
  **它不解决「单次抽样本身不稳」**。两个问题要分别用两种手段:
  配对检验 + 重复采样,**缺一不可**。
- **教训二**:**我先前的噪声地板估计(3.3 题)是错的**,
  它来自三个恰好抽得很紧的 `noise-rep` 臂(3/1/1)。
  **三个样本估不出方差。**21 个臂的数据显示真实极差可到 8。
- **教训三**:效应量 ≈ +1 题、组内极差 = 8 时,**任何单次比较都没有信息量**,
  包括零反例的那种。要确认这个量级的效应,需要每臂 10 次以上重复。
- **可操作规则**:报任何 delta 之前,先问「这个臂我跑了几次」。
  只跑过一次的,**结论一律标注为「未验证」**,不写 p 值。
