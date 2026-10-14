> **Historical archive — not current package instructions or a current leaderboard.**
> Preserve the protocol, roster, denominator, incomplete/withdrawn flags and audit
> caveats stated below; do not reinterpret these results using today's judge policy.
> See the [archive index](README.md) and [current documentation](../README.md).
> Historical commands and local artifact references may require assets not shipped
> in this repository. Publication changes are limited to this notice, portable
> path placeholders and repaired links; original bytes remain in the backup.

# SFT 八臂四门(legacy 档补到 24/34)

生成时间:2026-09-02 08:01   记录数 **3064 / 3216**(未满)

> ⚠️ **这份自动生成的标题原本写「全部补齐」,那是错的** ——
> 它自己的记录数 3064/3216 就说明还差 152 条。已改正。
>
> 缺的 34 题属 `published_legacy` 档,对所有模型都没跑过(P109)。
> 目前**补回 24 题**(gopy/scripted 15 + jvm/tsjs 9),
> **余 10 题**(8 native + 2 rust:ClickHouse、cpython、rippled、dpnp、
> memilio、tsfile、singularity-eos、desbordante、fuel-tx、anchor)。
>
> 那 10 题一度**没有人接手** —— 负责 legacy 的 `chain_reserve.sh`
> 在 08:00 打完 `JVM_TSJS_DONE` 就退出了,而它要用的 `heavy_ids.txt` 是空的。
> 已算出 ID 并排队(`/tmp/heavy_legacy.sh`,等两条 native lane 和 api2 恢复之后启动)。
>
> **所以每臂现在是 383/402,不是 402。**

> **读表先看 `scored` 这一列。** `no data` 的行不是「零分」,
> 是它们的 correctness 在别的目录里(本次只传了 `--correctness correctness-native`)。
> 要看 RL 那几臂,补一个 `--correctness correctness-rl6`。

## 一、四门总表

```
model                                     judged  scored  Maint  Correct  FINAL  votes  bndry
----------------------------------------------------------------------------------------------
native-qwen3-14b                             376     355      7       54      6     45   257%
r16-nobanner-native-qwen3-14b                397     373      6       55      4     34   250%
r16-nobanner-native-qwen3-8b                 382     360      3       31      3     19   300%
native-qwen3-8b                              372     350      5       44      3     23   280%
nobanner-native-qwen3-14b                    392     370      5       38      2     18    40%
nobanner-native-qwen3-8b                     371     347      1       38      1     13   500%
base-qwen3-14b                               378     359      2       64      1     36   550%
rl-arm-sft                                   375       0      0  no data no data      0     0%
rl-arm-rft2                                  370       0      6  no data no data      0   233%
rl-arm-rft1                                  376       0      5  no data no data      0   200%
qwen3-8b-nothink-mtsft                       103       0      3  no data no data      0   167%
qwen3-8b-mtsft                               194       0      2  no data no data      0   400%
qwen3-4b-mtsft                               185       0      3  no data no data      0   133%
qwen3-32b-mtsft                              222       0      5  no data no data      0   160%
qwen3-14b-mtsft                              177       0      6  no data no data      0   117%
noise-rep3                                   370       0      1  no data no data      0   800%
noise-rep2                                   373       0      1  no data no data      0   800%
noise-rep1                                   371       0      3  no data no data      0   200%
base-qwen3-8b                                382     362      0       46      0     29     0%
b4k-rl-arm-sft                               374       0      2  no data no data      0   300%
b4k-rl-arm-rft2                              368       0      4  no data no data      0   225%
b4k-rl-arm-rft1                              376       0      3  no data no data      0   367%

WARNING: 53 task(s) lost a judge and are scored by an incomplete panel:
  b4k-rl-arm-rft1                             3
  b4k-rl-arm-rft2                             3
  b4k-rl-arm-sft                              3
  base-qwen3-14b                              3
  base-qwen3-8b                               3
  native-qwen3-14b                            3
  native-qwen3-8b                             3
  nobanner-native-qwen3-14b                   3
  noise-rep1                                  3
  noise-rep2                                  3
  noise-rep3                                  3
  r16-nobanner-native-qwen3-14b               3
  r16-nobanner-native-qwen3-8b                3
  rl-arm-rft1                                 3
  rl-arm-rft2                                 3
  nobanner-native-qwen3-8b                    2
  rl-arm-sft                                  2
  qwen3-14b-mtsft                             1
  qwen3-32b-mtsft                             1
  qwen3-8b-mtsft                              1
  qwen3-8b-nothink-mtsft                      1
  Verify these cannot change the ranking before publishing.

WARNING: 14 model(s) have no correctness rows at all -- their gates say 'no data', not zero:
  b4k-rl-arm-rft1
  b4k-rl-arm-rft2
  b4k-rl-arm-sft
  noise-rep1
  noise-rep2
  noise-rep3
  qwen3-14b-mtsft
  qwen3-32b-mtsft
  qwen3-4b-mtsft
  qwen3-8b-mtsft
  qwen3-8b-nothink-mtsft
  rl-arm-rft1
  rl-arm-rft2
  rl-arm-sft
  Point --correctness at the run that covers them.

WARNING: arms were scored over different task counts, so their
totals are not comparable. Check correctness.complete.json.
  b4k-rl-arm-rft1                              0
  b4k-rl-arm-rft2                              0
  b4k-rl-arm-sft                               0
  noise-rep1                                   0
  noise-rep2                                   0
  noise-rep3                                   0
  qwen3-14b-mtsft                              0
  qwen3-32b-mtsft                              0
  qwen3-4b-mtsft                               0
  qwen3-8b-mtsft                               0
  qwen3-8b-nothink-mtsft                       0
  rl-arm-rft1                                  0
  rl-arm-rft2                                  0
  rl-arm-sft                                   0
  nobanner-native-qwen3-8b                   347
  native-qwen3-8b                            350
  native-qwen3-14b                           355
  base-qwen3-14b                             359
  r16-nobanner-native-qwen3-8b               360
  base-qwen3-8b                              362
  nobanner-native-qwen3-14b                  370
  r16-nobanner-native-qwen3-14b              373
```

