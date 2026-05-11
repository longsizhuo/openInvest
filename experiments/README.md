# experiments/

训练 / 调参 / prompt 实验的 archive + config。**不影响生产**——这里只是研究材料。

## 文件清单

```
train_config.py                              ← 训练参数空间（单一可信源）⭐
optuna_final_summary.json                    ← 2026-05-11 第一轮 30-trial 完整结果
dspy_trainset_v1_2024_05_to_11.json          ← 66 样本，给 DSPy 用
prompt_variants/
├── cio_baseline_v0.py                       ← v0 (100% HOLD 复读机) 原版
└── cio_v1_cash_opportunity_cost.py          ← v1 (现金机会成本规则) 改后
```

## 想自己调实验？

1. **看 `train_config.py`** —— 列出所有 hyperparameter + range + 含义 + 哪些是 placebo
2. 想改 range 直接改这个文件（`rl_train.py` 会自动读）
3. 跑：`python -m scripts.rl_train --workspace /tmp/my_optuna ...`
4. 详细教程见 [docs/wiki/11-rl-training.md](../docs/wiki/11-rl-training.md)

## 为什么不放 `.env`

`.env` 是"用户必须配的运行时设置"（API key / 路径），错填程序跑不起来。
训练参数是"研究者的旋钮"，99% 用户不动，代码 default = v0 行为。
塞 `.env.example` 会让 fork 用户看着一堆数字困惑"这啥意思能不能删"。

## archive 规则

- 跑完一轮训练 → final_summary.json 复制到这里加时间戳
- 改 prompt → 旧版本 archive 到 `prompt_variants/<role>_<version>_<desc>.py`
- 别删历史 archive——A/B 对比 + 反思都靠它们
