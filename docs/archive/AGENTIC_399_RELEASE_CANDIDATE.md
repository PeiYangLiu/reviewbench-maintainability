> **Historical release-candidate report — referenced package is not included.**
> This report preserves a dated 399-task, three-judge historical subset and its
> original limitations. The ignored `data/releases/agentic-exec/` candidate package
> and local verification assets are **absent from this publication copy**.
> Its reported verification is historical, not a check performed by this cleanup
> or a current Astra-only/no-Gemini evaluation. See the [archive index](README.md).
> Only this notice and workstation-root placeholders were added; original bytes
> remain in the separate backup.

# ReviewBench 399：任务与真实模型结果候选包

日期：2026-09-21（America/Los_Angeles）。

## 用户问题的直接答案

**已经有真实模型评测，不只是配置环境。现在已实际生成399题的任务和本轮
基线结果候选包。** 本包可离线读取、统计和检查全部候选及轨迹；它没有宣称
“提交机制修复后的统一新基线”或“干净机器全量复跑已经通过”。

| 本轮 GPT-5.4 Responses 历史结果 | 数量 |
|---|---:|
| 原题池 | 402 |
| 候选排除项 | 3 |
| 当前候选子集 | 399 |
| 完整模型评测：生成候选＋正确性＋三裁判 | 397 |
| 其中 resolved | 174 |
| 其中未解决 | 223 |
| 原生成失败终态，仍计入分母 | 2 |
| 子集成功率 | 174 / 399 = 43.61% |

397份正确性记录包括308 pass和89 fail，397份裁判记录均为completed。
它们是模型候选的结果，不是public/start/golden环境校准的数量。

## 已经落地的文件

项目根目录：
`${REVIEWBENCH_WORKSPACE}`

- 候选目录：`data/releases/agentic-exec/20260921-399-rc1/`
- 压缩包：`data/releases/agentic-exec/reviewbench-399-20260921-rc1.tar.gz`
- 压缩包SHA256：
  `927ecfedc145a2e4b54544a58266fd9ffb7cd6e364dec7de9637fdc87672b195`
- 压缩包大小：32,360,343 bytes。
- 包内文件：857项；包括公共题目、私有原任务、明确399题名单、排除原因、
  原结果和三阶段记录、399份patch（含两份原空patch）、399份轨迹、
  当前源码快照、依赖锁、说明和独立标准库验包脚本。
- 原始journal中的绝对路径保持历史字节；`artifacts.jsonl`提供包内相对路径，
  离线验包使用后者。

公共题目与带参考答案的私有任务分开。评分证据、答案与轨迹不作为solver输入。
当前源码副本用于检查实现，不被称为所有历史任务共用的单一执行引擎。

## 本次实际验证

1. 原publication104的authority/results/report/snapshot及三阶段文件哈希匹配。
2. 399条生成的任务指纹逐一匹配原任务；原保留结果行及三阶段文件字节不变。
3. 399份patch和399份trajectory逐一匹配原生成记录的SHA256。
4. 13项统计、重复ID、私有信息投影和证据一致性控制通过。
5. 真实BASELINE/MODIFIED/ROLLBACK：402 → 399 → 402，各exit0。
   回滚仅作用于另一份测试副本，候选目录和修改文件保持399题。
6. 真实压缩后解压到独立目录，完整gzip CRC通过，857项物理文件再次通过
   包内独立Python程序验证；压缩与恢复共7.095898724秒。
7. 模型、正确性测试、裁判的新调用均为0；没有为打包重跑已有实验。

在候选目录中复核：

```bash
python3 -B verify.py package --root .
sha256sum -c SHA256SUMS
```

真实包内验证输出：

```text
VERIFIED tasks=399 completed=397 model_failed=2 incomplete=0 resolved=174 complete=true resolved_rate=0.436090225564 files=857
```

该`complete=true`仅指399题子集有完整计分终态，包括两条失败。
原402题的`complete=false`以及原三项缺测保持不变。

## 排除口径与已有实验问题

- 根据用户提出的方案，将Agoric8072、Cosmos ibc-go3520、TiDB68028列为本地
  候选包的明确排除项。该决定发生在本轮运行后，原因写入`exclusions.json`；
  不把原题删除，也不宣称三题本身无效。未来比较模型时应固定相同399个ID。
- 原测试、裁判、模型答案及评分不变；本次没有执行此前提出的测试/裁判新协议。
- 旧执行器存在提交识别过严问题。既有审计的398条生成中365条
  `format_error_limit`曾输出提交标记但未被识别；未证明早停时的patch与得分
  不变。原结果、成本和停止原因保持原样，新增IoTDB不冒充原398条审计对象。
- 原399条生成存在14个执行引擎指纹：371条使用同一指纹，另外28条分布于13个
  指纹。候选包明确为历史结果子集，不把它改称修复后统一引擎的新实验。
- 本轮只包含一个solver的真实工具执行结果。旧14模型line-edit榜单是另一个
  协议，不被当作本轮的新多模型结果。

## “能交付”和“完全可复跑”的区别

**已经完成：**任务数据、本轮原始模型结果、补丁、轨迹、离线统计、
哈希核验及压缩包恢复的候选交付。

**尚未验证：**完整399题运行时资产在其他机器的取得与复原、逐题历史adapter/
engine绑定的可移植运行入口，以及干净机器端到端复跑。
`code/`中的默认配置仍是原模板，不是已经接通所有399题资产的开箱即用配置。
提交识别修复后的新基线也尚未生成。本地候选产出不等于已向外部公开发布。

## 证据与独立审核

工作目录（相对项目根）：
`.benchmarks/agentic-399-release-candidate-20260921-v1/`

- `BUILD_RESULT.json`：来源和候选哈希、保留计数。
- `ARCHIVE_VERIFIED.json`：实际压缩、CRC、物理恢复和子进程输出。
- `MODIFIED_FILE.ids`、`DIFF_FILE.patch`、`VERIFICATION.txt`、`ROLLBACK.sh`：
  真实402→399→402事务；逐条命令、输入、字面输出、退出状态。
- `readiness-audit/READINESS.md`、`readiness-audit/readiness.json`：
  并行独立审核，实际核对原计数、模型证据链、CLI帮助、引擎和终止审计口径。
  审核收件时归档仍在进行；最终成功结果由上述`ARCHIVE_VERIFIED.json`补齐。
  审核没有重复执行归档或把未重复执行的Main检查冒称独立验收。

本轮仅形成附加候选交付；原402 authority、线上reader和原始记录未被替换。
