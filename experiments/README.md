# experiments/

训练 / 调参 / prompt 实验的 archive + config。**不影响生产**——这里只是研究材料。

## 实验数据放哪：3 个角色，1 个名字（2026-06-12 收编）

之前 `experiments/`（旧）和 `research/`（新 TA 包）是同一个东西的两个名，已统一。
任何实验数据按生命周期分 3 处，**别再起第四个名**：

| 角色 | 放哪 | PII | 例 |
|---|---|---|---|
| **进行中的实验**（活代码 + 原始落盘） | `test_ta` 式分支 + `memory/.{name}_experiment/`（gitignore） | 视内容 | TA 实验跑在 `test_ta` + `memory/.ta_experiment/` |
| **冻结的可复现产物**（干净包：数据+脚本+README，进 main） | **`experiments/<name>/`** ← 就是这里 | **必须去 PII** | `experiments/ta-analysts/`、本目录的 DSPy/Optuna 产物 |
| **防灾全量备份**（含 PII 的单点备份） | 私有 repo `openinvest-research-archive`（周更 cron） | 含 PII | 委员会 transcript、`.dreams/` 等 |

> 结论/决策不是数据 → 进 `docs/wiki/` + `docs/wiki/adr/`，不进这里。

**冻结一个实验进 main 时**：建 `experiments/<name>/`，子目录约定
`data/ inputs/ scripts/ baselines/ README.md SCHEMA.md`，PII 扫描过再 commit。
现有子包：`ta-analysts/`（TA 分析师消融，ADR-009）。

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