## 二、配对方向检验(连续口径,P104)

```
comparison                             gate      A    B  onlyA  onlyB        p
------------------------------------------------------------------------------
base-qwen3-8b:native-qwen3-8b          maint    53   92     30     57   0.0050**
base-qwen3-8b:native-qwen3-8b          correct   47   45     34     32   0.9022  
base-qwen3-8b:native-qwen3-8b          FINAL    21   12     16      7   0.0931  

comparison                             gate      A    B  onlyA  onlyB        p
------------------------------------------------------------------------------
base-qwen3-14b:native-qwen3-14b        maint    95  151     49     90   0.0006**
base-qwen3-14b:native-qwen3-14b        correct   65   54     45     34   0.2604  
base-qwen3-14b:native-qwen3-14b        FINAL    26   25     20     19   1.0000  

comparison                             gate      A    B  onlyA  onlyB        p
------------------------------------------------------------------------------
native-qwen3-8b:nobanner-native-qwen3-8b maint    89   74     43     40   0.8264  
native-qwen3-8b:nobanner-native-qwen3-8b correct   42   39     31     28   0.7948  
native-qwen3-8b:nobanner-native-qwen3-8b FINAL    10   10      6      6   1.0000  

comparison                             gate      A    B  onlyA  onlyB        p
------------------------------------------------------------------------------
native-qwen3-14b:nobanner-native-qwen3-14b maint   155  108     79     48   0.0075**
native-qwen3-14b:nobanner-native-qwen3-14b correct   54   39     39     24   0.0769  
native-qwen3-14b:nobanner-native-qwen3-14b FINAL    25   10     22      7   0.0081**

```

## 三、失败模式拆桶(P107)

```
model                                     passed     regressed  build_broken  tests_failed  out_of_scope      no_patch    apply_fail         infra  unclassified      n
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
base-qwen3-14b                             17.0%         26.4%         19.6%         10.4%          6.0%          2.3%          0.5%          3.4%         14.4%    383
r16-nobanner-native-qwen3-14b              14.4%         31.1%         19.3%         13.6%          1.6%          0.3%          0.0%          3.7%         16.2%    383
native-qwen3-14b                           14.1%         28.5%         18.0%         13.3%          3.1%          2.3%          1.3%          3.9%         15.4%    383
base-qwen3-8b                              12.3%         28.7%         21.4%         12.3%          6.3%          1.0%          1.0%          2.3%         14.6%    383
native-qwen3-8b                            11.7%         27.4%         21.7%         12.3%          3.4%          5.0%          0.5%          3.4%         14.6%    383
nobanner-native-qwen3-14b                  10.4%         30.5%         21.7%         12.8%          2.1%          0.3%          0.5%          4.2%         17.5%    383
nobanner-native-qwen3-8b                   10.2%         25.6%         21.7%         14.9%          1.8%          4.7%          2.1%          3.7%         15.4%    383
r16-nobanner-native-qwen3-8b                8.1%         31.3%         21.7%         14.9%          1.3%          3.9%          0.0%          4.2%         14.6%    383

share of failures that are the patch breaking passing tests:
  base-qwen3-14b                      44.3%   (baseline-confirmed 31.8%)
  r16-nobanner-native-qwen3-14b       52.1%   (baseline-confirmed 36.3%)
  native-qwen3-14b                    48.6%   (baseline-confirmed 33.1%)
  base-qwen3-8b                       46.7%   (baseline-confirmed 32.7%)
  native-qwen3-8b                     45.0%   (baseline-confirmed 31.1%)
  nobanner-native-qwen3-14b           48.4%   (baseline-confirmed 34.1%)
  nobanner-native-qwen3-8b            45.1%   (baseline-confirmed 28.5%)
  r16-nobanner-native-qwen3-8b        50.3%   (baseline-confirmed 34.1%)

unclassified is mostly raw build output that the root-cause matcher
did not recognise, not a distinct failure mode, so the share above is
a lower bound.
```
