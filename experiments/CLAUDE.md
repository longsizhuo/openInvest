# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 这是 `experiments/` 子树的指引。**产品/分层架构、发版、公开数据红线**在仓库根
> `../CLAUDE.md`，先读那份。本文件只讲这个目录特有的事。

## 这个目录是什么（先定性）

`experiments/` 是**冻结的、可复现的研究产物 archive**——训练集、调参结果、prompt
历史版本、消融复现包。**不影响生产**：production 代码从不 import 这里的任何东西。
反方向才对——这里的脚本 `from core.* / capabilities.*` 调生产代码来跑实验。

改这里的东西**不会触发后端版本 bump**（除非也碰了根目录代码）；commit 用
`research(scope):` / `chore(experiments):`，别用 `feat:`。

## 最重要的规则：实验数据 3 个角色，别起第四个名

`experiments/`（旧名）和 `research/`（曾用名）已统一。任何实验数据按生命周期分 3 处：

| 角色 | 放哪 | PII |
|---|---|---|
| **进行中的实验**（活代码 + 原始落盘） | `test_*` 分支 + `memory/.{name}_experiment/`（gitignore） | 视内容 |
| **冻结的可复现产物**（数据+脚本+README，进 main） | **`experiments/<name>/`** ← 就是这里 | **必须去 PII** |
| **防灾全量备份**（含 PII 单点） | 私有 repo `openinvest-research-archive`（周更 cron） | 含 PII |

- **结论/决策不是数据** → 进 `../docs/wiki/` + `../docs/wiki/adr/`，不进这里。
- **冻结一个新实验进 main**：建 `experiments/<name>/`，子目录约定
  `data/ inputs/ scripts/ baselines/ README.md SCHEMA.md`，**PII 扫描过再 commit**。
  范本看 `ta-analysts/`（自包含，`import pandas/numpy/openai` 就能跑，不依赖仓库其余部分）。
- archive 规则：跑完一轮训练 → summary 复制来加时间戳；改 prompt → 旧版 archive 到
  `prompt_variants/<role>_<version>_<desc>.py`。**别删历史**——A/B 对比和反思都靠它。

## 关键分工：runner 在仓库根，artifact 在这里

容易踩的坑——**多数训练脚本不在 `experiments/`，在仓库根 `../scripts/`**，从仓库根
以 `-m` 运行。这里放的是它们读/写的 JSON 产物 + 单一可信源 config。

| 想干啥 | 跑什么（从仓库根 `..` 运行） | 读/写到这里的 |
|---|---|---|
| DSPy v2 重建训练集 | `uv run --with dspy python -m scripts.build_dspy_trainset_v2 --output experiments/dspy_trainset_v2.json` | `dspy_trainset_v*.json` |
| DSPy v2 训练（MIPROv2） | `uv run --with dspy python -m scripts.rl_optimize_prompts_v2 --trainset … --output experiments/dspy_optimized_v2.json [--smoke-test\|--auto light]` | `dspy_optimized_v*.json` |
| Optuna 超参搜索 | `python -m scripts.rl_train --workspace /tmp/my_optuna …` | `optuna_final_summary.json` |

- **DSPy 没进 `pyproject.toml`**——必须 `uv run --with dspy`。
- **`.env` 加载坑**：`EMAIL_PASSWORD` 含空格，`set -a && source .env` 会炸。用
  `DEEPSEEK_API_KEY=$(grep '^DEEPSEEK_API_KEY=' .env | cut -d= -f2-)` 或 `dotenv.load_dotenv()`。
- `train_config.py` 是 **Optuna 参数空间的单一可信源**——`scripts.rl_train` 自动读它。
  改 range 直接改这个文件，不用 export env var。`confirmed_effective=False` 的字段是
  实测 placebo（当前 prompt 架构下调了没用），**别删**——换 prompt/资产组合可能 unblock。
- 训练参数**故意不放 `.env.example`**：`.env` 是"必须配否则跑不起来"，训练参数是
  "研究者的旋钮"，代码 default = v0 行为，塞进去只会让 fork 用户困惑。

## 子包 / 文件导航

- `ta-analysts/` — TA 分析师消融的去 PII 复现包（ADR-009）。**自包含**，有自己的
  `scripts/`（`ta_phase_a.py` 跑 LLM、`eval_ta_signal.py` 判预注册 Gate、`ta_data.py`
  数据源）。`data/` 已存全部原始结果——`eval_ta_signal.py` 指向它即可复算 Gate，**无需重跑 LLM**。
  数据字典看 `ta-analysts/SCHEMA.md`，结论叙事看 `../docs/wiki/16-ta-analysts-experiment.md`。
- `README_v2.md` — DSPy v2 pipeline（修了 v1 的 6 个结构问题）+ 接入 production 的两步。
- `prompt_variants/` — CIO prompt 历史版本（v0 baseline / v1 现金机会成本）。
- `audits/` — oracle 回测脚本 + 结果 JSON + 一次性审计（含 `ccy_hardcoding_audit.md`）。
- `sandbox/` — 一次性试跑脚本 + log，非可复现包。
- `archive_few_shot_failed/` — 失败的 few-shot v4 尝试，留作反面教材。

## 复现实验前必读的局限（投稿/引用前）

各复现包 README 末尾的"局限"段是**预注册的诚实声明**，不是免责套话——
ta-analysts 是 `n=2 资产 × 2 窗口`、模型训练截止可能晚于回测期（lookahead 风险）、
基线③是手搓机械映射未对经典因子 benchmark。改结论矩阵或加新窗口时同步更新这些。
