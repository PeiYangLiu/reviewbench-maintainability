> **Historical archive — not current package instructions or a current leaderboard.**
> Preserve the protocol, roster, denominator, incomplete/withdrawn flags and audit
> caveats stated below; do not reinterpret these results using today's judge policy.
> See the [archive index](README.md) and [current documentation](../README.md).
> Historical commands and local artifact references may require assets not shipped
> in this repository. Publication changes are limited to this notice, portable
> path placeholders and repaired links; original bytes remain in the backup.

# Agentic Exec v1

新增协议：`reviewbench-agentic-exec/v1`。旧 `agentic-line-edit/v1`、
`agentic-line-edit/v2`、旧 release 和旧统计脚本保持原样。

## 当前状态与验收边界

执行框架已经具备真实的多轮工具循环、原生 Chat/Responses 消息、
持久 Docker 工作区、多文件补丁和独立四门评测。

**全量 402 题是否就绪，以 `prepare --verify` 的逐题验证和
`readiness.json` 为准。现有历史 Oracle 不自动等价于一个已隔离、
可交互的开发环境。默认配置只生成完整 402 题清单，不会把未迁移的
任务伪装成 ready。**

仓库附带 CPython pathlib 和三道 Ninja 任务的迁移工具。它们复制历史
Oracle 和 source-only golden patch，保持原断言和补丁字节；
另设不同的公开开发检查。Ninja 使用整个候选仓库的 CMake 构建，
不是只编译或替换评审锚点。这些实例用于验证生产资产接入，
不代表其余任务已经完成迁移。

未在本次改动中重训 SFT/RL，也不把确定性测试服务器的结果当作真实 LLM 榜单。

402 题的增量批量迁移入口、依赖归档、构建缓存和逐题状态，见
`docs/AGENTIC_RUNTIME_MIGRATION.md`。完整就绪仍以当前执行引擎的实际
`readiness.json` 为准，不以已有 adapter 数量推断。

## 安装

核心包仍然没有强制运行时依赖。Agentic 功能使用 Python 3.12 的独立环境：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python \
  --require-hashes -r requirements-agentic-py312.lock
uv pip install --python .venv/bin/python -e .
.venv/bin/reviewbench-agent --help
```

锁定 `mini-swe-agent==2.4.6`，lock 同时固定传递依赖及包哈希。
`pip install -e '.[agentic]'` 适合开发；正式实验应使用 lock。
运行时记录 Python、主要 adapter 依赖和执行层源码指纹，续跑不接受混合引擎版本。
不会读取 mini 的个人 `.env` 来决定实验参数。

## 循环不是单次调试

`ReviewBenchAgent` 继承上游 `DefaultAgent`。`run/step/query` 继续使用上游循环，
这里只扩展执行观察和原子 checkpoint：

```text
模型决策 → 原生 bash 调用 → 实际执行 → stdout/stderr/退出码 → 下一轮模型决策
   ↑                                                                  |
   +------------------------------------------------------------------+
```

测试/编译失败没有独立次数上限。它们是正常 observation。
连续三次**工具格式错误**终止；一次正常 step（即使命令退出非零）会清零格式错误连续计数。
默认 100 次模型/传输尝试、累计 128000 输出 token、每次最多 8192 输出 token、
3600 秒交互预算。OpenAI 风格嵌套的 reasoning token 已包含在输出统计中，
不重复累加；兼容网关若单列 `reasoning_tokens`，用 `total_tokens` 确认
它是否已包含，避免少算。计数自相矛盾的响应记录为未完成测量。
准备和终局评分不占交互时间。
SDK/LiteLLM 内部重试关闭，由预算包装层统一处理并记录显式传输重试。
这里计量的是 runner 向配置端点发出的调用；共享网关自己在上游内部发生的
路由重试不可由客户端 usage 推断，不宣称把这些不可见尝试计进 100 次。

工具输出给模型提供有限长度的 stdout/stderr，完整日志保存在宿主。
超时由容器内 `timeout` 终止进程组；宿主侧再设置兜底超时。
普通命令失败仍可继续；原生 `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 提交，
或者预算/上下文耗尽时采集当前补丁。原始输出和终止原因均保留。
普通 shell 返回 `125/126/127` 也属于工具反馈，不因为退出码数值被当成
基础设施故障。真正的 Docker/命令监督器错误另行记录。

