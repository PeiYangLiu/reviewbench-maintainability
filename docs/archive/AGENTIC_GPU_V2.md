> **Historical archive — not current package instructions or a current leaderboard.**
> Preserve the protocol, roster, denominator, incomplete/withdrawn flags and audit
> caveats stated below; do not reinterpret these results using today's judge policy.
> See the [archive index](README.md) and [current documentation](../README.md).
> Historical commands and local artifact references may require assets not shipped
> in this repository. Publication changes are limited to this notice, portable
> path placeholders and repaired links; original bytes remain in the backup.

# GPU v2 真实 agent 评测

GPU 模型在 AMLT 上运行原生工具调用兼容的模型服务。真实
mini-SWE-agent、Docker 开发环境、原正确性测试及裁判继续在评测宿主上执行，
不要求 GPU 云节点支持 Docker。固定范围是原六个 GPU 模型各 399 题。

## Session边界

API求解模型的评测由另一个session管理。本GPU流程不获取API队列/来源锁，
不修改API配置、作业或状态，也不再更新全14模型的
`CURRENT_FULL_V2_VIEW.json`。GPU结果写入独立的 `CURRENT_GPU_V2.json`；
写域保护会拒绝访问API管理锁及全模型CURRENT。既有发布记录不回滚。

GPU答案自身的原正确性检查和三裁判仍照常执行；这不是启动API求解模型评测。
共享原数据/运行环境只读复用，公共资源门及Blob两槽照常协调自己的GPU任务。
不做共享缓存回收，不停止或修改另一个session的任务。

## 当前统一入口

`scripts/run_agentic_gpu.py` 提供只读预检、原作业查询和需精确批准的原句柄恢复。
`inspect` / `observe` 不改变云端作业；`resume` 会申请恢复一个原 serving job，
但不发模型请求，也不会自动重新运行没有登记结果的题。
执行阶段的接入仍须完成原 owner、有效服务实例和任务消费状态核对。

当前GPU进度以本session独立的
`.benchmarks/gpu-v2-autopilot-634fbf94/gpu-publication/CURRENT_GPU_V2.json`
为准。读取该指针后应核验其view、scope、model_progress和verification哈希；
不能改读API session的移动CURRENT来判断本GPU批次是否完成。

```bash
python3 -B scripts/run_agentic_gpu.py inspect \
  --campaign .benchmarks/agentic-399-all-models-20260921-v1
```

`inspect`只使用独立GPU指针，缺失时显式报错，不回退读取API/全模型CURRENT。
如果GPU发布转到已授权的独立主机，`REMOTE_GPU_AUTHORITY.json`只引用
已经完整回读并逐文件验hash的镜像，`inspect`同时显示执行host与镜像时间。
这不会伪装成本机门槛通过，也不会覆盖原本机GPU指针或API状态。

初始`INSPECTION.json`与会话files中的基线副本只保留历史起点，
不是执行授权或当前全量完成声明。

原 AMLT 环境较旧，不支持 `--json-tables`。使用其原生 Python API 输出
结构化记录，不抓取表格、不升级已有冻结环境：

```bash
${REVIEWBENCH_WORKSPACE}/miniconda3/envs/amlt10/bin/python -B \
  scripts/run_agentic_gpu.py observe \
  --campaign .benchmarks/agentic-399-all-models-20260921-v1 \
  --out-dir .benchmarks/gpu-v2-autopilot-634fbf94
```

查询包括原 serving 作业及其后继作业；不会根据名称新旧自动选一个作业接管。
`Running` 仍不是有效模型端点，更不是完整评测。必须继续核对当前模型、
revision、原生工具调用、连接归属、服务寿命和本机准入。

`resume` 只接受新的精确句柄批准文件和独立证据目录。它复用原成功的命令、
原 owner-lock 获取实现，重新核对 provider/history 的 Paused 状态、目标配额、
模型及配置，并在 CLI 之前持久化一次性 intent。失败或超时不自动重试；
同一目录再次执行不会再次调用 resume。CLI exit 0 之后仍需另行确认实际
端点和原 agent 链路，不能仅凭该退出码计入进度。

