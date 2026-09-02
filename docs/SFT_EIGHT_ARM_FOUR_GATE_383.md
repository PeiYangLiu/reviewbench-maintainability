# SFT 八臂四门(legacy 档补齐后:383 题/臂)

生成时间:2026-09-02 00:59　记录数 2944 → **3064** / 3216

> 此前是 368 题/臂。缺的 34 题属 `published_legacy` 档,对所有模型都没跑过(P109),
> 现已补回 15 题(gopy/scripted),余 19 题(jvm/tsjs/native/rust)在跑。

## 一、四门总表

```
model                                     judged  Maint  Correct  FINAL  votes  bndry
--------------------------------------------------------------------------------------
native-qwen3-14b                             376      7       54      6     45   257%
r16-nobanner-native-qwen3-14b                397      6       55      4     34   250%
r16-nobanner-native-qwen3-8b                 382      3       31      3     19   300%
native-qwen3-8b                              372      5       44      3     23   280%
nobanner-native-qwen3-14b                    392      5       38      2     18    40%
nobanner-native-qwen3-8b                     371      1       38      1     13   500%
base-qwen3-14b                               378      2       64      1     36   550%
rl-arm-sft                                   375      0        0      0      0     0%
rl-arm-rft2                                  370      6        0      0      0   233%
rl-arm-rft1                                  376      5        0      0      0   200%
```

⚠️ **`bndry` 列 250–550%** —— 这些臂的 `FINAL` 完全由「差一票的题这次翻没翻」决定,
**不可用于比较**。下面一律用 `votes`(连续口径,P104)。

## 二、配对方向检验(连续口径)

```
------------------------------------------------------------------------------
base-qwen3-8b:native-qwen3-8b          maint    53   92     30     57   0.0050**
base-qwen3-8b:native-qwen3-8b          correct   47   45     34     32   0.9022  
base-qwen3-8b:native-qwen3-8b          FINAL    21   12     16      7   0.0931  

------------------------------------------------------------------------------
base-qwen3-14b:native-qwen3-14b        maint    95  151     49     90   0.0006**
base-qwen3-14b:native-qwen3-14b        correct   65   54     45     34   0.2604  
base-qwen3-14b:native-qwen3-14b        FINAL    26   25     20     19   1.0000  

```

**补齐 15 题后结论不变,而且更强**:

| 对比 | maint 票数 | 提升 | p |
| --- | ---: | ---: | ---: |
| 8B 基座 → 第二版训练 | 53 → 92 | **+74%** | **0.0050** |
| 14B 基座 → 第二版训练 | 95 → 151 | **+59%** | **0.0006** |

`correct` 门 p=0.90 / 0.26 —— **训练没有改善 correctness**,与 P107 一致。

## 三、失败模式拆桶(P107)

```
model                                     passed     regressed  tests_failed      no_patch    apply_fail         infra  unclassified      n
-------------------------------------------------------------------------------------------------------------------------------------------
base-qwen3-14b                             17.0%         46.0%          6.5%          2.3%          6.3%          3.4%         18.5%    383
r16-nobanner-native-qwen3-14b              14.4%         50.4%          8.9%          0.3%          1.3%          3.7%         21.1%    383
native-qwen3-14b                           14.1%         46.5%          8.9%          2.3%          4.4%          3.9%         19.8%    383
base-qwen3-8b                              12.3%         50.1%          7.6%          1.0%          7.0%          2.3%         19.6%    383
native-qwen3-8b                            11.7%         49.1%          7.8%          5.0%          3.9%          3.4%         19.1%    383
nobanner-native-qwen3-14b                  10.4%         52.2%          8.4%          0.3%          2.6%          4.2%         21.9%    383
nobanner-native-qwen3-8b                   10.2%         47.3%         10.2%          4.7%          3.9%          3.7%         20.1%    383
r16-nobanner-native-qwen3-8b                8.1%         53.0%          9.7%          3.9%          1.0%          4.2%         20.1%    383

share of failures that are the patch breaking passing tests:
```