文件修改跨轮持续，shell 的 `cd/export` 不跨轮持续。多个工具调用按顺序执行，
每个结果保持原调用 ID。思考/文本中的 JSON 不作为动作执行。
默认设置 `PYTHONDONTWRITEBYTECODE=1`，避免快速、等长度修改落在同一秒时复用旧 pyc。

## 开发与评分环境

每题需要一个 `RuntimeRecipe` JSONL 记录：

- `task_id/repo/start_sha/lane` 与任务绑定；
- 镜像 digest 与匹配的 developer-image audit 文件及其哈希；
- 公开 `dev_checks`、固定 `bootstrap`、工作目录和资源上限；
- 文件资产的内容哈希、挂载位置和 `developer/grader` 可见性；
- 可选、仅在本题内部网络运行的 sidecar；
- `grading` 中的多文件构建说明、固定测试、原始测试保护路径及 source-only golden 资产。

开发环境只有起点快照和新的独立 Git 元数据，没有共享缓存的未来提交、
golden 或评分文件；模型凭据只留在宿主。原始仓库包含 submodule 的任务需要显式
补齐对应源码资产，当前 snapshot 层会报告该缺口而不是留下空目录继续。

Docker 使用只读根文件系统、独立 `/tmp`、资源上限和无外网配置。
`/tmp` 显式允许执行，使 CMake 探测程序和临时目录中的本机编译产物能运行。
依赖应在镜像或声明的固定资产里准备好；`bootstrap` 只准备环境，不修改起点源码。
开发镜像的 isolation audit 属于需审核、哈希绑定的运行资产，而不是自动生成的
“该历史镜像适合交给 agent”声明。

评分从新的起点快照应用完整补丁，再恢复 recipe 声明的固定测试文件。
模型自己新增的测试可以参与开发，但不替代固定 PASS_TO_PASS。
七条 lane 使用相同多文件入口，测试适配器支持：

| Parser | 固定测试标识 |
|---|---|
| `go-json` | `Package::Test` |
| `junit` | `classname::name` |
| `trx` | `testName` |
| `rust-test` | libtest 输出中的测试名 |
| `ctest` | CTest 测试名 |
| `json` | `reviewbench-exec-test-results/v1` 的测试 ID |
| `command` | 单个历史行为 harness，并要求其自己输出精确 `success_marker` 行 |

`command` 不接受“退出 0 就算通过”。marker 应来自固定行为 harness 完成断言后的输出，
不要在外层命令无条件追加 `echo PASS`。消失、跳过、失败的固定测试不会当作通过。
现有只编译/替换锚点文件的 harness 需要先迁移为完整候选构建，再声明 `multifile_build: true`。

## CLI

配置示例位于 `configs/agentic-exec-v1.yaml`。路径相对配置里的 `root`，
模型 ID 必须显式替换，API key 通过环境变量名传入，禁止隐式换模型。

```bash
export REVIEWBENCH_REPO_CACHE=/path/to/exact-commit-cache
export REVIEWBENCH_AGENTIC_ASSETS=/path/to/agentic-assets

# 不调用 LLM：清点全部 402 题、源码和 runtime 缺口。
reviewbench-agent prepare --config configs/agentic-exec-v1.yaml

# 加入已迁移的 JSONL recipes，并运行公开开发检查与 start/golden 各三次固定 Oracle。
reviewbench-agent prepare --config configs/agentic-exec-v1.yaml \
  --runtime-recipes /path/to/runtime-recipes.jsonl --verify

reviewbench-agent run --config /path/to/experiment.yaml --model MODEL --resume --workers 4
reviewbench-agent evaluate --config /path/to/experiment.yaml --workers 4
reviewbench-agent report --config /path/to/experiment.yaml
```

`run` 只接受通过验证且任务、runtime、冻结 Oracle、执行引擎指纹匹配的 runtime。
更改引擎或冻结测试后需要重新验证，旧 proof 不会继续作为 ready。
默认任务集合仍是 audited-402；
测试专用 scope 文件可以用于内部烟测，但不会被称为完整 402 榜。
返回码：`0` 表示所请求阶段完整，`2` 表示缺口、无效输入或未完成测量。

