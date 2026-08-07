# ReviewBench 306-Task Three-Model Evaluation

评测日期：2026-07-24

本次评测覆盖：

- v1 正式集：100 道，复用 2026-07-14 冻结结果
- 20260721 strict reserve：206 道，重新执行三模型生成、correctness 和维护性盲裁
- 合计：306 道

## 最终总榜

| 模型 | 成功生成补丁 | Correctness | 维护性通过 | Resolved@1 | Hard |
|---|---:|---:|---:|---:|---:|
| **GPT-5.5** | 303/306 | 218/306 | 232/306 | **184/306** | **12/26** |
| Gemini 3.5 Flash | 302/306 | **224/306** | **241/306** | **182/306** | **12/26** |
| Claude Opus 4.8 | **306/306** | 219/306 | 233/306 | **180/306** | 10/26 |

总体排名：

1. GPT-5.5：184
2. Gemini 3.5 Flash：182
3. Claude Opus 4.8：180

## 新增 206 题结果

| 模型 | 成功生成补丁 | Correctness | 维护性通过 | Resolved@1 | Hard |
|---|---:|---:|---:|---:|---:|
| **GPT-5.5** | 205/206 | 145/206 | 159/206 | **123/206** | 5/15 |
| Claude Opus 4.8 | **206/206** | 143/206 | 156/206 | **117/206** | 5/15 |
| Gemini 3.5 Flash | 205/206 | **146/206** | **162/206** | **116/206** | **6/15** |

## 判分口径

`Resolved@1` 要求四门同时通过：

1. 模型输出能转换为非空、可应用补丁。
2. 只修改允许的锚点生产文件，改动不超过 400 行。
3. 已验证的 PASS_TO_PASS correctness oracle 通过。
4. 严格维护性盲裁通过。

生成提示只包含 reviewer concern 和 start revision 源码，不包含 target、golden、quality judgment 或 oracle 结果。候选补丁没有人工修复。

这里的“成功生成补丁”不是说 Git 随后可能随机应用失败。模型先输出 SEARCH/REPLACE；只有 SEARCH 能定位精确 start 源码时，系统才生成规范 unified diff。306 题评测中共生成 911 个补丁，这 911 个补丁在 correctness 环节全部成功 `git apply`。另外 7 个 task-model 组合未生成补丁文件。

维护性裁判沿用 Claude Opus 4.8，候选模型身份隐藏。Claude 同时是候选之一，因此仍存在无法完全排除的自裁偏差。

## 成对结果

| 模型对 | 两者都解出 | 左侧独有 | 右侧独有 | 两者都未解出 |
|---|---:|---:|---:|---:|
| Claude vs Gemini | 154 | 26 | 28 | 98 |
| Claude vs GPT-5.5 | 155 | 25 | 29 | 97 |
| Gemini vs GPT-5.5 | 151 | 31 | 33 | 91 |

## 文件

| 路径 | 内容 |
|---|---|
| `report.json` | 完整总榜、难度、语言、类别和成对统计 |
| `results.jsonl` | 918 个 task-model 的四门结果 |
| `generation.jsonl` | 918 条生成状态与补丁路径 |
| `correctness.jsonl` | 918 条归一化 correctness 结果 |
| `judgments.jsonl` | 918 条维护性盲裁结果 |
| `patches/<model>/` | 911 个可应用模型补丁 |
| `manifest.json` | 机器可读配置和总结果 |
| `SHA256SUMS` | 全部评测产物哈希 |

所有 JSONL 按任务 `id` 和模型名称连接，不能依赖物理行号。
