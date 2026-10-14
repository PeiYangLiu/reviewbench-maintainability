> **Historical archive — not current package instructions or a current leaderboard.**
> Preserve the protocol, roster, denominator, incomplete/withdrawn flags and audit
> caveats stated below; do not reinterpret these results using today's judge policy.
> See the [archive index](README.md) and [current documentation](../README.md).
> Historical commands and local artifact references may require assets not shipped
> in this repository. Publication changes are limited to this notice, portable
> path placeholders and repaired links; original bytes remain in the backup.

# 主榜 14 模型（402/402 题已尝试）

生成时间：2026-09-03 23:20

> 11-model run: 4422/4422 rows; infra_retry excluded=35; complete=false
> 3-model API run: 1206/1206 rows; infra_retry excluded=10; complete=false

```
model                                     judged  scored  Maint  Correct  FINAL  votes  bndry
----------------------------------------------------------------------------------------------
claude-opus-5                                370     366    160      320    140    566    32%
gpt-5.6-sol                                  397     394    149      344    126    608    60%
gpt-5.5                                      397     394    153      337    122    567    43%
gpt-5.6-terra                                398     395    127      316    107    508    57%
claude-opus-4.8                              385     382    106      342     94    440    51%
gpt-5.6-luna                                 399     396     96      282     82    435    75%
gemini-3.6-flash                             288     285     93      214     69    317    55%
claude-sonnet-5                              235     232     72      216     67    321    54%
Qwen/Qwen3.6-27B                             382     379     85      221     59    345    93%
MiniMaxAI/MiniMax-M2.1                       378     374     37      119     30    169    95%
Qwen/Qwen3.6-35B-A3B                         392     389     41      187     27    235   180%
mistralai/Devstral-2-123B-Instruct-2512      365     362     20      137     17    116   145%
Qwen/Qwen3-Coder-Next-FP8                    355     352     23      119     17    118   135%
openai/gpt-oss-120b                          395     391      9      186      8    104   278%

WARNING: 36 task(s) lost a judge and are scored by an incomplete panel:
  Qwen/Qwen3.6-27B                            4
  Qwen/Qwen3.6-35B-A3B                        4
  mistralai/Devstral-2-123B-Instruct-2512     4
  claude-opus-4.8                             3
  gpt-5.5                                     3
  gpt-5.6-luna                                3
  gpt-5.6-sol                                 3
  gpt-5.6-terra                               3
  openai/gpt-oss-120b                         3
  Qwen/Qwen3-Coder-Next-FP8                   2
  gemini-3.6-flash                            2
  MiniMaxAI/MiniMax-M2.1                      1
  claude-sonnet-5                             1
  Verify these cannot change the ranking before publishing.

WARNING: arms were scored over different task counts, so their
totals are not comparable. Check correctness.complete.json.
  claude-sonnet-5                            232
  gemini-3.6-flash                           285
  Qwen/Qwen3-Coder-Next-FP8                  352
  mistralai/Devstral-2-123B-Instruct-2512    362
  claude-opus-5                              366
  MiniMaxAI/MiniMax-M2.1                     374
  Qwen/Qwen3.6-27B                           379
  claude-opus-4.8                            382
  Qwen/Qwen3.6-35B-A3B                       389
  openai/gpt-oss-120b                        391
  gpt-5.5                                    394
  gpt-5.6-sol                                394
  gpt-5.6-terra                              395
  gpt-5.6-luna                               396
```

## 与此前 368 题 correctness 榜的对比

补入 34 道 `published_legacy` 题后，**14 个模型排名 0 个变化**：

| # | 模型 | Correct 旧→新 | FINAL 旧→新 |
| ---: | --- | ---: | ---: |
| 1 | claude-opus-5 | 291 → **320** | 120 → **140** |
| 2 | gpt-5.6-sol | 314 → **344** | 109 → **126** |
| 3 | gpt-5.5 | 307 → **337** | 108 → **122** |
| 4 | gpt-5.6-terra | 286 → **316** | 92 → **107** |
| 5 | claude-opus-4.8 | 310 → **342** | 80 → **94** |
| 6 | gpt-5.6-luna | 259 → **282** | 73 → **82** |
| 7 | gemini-3.6-flash | 198 → **214** | 59 → **69** |
| 8 | claude-sonnet-5 | 196 → **216** | 58 → **67** |
| 9 | Qwen/Qwen3.6-27B | 200 → **221** | 52 → **59** |
| 10 | MiniMaxAI/MiniMax-M2.1 | 114 → **119** | 28 → **30** |
| 11 | Qwen/Qwen3.6-35B-A3B | 174 → **187** | 25 → **27** |
| 12 | Qwen/Qwen3-Coder-Next-FP8 | 108 → **119** | 16 → **17** |
| 13 | mistralai/Devstral-2-123B-Instruct-2512 | 130 → **137** | 16 → **17** |
| 14 | openai/gpt-oss-120b | 169 → **186** | 8 → **8** |

Maint 不变，因为 402 题判分原本就齐；变化全部来自 correctness 的
legacy 补齐。**旧榜的排序结论没有被 34 题缺口推翻。**

## 数据状态

- 两个 correctness run 都达到 `records == expected`，missing=0；
- 45 条 `infra_retry`（11-model run 35、API run 10）显式排除；
- API 恢复目录从逐题丢失事故后的 45 行恢复到 **1206/1206**；
- Sentry 单点在安静机器上累计重试 6 次仍为 infra，因此没有把旧文档的
  291 硬凑回来；402 题新榜按可复现的当前证据计分。