旧 Oracle 到一个真实 recipe 的迁移示例：

```bash
python scripts/prepare_agentic_pathlib_runtime.py \
  --release data/releases/hard/20260808 \
  --legacy-root /path/to/hard_oracle_20260804 \
  --repo-cache "$REVIEWBENCH_REPO_CACHE" \
  --out-dir /path/to/pathlib-runtime --verify
```

它要求预先存在已固定 digest 的公共 Python 镜像，实际检查镜像中没有 Git 历史和
benchmark 参考产物。生成的 assets 和 recipe 包保持相对路径，可一起搬迁。
随后将该 `runtime-recipes.jsonl` 加入全量的 recipe 集合；其余缺口仍逐题列出。

Ninja 的完整多文件构建实例：

```bash
python scripts/prepare_agentic_ninja_runtimes.py \
  --release data/releases/hard/20260808 \
  --legacy-root /path/to/hard_oracle_20260804 \
  --repo-cache "$REVIEWBENCH_REPO_CACHE" \
  --out-dir /path/to/ninja-runtime --verify --workers 3
```

该工具绑定一个已经检查过、只包含 Ubuntu 软件包和编译工具链的镜像 ID；
镜像 history、根文件系统扫描和原 Dockerfile 哈希一起保存。公开检查使用
完整生产构建和原仓库 `misc/ninja_syntax_test.py`；终局单独挂载未改变的
depfile 行为 harness。编译目录位于容器 `/tmp`，不会混进候选补丁。

### 本机网关与并发

`configs/agentic-exec-v1.gateway.yaml` 提供显式本机网关配置：
`http://127.0.0.1:4000/v1`，两个 solver arm 分别是 Chat 与 Responses。
模型名和允许返回的版本均固定；三位判官的名单也在实验开始前确定。
调用方 key 可使用 `api_key_env` 指定环境变量名，配置和轨迹不保存 key。

```bash
export REVIEWBENCH_REPO_CACHE=/path/to/repos
export REVIEWBENCH_AGENTIC_ASSETS=/path/to/combined-runtime-assets
export REVIEWBENCH_AGENTIC_RECIPES=/path/to/runtime-recipes.jsonl

reviewbench-agent prepare --config configs/agentic-exec-v1.gateway.yaml \
  --verify --workers 8
reviewbench-agent run --config configs/agentic-exec-v1.gateway.yaml \
  --resume --workers 8
reviewbench-agent evaluate --config configs/agentic-exec-v1.gateway.yaml --workers 8
```

这些命令默认仍要求完整 402 题 ready。开发烟测需显式传入较小的
`--scope-ids` 和独立 `--out-dir`，不自动跳过缺少 runtime 的任务。
组合不同 recipe 包时，所有资产路径应相对同一个 `asset_root`；
改写路径后 runtime 指纹随之改变，需要重新验证组合后的 recipes。

`prepare --verify`、`run`、`evaluate` 都支持 `--workers N`；
配置字段为 `execution.workers`，默认 1，CLI 优先。并发单位分别是
runtime 验证、task×model episode、候选评分，各自使用独立容器和目录。
每个 episode 内的工具调用仍按模型给出的顺序执行。每个任务完成后立即
原子保存聚合结果，固定分母和行顺序不随完成先后改变。

同一个输出目录有进程锁，拒绝两个 CLI 同时写入，防止重复抽样或覆盖 checkpoint。
改变 worker 数量只影响调度，不改变实验身份，续跑可以调整并发。
独立实验可使用不同输出目录并行；调整 worker 数时应同时考虑每题 CPU、
内存和源码快照磁盘用量，而不只考虑网关的请求并发。

执行 CLI 现另有跨进程资源准入、本地热缓存、Blob 分块归档和终态回收。
默认最多 8 个任务、3 个重构建、32 核 CPU 预留；内存和磁盘水位参与准入。
Blob 只存归档块，Git/构建/工具反馈不直接在挂载层运行。
完整配置、全量源归档、lease 续跑与证据恢复见
`docs/AGENTIC_STORAGE_AND_CONCURRENCY.md`。源归档完成不等于 402 个 runtime 已就绪。

