> **Historical archive — not current package instructions or a current leaderboard.**
> Preserve the protocol, roster, denominator, incomplete/withdrawn flags and audit
> caveats stated below; do not reinterpret these results using today's judge policy.
> See the [archive index](README.md) and [current documentation](../README.md).
> Historical commands and local artifact references may require assets not shipped
> in this repository. Publication changes are limited to this notice, portable
> path placeholders and repaired links; original bytes remain in the backup.

# 臂命名对照表

作业名是按「配置差异」起的,读起来像模型名但不是。这张表把它们翻译成
「哪个基座 + 训过没有 + 哪一版训练」,报告里应当用右边那一列。

## SFT 侧

| 作业名 | 基座 | 训练 | 报告里该写 |
| --- | --- | --- | --- |
| `base-qwen3-8b` / `base-qwen3-14b` | Qwen3-8B / 14B | **无** | **基座(未训练)** |
| `qwen3-*-mtsft` | 同上 | 第一版:按轮切分(一条轨迹拆成 R 个样本,每个只监督一轮) | **第一版训练** |
| `native-qwen3-*` | 同上 | 第二版:原生多轮(整条轨迹一个样本,监督全部轮次)+ 全参 | **第二版训练** |
| `nobanner-native-*` | — | **权重与第二版相同**,只改评测时的服务格式(裸 JSON,32 轮) | 第二版训练,裸 JSON@32 |
| `r16-nobanner-native-*` | — | **权重与第二版相同**,裸 JSON + 16 轮 | 第二版训练,裸 JSON@16 |

- `native` 指「原生多轮」这种**训练数据组织方式**,不是模型名,也不是
  「原版/未微调」的意思——恰恰相反,它是训得最多的那一版。
- `nobanner` / `r16` 这一族**不是新模型**,是同一份权重换服务方式,
  用来分离「模型学到的」和「提示告诉它的」。

## RL 侧

| 作业名 | 含义 |
| --- | --- |
| `rl-arm-sft` | RFT 的起点(SFT 之后、RL 之前) |
| `rl-arm-rft1` / `rft2` | RFT 第一轮 / 第二轮 |
| `b4k-rl-arm-*` | 同三臂,只把输出预算从 8192 降到 4096(P88 对照) |
| `noise-rep1/2/3` | **同一个 adapter、同一套配置,跑三遍**。这是噪声地板,不是三个模型 |
| `rl-arm-*-r1/r2/r3` | 三臂各跑三遍,用来把臂间差异和重采样方差直接对比 |

## 报告规范

1. **任何 delta 必须写清分母是「基座」还是「上一版」**——两者会给出方向相反的
   故事(P91:相对上一版 +180,相对基座 −10)。
2. **同一张表里必须带噪声地板**,否则「0 → 5 → 6」看起来像单调提升,
   而重复臂本身就是「3 / 1 / 1」。
3. 优先用「基座 / 第一版训练 / 第二版训练」这样的说法,作业名放在括号里。
