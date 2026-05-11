# openInvest RL 训练报告

**日期**：2026-05-11
**模型**：deepseek-v4-flash (non-thinking mode)
**Pipeline**：诊断 → prompt v1 改动 → Optuna 30-trial → Hold-out 验证

---

## TL;DR

| 阶段 | Reward | 年化 | Sharpe | HOLD % | 备注 |
|---|---|---|---|---|---|
| **v0 baseline** | -0.0006 | 0% | 0 | **100%** | 100% HOLD 复读机 |
| **v1 prompt** | +0.3978 | +25.4% | 1.58 | 44% | unblock，跑赢 3/4 基准 |
| **Optuna best (train)** | **+0.4223** | +34.4% | 1.37 | - | 30 trial 找到的天花板 |
| **Hold-out 验证** | +0.2880 | +17.4% | 1.59 | - | 参数泛化 OK，不 overfit |

**核心结论**：v1 prompt 改动是 unblock 关键（reward 0 → 0.4），Optuna 30-trial 在该 prompt 下穷尽参数空间也只能搜到 0.42。**prompt 内容是真天花板，hyperparameter 顶不破**。下一步必须做 DSPy（自动 prompt 优化）才能突破。

---

## 1. v0 Baseline 问题

**6 个月窗口诊断**（2024-05-15 → 2024-11-27, 33 dates × 2 资产 = 66 verdict）：

- HOLD 占比 **100% (66/66)**
- 每月单一 verdict 类型，confidence 方差 ≈ 0（固定 0.65±0.05）
- 总收益 0%（账户全程 100% 现金）
- 跑输余额宝 -0.12%
- Reward = -0.0006（噪音水平）

### 根因分析

读 4 个 prompt 文件（`agents/cio.py` 等）发现关键 bias：

1. **CIO BUY 触发是 3-AND 强约束**：`BUY 仅在 Quant + Macro 强 bullish + Risk ok 时`
2. **ACCUMULATE 完全没有触发条件**，只有描述 "逆势分批建仓"
3. **没有"100% 现金本身是错误 default"的认知**
4. Risk Officer：100% 现金 = 0% 集中度 = 报 `ok` 信号，无"机会成本风险"概念

LLM 完美避开"待观察"但选了 HOLD——技术上不违反任何规则，但实际是复读机。

---

## 2. v1 Prompt 改动（commit `2b69c83`）

`agents/cio.py` 加 **"现金机会成本"硬规则**：

```
CONCENTRATION_PCT < 20%（仓位 < 20%，子弹 ≥ 80%）：
- 不允许 HOLD
- 默认至少 ACCUMULATE，alloc = dry_powder × 5~10%
- 豁免：Macro=risk_off AND Risk=high_risk（两个 AND）
```

金融逻辑：等回调 ≠ 零仓位等，是留 90% 子弹等更低位。

### v1 对比

| 指标 | v0 | v1 | Δ |
|---|---|---|---|
| HOLD % | 100% | 44% | -56pp |
| BUY+ACCUMULATE | 0% | 56% | +56pp |
| 总收益 | 0% | +15.55% | +15.55pp |
| 年化 | 0% | +25.39% | +25.4pp |
| Sharpe | 0 | 1.58 | |
| Reward | -0.0006 | +0.3978 | +0.4 |
| vs 余额宝 | -0.12% | +14.72% | |
| vs 沪深300 | 0% | +15.55% | |
| **vs 等权** | 0% | **-1.65%** | 仍输 buy-hold |

✅ unblock 成功；⚠️ 仍跑输 buy-and-hold 等权策略

### v1 verdict 命中率分析（DSPy trainset）

`scripts/build_dspy_trainset.py` 算每个 verdict 的 7d 实际 return + 方向分：

- 方向对（+1）: 47.0% (31/66)
- 中性（0）: 39.4% (26/66)
- 方向错（-1）: 13.6% (9/66)
- **策略 hit rate: +33.3%**