独立宿主的新owner服务使用`resume_isolated_gpu_service.py`：仅允许当前独立
GPU项目的原句柄，持有对应model锁并重验model/revision、provider/history双Paused、
实际target quota及其他活动句柄，再写一次性intent后调用受支持的AMLT前端。
不占用其他模型的锁，不借用旧owner权限，恢复后仍须完整新epoch资格。

## 六模型原生执行与公平调度

`prepare_agentic_gpu_qualification.py` 将原 Qwen35 的只读 provider/公开
host-key/SSH peer/manifest/daemon 验证代码逐字节绑定到实际新 epoch，
拒绝 Queued、陈旧 manifest 和已过期的服务期限。
`run_agentic_gpu_case.py` 将一个尚未消费的原题交给原 composed dispatcher，
不更换 mini-SWE-agent、controller、Docker 环境或评分器。
六个原GPU模型均已建立真实AMLT句柄、严格peer/manifest/daemon资格并运行原生
agent单例。`run_agentic_gpu_queue.py`按模型轮转、宿主2槽/重任务1执行，
每个模型及任务另有排他归属锁，并将槽锁传给真实controller子进程。
只选择没有原输出/attempt的任务；缺少正式结果不等于可以重新生成。
资源不够的题保留并让其他可准入题推进，2394分母不随临时轻任务批次变化。
继续全任务时不传`--light-only`；原每题请求先独立检查，重任务仍最多1个。
队列可以用`--admission-wait-seconds`有界等待原资源门恢复，不能降低阈值。
`--qualification-map`仅在该模型没有运行中episode时接入新的已核验服务epoch，
重开该模型的后续任务；不把运行中的答案切换到另一实例。

本次已实际完成的任务是 DFE-Digital pull648：49 次模型调用后 Submitted，
原正确性测试执行完毕（0/1），原三裁判完成（2/3 通过），最终 resolved=false。
这是有效模型负结果，不是基础设施失败，不能为了提高分数重跑。
原生记录、patch、trajectory 和 terminal archive 均已保存。

Qwen35与MiniMax的双模型验收记录证明实际模型响应时间范围重叠210秒，
两条原G/C/J均完整结束。后续六模型公平批次每10条完整结果自动写入
GPU独立发布器，不触碰API入口；历史批次启动记录不代表当前仍运行或全量完成。

新serving复用了原启动代码；发现旧host-key observer的模型等值守卫硬编码Qwen35
后，仅在新副本绑定各自的精确模型键，旧失败、旧代码和句柄保留。
未删除、放宽host-key/daemon/模型身份校验。

原服务探针将合法bash的LF/转义LF或固定命令组合当作基础设施失败。
对GPT-OSS/Devstral仅续用已保留的首次原生响应，再执行一次有真实tool反馈的
后续请求；固定无害allowlist限制宿主可执行命令，保留所有原始响应。
该v2服务资格不计入benchmark，不改变实际agent、任务、预算或评分。
同一服务epoch后续任务复用已成功的资格，不重复探针模型请求。

AMLT WebSocket断开时只对原句柄/原epoch验证后重建自己的SSH转发，最多3次；
不重放HTTP字节、不重启原agent、不重置任何模型预算。过期或未知实例保持拒绝。
在G之前的只读endpoint观察中，精确`BackendNotReachableException`可最多做
3次backend发现，每次结果独立保留；身份/manifest/Paused等其他拒绝不走重试。
这不扩展原模型transport预算，也不重发任何solver请求。
宿主proxy/tunnel监督另预留这些G前只读重验的有界耗时，避免它们挤占原agent
的传输窗口；agent调用数、3600秒墙钟、每次/总输出及实际serving剩余寿命守卫不变。

`agentic_gpu_host.py` 只读取原 engine 的资源请求和共享80GiB硬门。
其预览通过不代表较高的100GiB launch门通过，也不授权执行。
所有新阶段记录位于 `.benchmarks/gpu-v2-autopilot-634fbf94/`；
已验证的初始发布快照副本保存在本次会话的 `files/gpu-v2-baseline/`。

