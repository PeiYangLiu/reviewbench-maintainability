# RL 六臂四门（402/402 题已尝试）

生成时间：2026-09-03 23:20

> 2412/2412 rows; infra_retry excluded=21; complete=false
>  只表示仍有明确的基础设施失败；这些题不伪装成模型失败。

## 一、六臂四门总表

```
model                                     judged  scored  Maint  Correct  FINAL  votes  bndry
----------------------------------------------------------------------------------------------
rl-arm-rft2                                  370     365      6       57      4     30   233%
rl-arm-rft1                                  376     373      5       53      2     25   200%
noise-rep1                                   371     366      3       54      2     19   200%
noise-rep3                                   370     368      1       45      1     16   800%
noise-rep2                                   373     370      1       45      1     14   800%
rl-arm-sft                                   375     372      0       49      0     12     0%

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
  rl-arm-rft2                                365
  noise-rep1                                 366
  noise-rep3                                 368
  noise-rep2                                 370
  rl-arm-sft                                 372
  rl-arm-rft1                                373
```

## 二、配对方向检验（连续票数口径，同次输出噪声地板）

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
rl-arm-sft:rl-arm-rft1                 correct   50   55     34     39   0.6400  
rl-arm-sft:rl-arm-rft1                 FINAL     9   14      7     12   0.3593  
rl-arm-sft:rl-arm-rft2                 maint    54  106     24     62   0.0001**
rl-arm-sft:rl-arm-rft2                 correct   51   58     34     41   0.4887  
rl-arm-sft:rl-arm-rft2                 FINAL     7   16      4     13   0.0490**
rl-arm-rft1:rl-arm-rft2                maint    86  110     36     55   0.0586  
rl-arm-rft1:rl-arm-rft2                correct   55   58     27     30   0.7914  
rl-arm-rft1:rl-arm-rft2                FINAL    15   17      9     11   0.8238  
```

## 三、失败模式

```
model                                     passed     regressed  build_broken  tests_failed  out_of_scope      no_patch    apply_fail         infra  unclassified      n
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
rl-arm-rft2                                14.4%         24.9%         16.7%         16.9%          1.7%          5.7%          1.0%          3.2%         15.4%    402
noise-rep1                                 13.9%         25.1%         18.4%         15.7%          2.7%          5.5%          0.7%          3.2%         14.7%    402
rl-arm-rft1                                13.7%         26.6%         18.2%         16.9%          2.2%          4.7%          0.5%          3.0%         14.2%    402
rl-arm-sft                                 12.7%         26.6%         17.9%         16.9%          2.0%          5.2%          0.5%          4.0%         14.2%    402
noise-rep2                                 11.7%         29.6%         16.2%         16.9%          1.7%          5.5%          0.5%          3.7%         14.2%    402
noise-rep3                                 11.2%         27.4%         17.9%         17.7%          1.2%          7.0%          0.2%          2.7%         14.7%    402

share of failures that are the patch breaking passing tests:
  rl-arm-rft2                         48.8%   (baseline-confirmed 29.1%)
  noise-rep1                          47.4%   (baseline-confirmed 29.2%)
  rl-arm-rft1                         50.4%   (baseline-confirmed 30.8%)
  rl-arm-sft                          49.9%   (baseline-confirmed 30.5%)
  noise-rep2                          52.7%   (baseline-confirmed 33.5%)
  noise-rep3                          50.7%   (baseline-confirmed 30.8%)

unclassified is mostly raw build output that the root-cause matcher
did not recognise, not a distinct failure mode, so the share above is
a lower bound.
```

## 四、结论

- **RFT-2 的 maintainability 提升成立**：SFT 54 → RFT-2 106 票，
  配对 p=0.0001；明显超过三对重复副本的噪声地板（最低 p=0.3961）。
- **correctness 仍未改善**：SFT vs RFT-2，51 vs 58，p=0.4887。
- **FINAL 的边缘 p=0.0490 不作为主结论**：全票阈值对重复运行方差敏感。
- 最后 10 道重题只给 SFT/RFT1/RFT2 新增 1/2/0 个 correctness pass，
  六臂 FINAL 总数全部不变。

最终解释保持不变：**RL 把“手”训好了，没有把“眼睛”训好。**