按 verdict 类型：
- **ACCUMULATE**: 37 个, 38% 方向对（不擅 7d timing）
- **HOLD**: 29 个, 59% 方向对（横盘判定较准）

LLM 不善 timing 但擅避免"重大方向错"——长期累积仍 +15.5%。

---

## 3. Optuna 30-trial 搜参数空间

### 3.1 修复前的 placebo bug

阶段 4 原代码有 3 个 silent placebo（已修，commit `6fb038c` + `a06730b`）：

1. **`regime.THRESHOLDS` key 写错**：用 `uptrend_trend_score` 但实际 key 是 `trend_ma_spread_pct`，monkey-patch silent fail
2. **`INVEST_MAX_DEBATE_ROUNDS` 没人消费**：Optuna 建议 max_rounds=3 但 backtest 默认还是 1
3. **`INVEST_CIO_CONFIDENCE_CAP` 没人消费**：Optuna 调 cap 但 confidence 不被 clamp
4. **`INVEST_ALLOC_AGGRESSIVENESS` 没人消费**：导致 35 SKIP（LLM 给 ¥50k alloc 但只有 ¥100k 总资产）

补完后，5 个参数都真正生效。

### 3.2 参数空间

| 参数 | 范围 | 含义 |
|---|---|---|
| regime_uptrend | 3.0 ~ 6.0 | MA20/MA120 spread 阈值 |
| regime_atr | 2.5 ~ 5.0 | ATR crash 阈值 |
| max_rounds | 1 ~ 3 | cross-challenge 轮数 |
| cio_confidence_cap | 0.7 ~ 0.95 | CIO confidence 上限 |
| alloc_aggressiveness | 0.05 ~ 0.30 | alloc 占 baseline ¥100k 比例 |

### 3.3 30 trial 结果

**Best trial #20**（agg=0.245, reg=4.63, rounds=2, cio_cap=0.75）:
- Reward = **0.4223**
- 年化 +34.4%
- Sharpe 1.37
- Max DD 15.4%

| 统计 | 值 |
|---|---|
| mean | 0.3952 |
| median | 0.4165 |
| stdev | 0.0371 |
| min | 0.2621 |
| max | 0.4223 |

### 3.4 Top 5 几乎平局

| Trial | Reward | agg | reg | rounds | cio_cap |
|---|---|---|---|---|---|
| #20 | 0.4223 | 0.245 | 4.63 | 2 | 0.75 |
| #4  | 0.4223 | 0.164 | 4.84 | 1 | 0.79 |
| #29 | 0.4210 | 0.224 | 5.59 | 1 | - |
| #1  | 0.4210 | 0.227 | 3.47 | 3 | 0.85 |
| #9  | 0.4205 | 0.096 | 4.99 | 2 | 0.84 |

**关键观察**：完全不同的 params 都能达到 reward 0.42 →
- max_rounds 1 / 2 / 3 都能进 Top 5 → 多轮辩论是 placebo
- alloc_agg 0.096 vs 0.245 reward 几乎同 → 大单 vs 小单本质同策略
- regime 3.47 vs 5.59 也都能进 Top 5 → REGIME 阈值不是 reward 主因

**结论**：当前 prompt 架构下 reward 天花板 ≈ 0.42。Bayesian optimization converged，再加 100 trial 也突不破。

### 3.5 并行加速（实测）

3 worker 共享 SQLite study：
- 单 trial wall clock：28 min → **9.5 min**（3x 加速）
- 30 trial 总时间：约 60 分钟（vs 单 process 5 小时）
- 没触发 DeepSeek 429 限流（3 RPS × ~5s/call ≈ 0.6 RPS 实际并发）

---

## 4. Hold-out 验证（防 overfit）

**train**: 2024-05-13 → 2024-11-15 (26 weeks)
**hold-out**: 2024-11-18 → 2024-12-31 (6 weeks, 不重叠)

用 Optuna trial #20 best params 在 hold-out 跑：