## 不可变边界

- 正式完成数读取原发布器的 `complete_gate_or_native_model_failure`，不把所有
  `model_failed` 标签或所有 generation 终态直接相加。
- 已有答案只接续缺失的正确性/裁判流程；没有正式结果不等于没有消费模型请求。
- GPU补充恢复单独记录：空panel上下文故障仅补原首次J；已Submitted但collector
  校验失败仅重放原patch做同答案证明，然后补缺失C/J。原G、使用量、负结果及
  原失败文件均不覆盖。补充结果只进GPU独立视图。
- 已被原agent确认为`BudgetLimit: context_limit`且只在后续collector失败的答案，
  可显式`--allow-original-context-boundary`做同终态候选验证；不增加上下文、
  不重G、不改原使用量，恢复后仍标为context_limit而非Submitted。
- 纯裁判阶段复用原`source_judge_job`的源码资源作用域，不重复开发构建或要求
  仅构建阶段使用的Git scratch校验；完整源码、子模块、证据hash及原资源门仍核验。
  非空panel不走空panel恢复。`recover_agentic_gpu_rejected_judge.py`只允许一个
  明确命名、绑定原G/C/J和原始响应hash的未接受成员，使用原source-scoped一次性
  claim；接受过的票（包括否决票）、提示、输出预算均不改，不按错误类型自动重试。
- 超出1.6M字符的完整J证据不能截断。当前仅PWA1760的已量测源/证据hash
  profile及MFEM4044的独立精确profile允许复用既有3.2M上限；输出预算仍4096。
  超出该上限的Yaprmtrc证据继续缺测，不能截断或自行扩容。
  已记录Sol timeout的接续
  只按原transport谓词执行一次，并有source-global claim，不能换目录重复请求。
- `recover_gpu_immutable_correctness.py`只接受原C尚未执行Oracle、已有G与完整J、
  且失败为原immutable路径类型的任务。复用既有typed-input兼容engine与完整
  Git包含性/引用证明，prepare先做原失败→恢复→回滚同输入验证，再一次性execute。
  原G/J、校准、测试/Oracle及完整资源请求不重做或修改；发布时按G/C/J分别核对
  原engine和既有C-only兼容engine，不篡改指纹冒充完全相同的代码。
- `complete_gpu_wave_correctness.py`可作为最多30对GPU波次的C-only后继：
  等待原波次真实终态，再持有同一GPU coordinator锁逐一处理上述精确缺口并发布。
  其他错误与不明消费保持hold，prepare/execute失败不自动重试，更不发新的G/J。
- 已有实际完成的原C-only结果应直接复用而非重测。`--recorded-correctness`
  当前只接受精确hash绑定的Picongpu5174旧C记录，核对原G/J、执行凭据、
  完整typed输入和各阶段engine后采用原评分器导入；此路径0新G/C/J。
- 旧 `run_open_model_job.sh` 是 line-edit 入口，不用于本次真实 agent 评测。
- GPU 评测与 API Batch 队列分开；保留 GPU 宿主 2 任务/重 1 和原全局资源门。
- 旧服务期限、请求日志、归属锁和运行记录保持不变。服务已停止或旧期限过期时，
  需要新的受控接续绑定，不能删除旧记录或伪造新的就绪标记。

执行前须遵守 `AGENTIC_EVALUATION_REUSE_PRINCIPLES.md`。详细任务和验收要求
保存在当前会话的 `plan.md`；这里不把已完成预检当作全量评测完成。

## 磁盘持续阻塞时

连续等待原100GiB启动门后仍无法准入时，让本session自己的GPU队列排空，
不停止在途agent，不清理API session的共享存储。确认本地原生episode和
转发均结束、确切原模型句柄及归属锁一致后，可用
`pause_agentic_gpu_idle.py`暂停本session的空闲服务，保留云作业和全部结果。
`--verify-only`只查询已发pause请求的provider/history终态；命令exit0本身不代表
实际暂停完成。后续须先恢复资源余量，再只发布已有完整结果并接续缺失阶段。

