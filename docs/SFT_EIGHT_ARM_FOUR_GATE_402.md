# SFT 八臂四门（402/402 题已尝试）

生成时间：2026-09-03 18:40

> correctness 覆盖 **3216/3216**，missing=0；179 条历史 `infra_retry` 显式排除，不伪装成模型失败。

> TS/JS 历史 runner 没有保存顶层 patch hash，但显式保存 `patch_unmodified=true`、`model_identity_preserved=true` 与 `generation_status=generated`；finalize 仅在这三项同时成立时接受，无 hash 且无 attestation 仍 fail-closed。

## 一、八臂四门总表

```
model                                     judged  scored  Maint  Correct  FINAL  votes  bndry
----------------------------------------------------------------------------------------------
native-qwen3-14b                             376     354      7       53      6     45   257%
r16-nobanner-native-qwen3-8b                 382     359      3       31      3     19   300%
r16-nobanner-native-qwen3-14b                397     373      6       51      3     31   250%
native-qwen3-8b                              372     351      5       43      3     23   280%
nobanner-native-qwen3-14b                    392     371      5       37      2     17    40%
nobanner-native-qwen3-8b                     371     346      1       38      1     12   500%
base-qwen3-14b                               378     357      2       61      1     34   550%
base-qwen3-8b                                382     360      0       43      0     28     0%

WARNING: 23 task(s) lost a judge and are scored by an incomplete panel:
  base-qwen3-14b                              3
  base-qwen3-8b                               3
  native-qwen3-14b                            3
  native-qwen3-8b                             3
  nobanner-native-qwen3-14b                   3
  r16-nobanner-native-qwen3-14b               3
  r16-nobanner-native-qwen3-8b                3
  nobanner-native-qwen3-8b                    2
  Verify these cannot change the ranking before publishing.

WARNING: arms were scored over different task counts, so their
totals are not comparable. Check correctness.complete.json.
  nobanner-native-qwen3-8b                   346
  native-qwen3-8b                            351
  native-qwen3-14b                           354
  base-qwen3-14b                             357
  r16-nobanner-native-qwen3-8b               359
  base-qwen3-8b                              360
  nobanner-native-qwen3-14b                  371
  r16-nobanner-native-qwen3-14b              373
```

## 二、训练主结论（连续票数口径，同次输出噪声地板）

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
base-qwen3-8b:native-qwen3-8b          maint    53   92     30     57   0.0050**
base-qwen3-8b:native-qwen3-8b          correct   44   44     32     32   1.0000  
base-qwen3-8b:native-qwen3-8b          FINAL    20   12     15      7   0.1338  
base-qwen3-14b:native-qwen3-14b        maint    95  151     49     90   0.0006**
base-qwen3-14b:native-qwen3-14b        correct   62   53     43     34   0.3620  
base-qwen3-14b:native-qwen3-14b        FINAL    25   25     19     19   1.0000  
```

## 三、失败模式

```
model                                     passed     regressed  build_broken  tests_failed  out_of_scope      no_patch    apply_fail         infra  unclassified      n
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------
base-qwen3-14b                             15.4%         25.1%         18.7%         10.9%          9.5%          2.2%          1.2%          3.2%         13.7%    402
native-qwen3-14b                           13.2%         27.1%         17.2%         12.7%          7.5%          2.2%          1.7%          3.7%         14.7%    402
r16-nobanner-native-qwen3-14b              12.7%         29.6%         18.4%         13.9%          6.2%          0.2%          0.0%          3.5%         15.4%    402
base-qwen3-8b                              10.9%         27.4%         20.4%         12.4%         10.2%          1.0%          1.5%          2.2%         13.9%    402
native-qwen3-8b                            10.9%         26.1%         20.6%         12.4%          7.5%          4.7%          0.5%          3.2%         13.9%    402
nobanner-native-qwen3-14b                   9.7%         29.1%         20.6%         12.7%          6.5%          0.2%          0.5%          4.0%         16.7%    402
nobanner-native-qwen3-8b                    9.7%         24.4%         20.6%         14.2%          6.2%          4.5%          2.2%          3.5%         14.7%    402
r16-nobanner-native-qwen3-8b                7.7%         29.9%         20.6%         13.9%          6.0%          3.7%          0.2%          4.0%         13.9%    402

share of failures that are the patch breaking passing tests:
  base-qwen3-14b                      42.6%   (baseline-confirmed 29.7%)
  native-qwen3-14b                    45.8%   (baseline-confirmed 31.2%)
  r16-nobanner-native-qwen3-14b       49.9%   (baseline-confirmed 33.9%)
  base-qwen3-8b                       44.7%   (baseline-confirmed 30.7%)
  native-qwen3-8b                     43.3%   (baseline-confirmed 29.3%)
  nobanner-native-qwen3-14b           46.3%   (baseline-confirmed 32.2%)
  nobanner-native-qwen3-8b            42.7%   (baseline-confirmed 27.0%)
  r16-nobanner-native-qwen3-8b        47.4%   (baseline-confirmed 32.3%)

unclassified is mostly raw build output that the root-cause matcher
did not recognise, not a distinct failure mode, so the share above is
a lower bound.
```

## 四、结论

- **8B maintainability 提升成立**：53 → 92 票，p=0.0050；
- **14B maintainability 提升成立**：95 → 151 票，p=0.0006；
- **8B correctness 完全不动**：44 → 44，p=1.0000；
- **14B correctness 没有改善**：62 → 53，p=0.3620（方向反而下降，但不显著）；
- 两条 maintainability 结论均明显超过重复运行噪声地板（最低 p=0.3961）。

因此 402 题最终结论与阶段板一致：**SFT 明确改善可维护性行为，
没有改善可执行正确性。**
