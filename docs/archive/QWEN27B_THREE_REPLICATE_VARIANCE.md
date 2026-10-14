> **Historical archive — not current package instructions or a current leaderboard.**
> Preserve the protocol, roster, denominator, incomplete/withdrawn flags and audit
> caveats stated below; do not reinterpret these results using today's judge policy.
> See the [archive index](README.md) and [current documentation](../README.md).
> Historical commands and local artifact references may require assets not shipped
> in this repository. Publication changes are limited to this notice, portable
> path placeholders and repaired links; original bytes remain in the backup.

# Qwen3.6-27B 三副本方差实验

生成实验：`qwen27b-reps-20260902`  
副本：`r1` / `r2` / `r3`，每个 402 题，8,192 输出预算。

> Claude 当前不可用。生成侧分析不依赖判官；质量侧使用固定的
> `gpt-5.6-sol + deepseek-v3.2 + gemini-3.6-flash` 非 Claude 面板。
> 1206 个 task-model pair 已全部 attested（1166 judged + 40 generation_failed）。
> 该面板只估计同配置副本方差，不与旧三判官绝对分数混用。

## 一、生成成功率

| 副本 | generated | failed | 成功率 | 中位轮数 | 中位改动行 | 中位耗时 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| r1 | 393 | 9 | 97.8% | 10 | 41 | 545.3s |
| r2 | 386 | 16 | 96.0% | 10 | 41 | 631.8s |
| r3 | 387 | 15 | 96.3% | 10 | 39 | 387.4s |

成功数均值 **388.67**，样本标准差 **3.79 题**，极差 **7 题
（1.7 个百分点）**。成功数本身的 CV 是 **0.97%**。

## 二、逐题稳定性

| 三次中成功次数 | 题数 | 含义 |
| ---: | ---: | --- |
| 3 | 374 | 三次都成功 |
| 2 | 17 | 一次失败 |
| 1 | 10 | 两次失败 |
| 0 | 1 | 三次都失败 |

**375/402（93.3%）的成功/失败状态稳定；27/402（6.7%）会跨副本翻转。**

| 副本对 | 状态一致 | 仅前者成功 | 仅后者成功 |
| --- | ---: | ---: | ---: |
| r1 / r2 | 385/402（95.8%） | 12 | 5 |
| r1 / r3 | 386/402（96.0%） | 11 | 5 |
| r2 / r3 | 381/402（94.8%） | 10 | 11 |

失败题的重叠很小：r1/r2、r1/r3、r2/r3 分别只重叠 **4/4/5 题**，
三次都失败仅 **1 题**。所以“失败 9–16 题”看起来稳定，但**失败的是谁并不稳定**。

40 个失败记录**全部跑满 32 轮**，没有一个是服务或协议错误。
它们不是“没产出任何内容”，而是持续 read/find/edit 后没有在轮数上限前成功 finish。
所以这里测到的是 agent 终止行为的随机性，不是 endpoint 可用性。

## 三、patch 级随机性

在两次都成功的题中，patch SHA-256 完全相同的比例只有：

| 副本对 | 两次都成功 | patch 完全相同 | 比例 |
| --- | ---: | ---: | ---: |
| r1 / r2 | 381 | 20 | 5.2% |
| r1 / r3 | 382 | 15 | 3.9% |
| r2 / r3 | 376 | 16 | 4.3% |

**生成成功率稳定，不代表生成内容稳定。**同一题同一模型约 95% 会产出 patch，
但约 95% 的成功题在另一个副本会产出不同 patch。

## 四、非 Claude 面板下的质量方差

| 副本 | generated/judged | generation failed | Sol 通过 | DeepSeek 通过 | Gemini 通过 | 三判总票 | 全票通过 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| r1 | 393 | 9 | 149 | 219 | 301 | **669** | **102** |
| r2 | 386 | 16 | 150 | 224 | 307 | **681** | **117** |
| r3 | 387 | 15 | 152 | 216 | 304 | **672** | **115** |

三个单判官自身都稳定：Sol 的通过数极差 3，DeepSeek 极差 8，Gemini 极差 6。
三判总票同样稳定：

| 口径 | 三副本 | 样本 SD | CV | 极差 |
| --- | --- | ---: | ---: | ---: |
| **总票数（0–3/题）** | 669 / 681 / 672 | 6.24 | **0.93%** | 12 |
| **多数通过（≥2票）** | 240 / 244 / 238 | 3.06 | **1.27%** | 6 |
| **全票通过（3票）** | 102 / 117 / 115 | 8.14 | **7.32%** | 15 |

**全票阈值把同一批稳定票数的相对波动放大了 7.9 倍。**
原因直接写在分布里：

| 副本 | 0 票 | 1 票 | **2 票（边界）** | 3 票 |
| --- | ---: | ---: | ---: | ---: |
| r1 | 75 | 87 | **138** | 102 |
| r2 | 82 | 76 | **127** | 117 |
| r3 | 83 | 81 | **123** | 115 |

每个副本都有 123–138 道题卡在“差一票全票通过”的边界。
因此少量判官波动会把全票分数推来推去，但不会明显改变总票。

全 402 题配对方向检验（generation failure 记 0 票）：

| 副本对 | 总票 | 仅前者更高 | 仅后者更高 | p |
| --- | ---: | ---: | ---: | ---: |
| r1 / r2 | 669 / 681 | 98 | 111 | 0.4066 |
| r1 / r3 | 669 / 672 | 97 | 111 | 0.3674 |
| r2 / r3 | 681 / 672 | 109 | 102 | 0.6797 |

**三对都与重复运行噪声无法区分。**只在两边都 generated/judged 的题上重算，
最低 p 也只有 0.1746，结论不变。

## 五、当前结论

1. 27B 的“能否交卷”噪声地板约为 **±4 题（1σ）**，三副本极差 7 题；
2. 逐题约 **6.7%** 会在交卷/不交卷之间翻转；
3. patch 内容高度随机，不能把单次生成当成该模型对一道题的唯一答案；
4. 任何小于约 7 题的边际成功数差异，不应解释成模型或干预效果；
5. 可维护性**总票数**噪声很小（CV 0.93%），适合作为训练迭代指标；
6. **全票分数 CV 7.32%，被阈值放大 7.9 倍，不适合迭代或比较接近模型**；
7. 这次 27B 实验独立复现了 P104：二值全票门会毁掉本来稳定的训练信号。

原始数据：
`${REVIEWBENCH_WORKSPACE}/reviewbench-openmodels/27b-reps-results/qwen27b-reps-20260902/`

非 Claude 判分：
`${REVIEWBENCH_WORKSPACE}/reviewbench-openmodels/judge-qwen27b-reps-noclaude/`