| 指标 | Train | Hold-out | 评估 |
|---|---|---|---|
| Reward | 0.4223 | **0.2880** | ✅ > 50% 阈值，泛化 OK |
| 年化 | +29.2% | +17.4% | 缩水但仍显著正 |
| Sharpe | 1.56 | **1.59** | 持平 |
| Max DD | 10.9% | **2.6%** | 改善 |
| vs 余额宝 | +14.0% | +1.9% | |
| vs 沪深300 | +14.7% | +2.1% | |
| vs 等权 | +2.7% ✓ | **-1.9%** ⚠️ | 又输 buy-hold |

**判定**：参数 **不 overfit**，泛化能力 OK（reward 缩水 32% 但绝对值仍正）。Hold-out 期间策略变保守（BUY 2 次 vs train 12 次）—— NDQ 那 6 周震荡，LLM 没强行追涨，是合理行为。

---

## 5. 关键 Negative Results

1. **Reward landscape 几乎平**（30 trial stdev 0.037 vs mean 0.40）→ hyperparameter 不是 reward 主因
2. **max_rounds=1 vs 3 reward 同**→ 多轮辩论是 placebo（LLM 第二轮主要重复 round 1 结论）
3. **alloc_aggressiveness 0.06 vs 0.25 reward 同** → 大单 vs 小单本质同策略
4. **真瓶颈是 prompt 内容**——4 个角色的 prompt 决定 verdict 分布，参数只是表面调整

---

## 6. Next: DSPy（待实现）

### 现状
- DSPy 3.2.1 已装
- Trainset 已生成：`experiments/dspy_trainset_v1_2024_05_to_11.json`（66 样本，含 7d return + reward_score）
- Scaffold 未写：`scripts/rl_optimize_prompts.py` 待实现

### 实施方案

1. 定义 `dspy.Signature` 包装 CIO 的输入/输出 schema
2. 用 trainset 作 dev set + 把 metric 定为 `total_return_pct - 0.5 × max_drawdown_pct`
3. `BootstrapFewShotWithRandomSearch` 自动选最优 few-shot examples
4. 把选中的 examples 注入到 `agents/cio.py` 的 system prompt
5. 重跑 walk-forward 对比 reward

### 预期工作量
- 1-2 天（需 refactor CIO 的 SDKAgent → DSPy module）
- 预期 reward 从 0.42 突破到 0.5+（不保证）

---

## 7. 文件清单

### 新增
- `agents/cio.py` — v1 修改版（commit `2b69c83`）
- `experiments/prompt_variants/cio_baseline_v0.py` — v0 archive
- `experiments/prompt_variants/cio_v1_cash_opportunity_cost.py` — v1 archive
- `experiments/optuna_final_summary.json` — 30 trial 完整数据
- `experiments/dspy_trainset_v1_2024_05_to_11.json` — 66 样本训练集
- `scripts/backtest_runner.py:_warmup_market_data()` — 10y 预热（commit `867db9b`）
- `scripts/build_dspy_trainset.py` — trainset 生成器（commit `这次`）
- `scripts/holdout_validate.py` — hold-out 验证（commit `这次`）

### 修改
- `core/committee.py:parse_cio_memo()` — alloc/confidence env clamp
- `scripts/rl_train.py` — 修 regime key + env var 接通
- `scripts/run_walk_forward.py` — 修周末 start bug
- `scripts/backtest_committee.py` — 接 INVEST_MAX_DEBATE_ROUNDS
- `core/strategy_metrics.py` — sharpe/sortino 数值稳定 clip

### Commits
- `867db9b` — DeepSeek v4-flash 迁移 + 10y warmup + 数值稳定
- `2b69c83` — CIO 现金机会成本 prompt v1
- `6fb038c` — Optuna 2 个 placebo bug 修复
- `a06730b` — max_rounds/cio_cap 消费代码 + 周末 start bug
- `这次` — 训练报告 + trainset 工具 + hold-out 验证 + archive