## 独立GPU宿主复用

本机无足够GPU自有可回收空间时，可以使用用户已授权且实际资源满足原阈值的
其他宿主，而不是清理API session存储。当前备用host为`10.190.129.27`；
GPU专属目录在其`${REVIEWBENCH_WORKSPACE}/reviewbench-gpu-runtime-634fbf94`。

迁移保留原Python、mini-SWE-agent、依赖、AMLT/SSH协议、Git和Docker客户端字节，
并复制原hash-pinned image而非重建solver image。原完整GPU计划和controller
离线核验必须通过，旧校准只复用。也必须复用proxy解释器、vendor目录别名、
原Blob输入和只读挂载；不能只复制主镜像。

备用host的身份材料仅在该host只读mount；不复制用户私钥、token cache或绕过
账号/作业所有权限制。若当前合法账号无法恢复旧作业，保留其明确拒绝证据，
新服务须走该账号正常支持的AMLT前端及独立GPU-only项目，不冒充旧owner。
新项目服务仍须完整严格peer/manifest/native-tool资格和一条真实原agent+C/J，
不能把环境验证、代码传输或服务Running算作迁移完成。

`reserve_export_gpu_batch.py`只预留原计划中无消费证据的任务，并在源端留下
custody防重记录。`--model-key`可以重复指定当前已验收运行环境仍有未执行题的
模型；这只是执行波次子集，manifest必须同时保留原六模型名单和2394分母。
`--image-id`只筛选本来就使用该精确镜像ID的原任务，不修改recipe或接受可变tag；
导出清单不代表该镜像的宿主依赖已验收，更不授权直接批量执行新运行时。
`run_isolated_gpu_wave.py`拒绝不披露原完整范围、串换任务描述符或重复slot的波次。
子队列因serving/transport拒绝某模型后，后续cycle保留该model hold，
不再拿同一旧资格给其他未消费slot试启动。只有明确不同SHA的新已验证资格才能
解除hold；资格映射只在cycle边界重读，在途答案不重绑。
新运行时的后续波次使用`--prior-queue`指向单例波次，并加
`--require-prior-runtime-pilot`：除等待结束外，还核验同一原镜像、实际模型调用、
Docker工具轨迹、完整原C/J及patch/trace哈希；单例缺测时不放行后续生成。
若单例仅补过原C，可用`--prior-correctness-recovery`引用该精确补充；门还要求
GPU独立正式视图已经接受同一结果，原失败exit及已有G/J保持不变。
`--wait-for`可重复指定本GPU域的前置`RESULT.json`，用于在原队列或精确补缺
  流程真实排空后才运行下一波次。它只接受GPU-only终态ledger，不能以STATE、
  API队列或准备记录代替；前置缺测保留，不代表整张bench已经完成。
某模型在当前运行环境下没有剩余任务时，不能把其其余题视为已完成；需要继续
恢复并验收其他原镜像及宿主依赖。

Qwen27的原内部转发端口44991若被其他用户进程占用，不停止该进程。
显式`--qwen27-private-port`只在本次传输副本将内部端口映射到45991；
公共模型端口43991、原模型、SSH身份、预算和评测规则保持不变。

先在原GPU任务slot留下单例custody，防止两host重复生成。若controller尚未运行、
原attempt目录只有已知非零退出的原`PREFLIGHT.json`，可以显式
`--preflight-only-continuation`续接：旧attempt保留为`not_started`，下一次尝试使用
新序号。任何输出、STARTED、未知退出、其他attempt或owner存在都会拒绝该路径。
若原preflight已通过，但其后endpoint验证失败，则还必须显式提供
`--preflight-prior-case`；新保护核验原case/计划/终态/失败证据及不存在native开始，
不能把“成功preflight”单独当作重新执行许可。旧成功校准和attempt不删除。