## 轨迹、补丁与恢复

- 起点、可变 workspace、命令原始日志、轨迹、补丁和任务结果分别存储。
- 补丁由宿主对比完整工作树生成，不依赖模型的 HEAD/index；包含新文件、删除及重命名。
  新文件排除规则取可信起点的 `.gitignore`，不是模型改写后的忽略规则。
- 结果绑定任务、runtime、引擎、配置和补丁；固定四门表从完整 task×model 矩阵建立。
- `--resume` 复用已有终态；进行中的恢复只接受仍存活、源码状态匹配、
  没有未确定执行中动作或模型请求的同一沙箱。不重放 bash 来假装恢复进程状态。
  每次向模型端点发送请求前持久化 pending 标记和已计入的调用次数；
  控制器在网络请求途中崩溃后，不会无记录地重新采样或重置次数预算。
- `--retry-infra` 只重试基础设施错误并保留旧 attempt。模型失败不被重复采样。
  终局评分默认复用已完成结果；判官传输重试只重做缺失成员，不重采样有效否决票。
- 运行清单和缓存评分也绑定引擎指纹，变更执行/评分代码后不自动复用旧完整测量。
- `generation_results.jsonl` 保留兼容字段，并新增 `changed_files`、
  `runtime_fingerprint/engine_fingerprint` 等。工作记录里保留本机 artifact 路径；
  对外搬迁前仍需连同 artifacts 重定位这些引用，不能只复制顶层 JSONL。

## 判分

维护性评审包含完整多文件 diff 和 before/after，不静默删除超长文件证据；
证据超窗记录为未完成。保持三位不同判官一致通过，要求解决 concern、质量至少 4、
没有同等级新债务、修改相关。

无补丁是模型失败并在固定分母内计零；基础设施或判官缺失是未完成测量。
只有全部 gate 完整时发布 `resolved_rate`；否则提供明确标记的下界和缺口数量。
连续票数只是统计列，`1/3`、`2/3` 都不会变成 FINAL 通过。

判官输出允许一个 JSON 对象，或只包围这个对象的一层 Markdown JSON fence；
不从解释/推理段里搜索对象，不修复损坏的 JSON，也不挑选多个对象中的有利结果。
格式错误、截断、HTTP 错误和模型身份不符时仍保存收到的原始响应。

评分适配器升级时，可以显式重评已冻结的生成结果，不重新调用 solver：

```bash
reviewbench-agent evaluate --config /path/to/experiment.yaml \
  --regrade --retry-infra --workers 8
reviewbench-agent report --config /path/to/experiment.yaml --regrade
```

`--regrade` 只允许评分引擎变化；任务、runtime、solver 配置和判官面板仍须与
原运行清单一致，复用判官票前还校验原 rubric 指纹。生成轨迹和补丁哈希继续验证，
旧评分被归档，固定 Oracle
重新执行，已有有效判官票（包括否决票）原样保留，只补缺失成员。报告分别记录
generation/evaluation 引擎指纹。这不是重跑 solver 或对失败答案 best-of-N 的入口。

## 测试

```bash
uv pip install --python .venv/bin/python 'pytest>=8,<9'
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_agentic_exec.py

# 不调用付费 LLM：使用本机确定性 HTTP provider，但真的运行 mini、Docker 和测试程序。
REVIEWBENCH_DOCKER_TESTS=1 PYTHONPATH=src \
  .venv/bin/python -m pytest -q tests/test_agentic_exec.py
```

Docker 测试要求本机存在 `python:3.11`。包括 Chat/Responses 两次失败后修复通过、
多个 tool-call ID、格式错误阈值、预算耗尽保留补丁、进程组超时清理、
固定测试防删改、无补丁分母和 CLI prepare→run→evaluate→report 全链路。
另覆盖 worker 并发上限和聚合顺序、竞争输出锁、Oracle/引擎指纹失效、
网关独立 reasoning 计数、125/126/127 普通失败反馈、临时本机二进制执行。
