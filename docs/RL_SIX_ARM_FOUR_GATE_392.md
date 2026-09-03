# RL 六臂四门（阶段结果：392/402 题/臂）

生成时间：2026-09-02 18:50；correctness.complete.json：`complete=false`，**2352/2412** 条。

> ⚠️ 这不是最终板。modern 368 题已齐，published_legacy 34 题中已补 24 题；每臂仍缺 10 题（合计 60 条）。
> 旧自动文件名写 `COMPLETE`、标题写“402 题补齐后”，均为错误声明，已撤下。

## 一、六臂四门总表

```
model                                     judged  scored  Maint  Correct  FINAL  votes  bndry
----------------------------------------------------------------------------------------------
rl-arm-rft2                                  370     355      6       57      4     30   233%
rl-arm-rft1                                  376     363      5       51      2     25   200%
noise-rep1                                   371     356      3       54      2     19   200%
noise-rep3                                   370     358      1       45      1     16   800%
noise-rep2                                   373     360      1       45      1     14   800%
rl-arm-sft                                   375     362      0       48      0     12     0%

WARNING: 17 task(s) lost a judge and are scored by an incomplete panel:
  noise-rep1                                  3
  noise-rep2                                  3
  noise-rep3                                  3
  rl-arm-rft1                                 3
  rl-arm-rft2                                 3
  rl-arm-sft                                  2
  Verify these cannot change the ranking before publishing.

WARNING: arms were scored over different task counts, so their
totals are not comparable. Check correctness.complete.json.
  rl-arm-rft2                                355
  noise-rep1                                 356
  noise-rep3                                 358
  noise-rep2                                 360
  rl-arm-sft                                 362
  rl-arm-rft1                                363
```

> `scored` 小于 392 是因为 `infra_retry` 与判官拒判不被伪装成模型失败。六臂尝试的题集一致，组内配对检验只用共同有效题。

## 二、配对方向检验（连续票数口径，含同口径噪声地板）

```
noise floor -- replicates of one configuration:
comparison                             gate      A    B  onlyA  onlyB        p
------------------------------------------------------------------------------
noise-rep1:noise-rep2                  maint    60   61     28     30   0.8957
noise-rep1:noise-rep3                  maint    62   55     38     30   0.3961
noise-rep2:noise-rep3                  maint    57   58     36     35   1.0000
  -> rerunning the same configuration gives p as low as 0.3961; a comparison must beat that on the same test, not on totals.

comparison                             gate      A    B  onlyA  onlyB        p
------------------------------------------------------------------------------
rl-arm-sft:rl-arm-rft1                 maint    56   84     34     56   0.0263**
rl-arm-sft:rl-arm-rft1                 correct   49   53     33     37   0.7202  
rl-arm-sft:rl-arm-rft1                 FINAL     9   14      7     12   0.3593  
rl-arm-sft:rl-arm-rft2                 maint    54  106     24     62   0.0001**
rl-arm-sft:rl-arm-rft2                 correct   50   58     33     41   0.4160  
rl-arm-sft:rl-arm-rft2                 FINAL     7   16      4     13   0.0490**
rl-arm-rft1:rl-arm-rft2                maint    86  110     36     55   0.0586  
rl-arm-rft1:rl-arm-rft2                correct   53   58     25     30   0.5901  
rl-arm-rft1:rl-arm-rft2                FINAL    15   17      9     11   0.8238  
```

## 三、失败模式拆桶

```
model                                     passed     regressed  build_broken  tests_failed  out_of_scope      no_patch    apply_fail         infra  unclassified      n
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
rl-arm-rft2                                14.8%         25.5%         17.1%         14.8%          1.8%          5.9%          1.0%          3.3%         15.8%    392
noise-rep1                                 14.3%         25.8%         18.9%         13.5%          2.8%          5.6%          0.8%          3.3%         15.1%    392
rl-arm-rft1                                13.5%         27.3%         18.6%         15.3%          2.3%          4.8%          0.5%          3.1%         14.5%    392
rl-arm-sft                                 12.8%         27.3%         18.4%         15.1%          2.0%          5.4%          0.5%          4.1%         14.5%    392
noise-rep2                                 12.0%         30.4%         16.6%         14.8%          1.8%          5.6%          0.5%          3.8%         14.5%    392
noise-rep3                                 11.5%         28.1%         18.4%         15.6%          1.3%          7.1%          0.3%          2.8%         15.1%    392

share of failures that are the patch breaking passing tests:
  rl-arm-rft2                         47.3%   (baseline-confirmed 29.9%)
  noise-rep1                          45.8%   (baseline-confirmed 30.1%)
  rl-arm-rft1                         49.3%   (baseline-confirmed 31.6%)
  rl-arm-sft                          48.5%   (baseline-confirmed 31.3%)
  noise-rep2                          51.3%   (baseline-confirmed 34.5%)
  noise-rep3                          49.3%   (baseline-confirmed 31.7%)

unclassified is mostly raw build output that the root-cause matcher
did not recognise, not a distinct failure mode, so the share above is
a lower bound.
```

**当前可用结论**：RFT-2 相对 SFT 的 maintainability 票数 54→106，p=0.0001；correctness 子维度未显著变化。最终总数与 FINAL 必须等最后 10 题补齐后再发布。
