# SFT 八臂四门（402/402 题已尝试）

生成时间：2026-09-03 23:20

> 3216/3216 rows; infra_retry excluded=179; complete=false

> TS/JS 历史 runner 没有顶层 patch hash，但显式保存
> `patch_unmodified=true`、`model_identity_preserved=true` 和
> `generation_status=generated`；finalize 只接受三项同时成立的旧记录，
> 无 hash 且无 attestation 仍 fail-closed。

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

## 二、两条训练主结论（同次输出噪声地板）

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

## 三、结论

- **8B maintainability**：53 → 92 票，p=0.0050；
- **14B maintainability**：95 → 151 票，p=0.0006；
- **8B correctness**：44 → 44，p=1.0000；
- **14B correctness**：62 → 53，p=0.3620（下降但不显著）；
- 两条 maintainability 结论均明显超过重复运行噪声地板（最低 p=0.3961）。

402 题最终结论：**SFT 改善可维护性行为，没有改善可执行正确性。**
