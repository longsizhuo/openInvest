# DSPy v2 Training Pipeline

2026-05-18 加入。v1 训出来的 demos 因为 6 个结构问题（verdict 塌缩、symbol 丢失、portfolio_state 单一、单 regime、reward 过粗、任务定义错位）从未接入 production。v2 修了这些。

## 6 个修复 vs v1

| v1 | v2 |
|----|----|
| 训练集 66 样本 | 5565 样本 |
| Verdict 只 HOLD + ACCUMULATE | 5 种全有：BUY/ACC/HOLD/TRIM/SELL |
| Symbol = `'?'` 抹掉 | 保留 10 个 symbol（NDQ/GC/QQQ/AAPL/TSLA/BTC/TLT/IWM/EEM/SPY）|
| portfolio_state 全 100% 现金 | 5 种 portfolio_state × 5 种 solvency |
| 2024-05~2024-12 单 regime | 2024-05~2026-05 跨 regime |
| Reward = 7d return ±2% 阈值 | `forward_30d_sharpe - λ·|forward_30d_mdd_pct|/100` |
| Input 2 字段 (mkt_ctx, portfolio_state) | 3 字段 (macro / market / portfolio) |
| Optimizer: BootstrapFewShotWithRandomSearch | **MIPROv2** (Bayesian Opt) |

## 关键文件

| 文件 | 用途 |
|------|------|
| `scripts/build_dspy_trainset_v2.py` | yfinance 多 symbol × 2 年 → oracle labeling → forward-window metrics → trainset_v2.json |
| `core/backtest_reward.py:forward_window_reward()` | per-sample reward 公式 |
| `core/backtest_reward.py:verdict_oracle_accuracy()` | DSPy metric: verdict 与 forward outcome 一致性 |
| `scripts/rl_optimize_prompts_v2.py` | MIPROv2 训练器 |
| `capabilities/dspy_few_shot_loader.py` | 把 v2 optimized demos format 成 markdown，注入 CIO prompt |
| `experiments/dspy_trainset_v2.json` | 5565 样本训练集 |
| `experiments/dspy_optimized_v2.json` | MIPROv2 优化后的 program（含 demos）|

## 重新跑训练

```bash
# 1. 重建 trainset (yfinance 拉数据，~5min)
uv run --with dspy python -m scripts.build_dspy_trainset_v2 \
    --output experiments/dspy_trainset_v2.json

# 2. smoke test (50 train / 20 dev, ~3min)
DEEPSEEK_API_KEY=$(grep '^DEEPSEEK_API_KEY=' .env | cut -d= -f2-) \
DEEPSEEK_BASE_URL=$(grep '^DEEPSEEK_BASE_URL=' .env | cut -d= -f2-) \
uv run --with dspy python -m scripts.rl_optimize_prompts_v2 \
    --trainset experiments/dspy_trainset_v2.json \
    --output experiments/dspy_optimized_v2_smoke.json \
    --smoke-test

# 3. 正式训练 (5565 train / dev, auto=light ~20min, medium ~1h)
DEEPSEEK_API_KEY=$(grep '^DEEPSEEK_API_KEY=' .env | cut -d= -f2-) \
DEEPSEEK_BASE_URL=$(grep '^DEEPSEEK_BASE_URL=' .env | cut -d= -f2-) \
uv run --with dspy python -m scripts.rl_optimize_prompts_v2 \
    --trainset experiments/dspy_trainset_v2.json \
    --output experiments/dspy_optimized_v2.json \
    --auto light
```

## .env 用法注意

不要用 `set -a && source .env`——`EMAIL_PASSWORD` 含空格 bash 解析会炸。
用上面命令行的 `grep | cut` 方式，或者 python `dotenv.load_dotenv()`。

## 接入 production

`capabilities/committee/cio/cio.py:build_cio_prompt` 已经在用 `load_skill("cio")` 读 SKILL.md。
接入 v2 demos 的两步：

1. 改 `capabilities/committee/cio/cio.md`，末尾加 `{{few_shot_examples}}` 占位符
2. 改 `capabilities/committee/cio/cio.py:build_cio_prompt`，调 `load_v2_few_shot_examples()` 拿 demos，作为 `few_shot_examples=...` 传给 `load_skill()`

graceful 退化：v2 artifact 不存在时 `load_v2_few_shot_examples()` 返回 ""，
SKILL.md 渲染时 `{{few_shot_examples}}` 替换成空字符串，CIO 用纯 SKILL.md prompt 跑。

## 已知限制

- Verdict 分布偏 HOLD (66%)，BUY 只 114 样本，stratified split 后 dev BUY 只 ~23 个，
  BUY 维度的 accuracy 信噪比较弱。后续可 oversample 少数类。
- 训练时 `asset_pct` 没启用进 `verdict_oracle_accuracy`（保留接口），未来调阈值用。
- DSPy 没装进 pyproject.toml 依赖，要用 `uv run --with dspy`。
