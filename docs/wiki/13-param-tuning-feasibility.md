---
type: wiki-chapter
title: openInvest 参数调优可行性诊断
tags: [param-tuning, regime, committee, backtest, calibration]
intent: 决策参数
schema_source:
  - core/regime.py:THRESHOLDS
  - core/regime.py:ASSET_OVERRIDES
  - core/committee/cio_parse.py:THRESHOLDS
  - core/backtest_reward.py:compute_strategy_reward
  - core/backtest_reward.py:verdict_oracle_accuracy
documents:
  endpoints: []
  config_keys: []
  symbols:
    - THRESHOLDS
    - ASSET_OVERRIDES
    - compute_strategy_reward
    - verdict_oracle_accuracy
---

# openInvest 参数调优可行性诊断

> 纯只读评估，不改代码。2026-05-27。
> **补充文档**（耦合分析 / Do-Not-Tune / Optuna 回顾 / Type B 细分）：[13-param-tuning-feasibility-addendum.md](13-param-tuning-feasibility-addendum.md)

---

## 1. 参数清单

### 1.1 Regime 分类阈值 (`core/regime.py`)

| 参数 | 文件:行号 | 当前值 | 依据 | 影响范围 |
|------|-----------|--------|------|----------|
| `trend_ma_spread_pct` | `core/regime.py:37` | 3.0% (默认) | MA20 vs MA120 偏离 3% 有趋势。无 commit/ADR 记录，属经验初始值 | Quant prompt 策略提示、Dreaming regime 分桶、verdict_review regime 标记 |
| `crash_atr_pct_min` | `core/regime.py:39` | 5.0% (默认) | 14 日 ATR ≥5% 视为异常高波动。无明确依据 | Crash 免责判定、Dreaming 免责、verdict_review macro_shock |
| `crash_drawdown_30d_pct` | `core/regime.py:42` | 20.0% | 30 日跌 ≥20% + 高波动 = 急跌 crash。无 ADR | 同上 |
| `crash_deep_drawdown_30d_pct` | `core/regime.py:46` | 30.0% | 30 日跌 ≥30% 单独触发 crash（不看波动）。2026-05-26 新增，有 commit 记录 | 同上 |
| `recovery_rebound_pct` | `core/regime.py:48` | 10.0% | 从 30 日低点反弹 ≥10% + 仍在低位 = recovery。无 ADR | Quant prompt "复苏"策略提示 |
| `recovery_quantile_max` | `core/regime.py:50` | 0.50 | 价格 2y 分位 <50% 才算"仍在低位"。无依据 | 同上 |
| `low_quantile_threshold` | `core/regime.py:52` | 0.20 | 2y 分位 ≤20% = "低位"。无依据 | **已停用**（2026-06 拆方向锁后 strategy hint 不再读；键位保留兼容） |
| `high_quantile_threshold` | `core/regime.py:54` | 0.80 | 2y 分位 ≥80% = "高位"。无依据 | **已停用**（同上） |

**Per-asset overrides** (`core/regime.py:69-85`):

| Asset | 覆盖参数 | 值 | 依据 |
|-------|---------|-----|------|
| GC=F | trend_ma_spread_pct | 5.0% | "黄金趋势更连贯，3% 太敏感" — commit 注释，无量化验证 |
| GC=F | crash_atr_pct_min | 3.5% | "黄金长期 ATR ~1%，5% 永远不触发" — 经验判断 |
| NDQ.AX | trend_ma_spread_pct | 4.0% | "ETF 比单股更平滑" — 经验判断 |
| BTC-USD | trend_ma_spread_pct | 8.0% | 无依据 |
| BTC-USD | crash_atr_pct_min | 8.0% | "加密 ATR 3-5%，8% 防误报" — 经验判断 |
| ETH-USD | trend_ma_spread_pct | 8.0% | 无依据 |
| ETH-USD | crash_atr_pct_min | 8.0% | 无依据 |

### 1.2 Committee Sanity Check 阈值 (`core/committee/cio_parse.py`)

| 参数 | 文件:行号 | 当前值 | 依据 | 影响范围 |
|------|-----------|--------|------|----------|
| `buy_confidence_overdrive` | `core/committee/cio_parse.py:84` | 0.95 | "60 天样本信号不显著，>0.95 历史命中率反而低于 0.7-0.8" — commit 注释，有数据支撑 | BUY→ACCUMULATE 降级触发线 |
| `buy_confidence_downgrade_to` | `core/committee/cio_parse.py:85` | 0.6 | "表达原 LLM 过度自信" — 设计意图，无量化 | 降级后的 confidence 值 |
| `alloc_cny_ceiling` | `core/committee/cio_parse.py:86` | 100,000 | "月投 ~¥10k，单笔超 10x 几乎一定是 LLM 错乱" — 经验 | alloc clamp 上限 |
| `worker_unavailable_confidence_floor` | `core/committee/cio_parse.py:87` | 0.4 | "worker 不全 → 证据不足" — 设计意图 | worker 失败时 confidence 封顶 |

### 1.3 LLM 调用参数 (`core/committee/agent_io.py`，`max_debate_rounds` 在 `debate.py`)

| 参数 | 文件:行号 | 当前值 | 依据 | 影响范围 |
|------|-----------|--------|------|----------|
| `LLM_MAX_ATTEMPTS` | `core/committee/agent_io.py:23` | 3 (env) | "3 次在 ~14s 内完成" — 工程经验值 | 重试次数，影响延迟和成本 |
| `LLM_BASE_DELAY` | `core/committee/agent_io.py:24` | 2.0s (env) | 无依据 | 重试间隔指数退避基数 |
| `LLM_MAX_DELAY` | `core/committee/agent_io.py:25` | 20.0s (env) | 无依据 | 重试最大等待 |
| `temperature` | `core/committee/agent_io.py:35` | 0.2 | 无依据。CIO 用 0.1 | LLM 输出随机性 |
| `max_tool_iterations` | `core/committee/agent_io.py:75` | 4 | 无依据 | Agent tool call 轮次上限 |
| `max_debate_rounds` | `core/committee/debate.py:153` | 1 (默认) / 4 (live) | 工程经验值 | Cross-challenge 轮数，影响 LLM 调用量 |

### 1.4 Dreaming 系统参数 (`jobs/dreaming.py`)

| 参数 | 文件:行号 | 当前值 | 依据 | 影响范围 |
|------|-----------|--------|------|----------|
| `LOOKBACK_DAYS` | `jobs/dreaming.py:64` | 90 (env) | "只学近期行为" — 设计意图 | Light Sleep 摄入窗口 |
| `WINDOWS` | `jobs/dreaming.py:65` | [7, 30] | "不看 1d（1d 几乎不动是 horizon 假象）" — commit 注释 | 回看窗口天数 |
| `MIN_RECALL` | `jobs/dreaming.py:66` | 3 | 无依据。LLM 验伪 prompt 里写 "count≥6" | 最小样本量门槛 |
| `MIN_SCORE` | `jobs/dreaming.py:67` | 0.8 | "OpenClaw 同款" — 来源明确但无自身验证 | Deep Sleep 入选阈值 |
| `CAUTION_MIN_BASE_DOWN` | `jobs/dreaming.py:72` | 0.15 | 2026-05-27 ADR 008，有 commit 和设计论证 | Caution 模式下行基率门槛 |
| `CAUTION_LIFT_FULL` | `jobs/dreaming.py:73` | 0.20 | 2026-05-27 ADR 008，有 commit 和设计论证 | Caution lift 满分阈值 |
| VIX 分桶 (low/mid/high) | `jobs/dreaming.py:227-231` | <18 / 18-25 / ≥25 | 无依据。标准经验值 | REM Sleep regime 标签 |
| TNX 分桶 (low/mid/high) | `jobs/dreaming.py:234-238` | <4.0 / 4.0-4.5 / ≥4.5 | 无依据。标准经验值 | REM Sleep regime 标签 |

### 1.5 Verdict Review 评分参数 (`jobs/verdict_review.py`)

| 参数 | 文件:行号 | 当前值 | 依据 | 影响范围 |
|------|-----------|--------|------|----------|
| `K_FLAT` | `jobs/verdict_review.py:244` | 1.0 | "不让系统自学习评分尺子" — 有明确防 reward hacking 设计 | HOLD "没动" 判定乘数 |
| `FLAT_CEILING_PCT` | `jobs/verdict_review.py:245` | 8.0% | "防 atr 异常时尺子失控" — 设计意图 | HOLD 判定封顶 |
| `DEFAULT_DAILY_VOL_PCT` | `jobs/verdict_review.py:246` | 2.0% | "atr_pct 拉取失败时兜底" — 工程容错 | ATR 拉取失败回退值 |

### 1.6 Backtest Reward 函数权重 (`core/backtest_reward.py`)

| 参数 | 文件:行号 | 当前值 | 依据 | 影响范围 |
|------|-----------|--------|------|----------|
| max_drawdown 权重 | `core/backtest_reward.py:56` | -0.5 | 无依据。公式: `reward = annualized - 0.5*max_dd + 0.5*alpha + 0.2*max(0,sharpe-1)` | Optuna 优化目标 |
| alpha_vs_yuebao 权重 | `core/backtest_reward.py:57` | 0.5 | 无依据 | 同上 |
| sharpe_bonus 权重 | `core/backtest_reward.py:58` | 0.2 | 无依据 | 同上 |
| sharpe_bonus_threshold | `core/backtest_reward.py:58` | 1.0 | 无依据 | 同上 |
| `lam_mdd` (v2 forward) | `core/backtest_reward.py:91` | 1.0 | 无依据 | per-sample reward MDD 惩罚 |
| `lam_return` (v2 forward) | `core/backtest_reward.py:92` | 0.05 | "小权重防双计 Sharpe" — 设计意图 | per-sample reward return 加成 |

### 1.7 Verdict Oracle Accuracy 阈值 (`core/backtest_reward.py:123-180`)

| 参数 | 文件:行号 | 当前值 | 依据 | 影响范围 |
|------|-----------|--------|------|----------|
| BUY: +5% / -3% | `backtest_reward.py:150-154` | 5.0 / -3.0 | 无依据 | DSPy 训练 oracle metric |
| ACCUMULATE: +3% / -3% | `backtest_reward.py:156-160` | 3.0 / -3.0 | 无依据 | 同上 |
| HOLD: ±3% / ±8% | `backtest_reward.py:162-166` | 3.0 / 8.0 | 无依据 | 同上 |
| TRIM: -3% / +5% | `backtest_reward.py:168-172` | -3.0 / 5.0 | 无依据 | 同上 |
| SELL: -5% / +3% | `backtest_reward.py:174-178` | -5.0 / 3.0 | 无依据 | 同上 |

### 1.8 Market Metrics 参数 (`utils/market_metrics.py`)

| 参数 | 文件:行号 | 当前值 | 依据 | 影响范围 |
|------|-----------|--------|------|----------|
| `LOOKBACK_30D` | `market_metrics.py:28` | 30 | 交易日标准 | return_30d、rebound_off_30d_low 计算 |
| `RVOL_WINDOW` | `market_metrics.py:30` | 20 | 无依据 | 相对成交量均量窗口 |
| RSI period | `market_metrics.py:43` | 14 | Wilder 1978 原版标准 | RSI 计算 |
| ATR period | `market_metrics.py:75` | 14 | 标准 | ATR% 计算、regime crash 判定 |
| MA20 / MA120 / MA250 | `market_metrics.py:250-252` | 20 / 120 / 250 | 标准技术分析周期 | Regime 趋势判定、quantile 计算 |

### 1.9 Strategy Metrics 参数 (`core/strategy_metrics.py`)

| 参数 | 文件:行号 | 当前值 | 依据 | 影响范围 |
|------|-----------|--------|------|----------|
| `risk_free_annual` | `strategy_metrics.py:72` | 0.013 (1.3%) | "余额宝当前" — 时效性强 | Sharpe / Sortino 计算 |
| `trading_days_per_year` | `strategy_metrics.py:73` | 252 | 标准 | 年化收益 / 波动率 |

### 1.10 交易摩擦参数 (`utils/exchange_fee.py`)

| 参数 | 文件:行号 | 当前值 | 依据 | 影响范围 |
|------|-----------|--------|------|----------|
| `cn_cable_fee` | `exchange_fee.py:66` | ¥150 | 银行电汇手续费。实测值 | 换汇摩擦计算 |
| `cn_commission_rate` | `exchange_fee.py:67` | 0.1% | 银行佣金费率。实测值 | 同上 |
| `cn_commission_min` | `exchange_fee.py:68` | ¥50 | 银行最低佣金。实测值 | 同上 |
| `cn_commission_max` | `exchange_fee.py:69` | ¥260 | 银行最高佣金。实测值 | 同上 |
| `au_inward_fee` | `exchange_fee.py:70` | ¥15 | 澳洲入账费。实测值 | 同上 |
| CommSec tier 阶梯 | `exchange_fee.py:71-74` | $5/$10/$19.95/0.12% | CommSec 官网费率表 | 交易佣金 |
| `GOLD_OZ_PER_GRAM` | `utils/gold_price.py:23` | 31.1035 | 物理常数 | 金价单位换算 |

### 1.11 其他散落参数

| 参数 | 文件:行号 | 当前值 | 依据 | 影响范围 |
|------|-----------|--------|------|----------|
| `max_single_invest_cny` 默认 | `core/portfolio_manager.py:188` | ¥10,000 | 无依据 | 单笔投资上限回退值 |
| `max_workers` (committee) | `core/runner/session.py` | 4 | "防 LLM API 限流" — 工程经验值 | 并发度 |
| `step_days` (walk-forward) | `experiments/train_config.py:121` | 7 | 无依据 | Optuna 训练决策频率 |
| Dreaming LLM 验伪 temperature | `jobs/dreaming.py:440` | 0.1 | "验伪要稳定" — 设计意图 | LLM 验伪随机性 |
| pnl_snapshot 渲染常数 | `jobs/pnl_snapshot.py:56-66` | W=800, LINE_H=240 等 | 视觉经验 | 图表渲染（非决策） |

---

## 2. 参数分类

### 类型 A：确定性，可干净校准（纯算术，fit 不需要 LLM）

**共 28 个**。这些参数在纯数学函数里，可以用历史行情数据直接计算最优值。

| # | 参数 | 所在模块 | 校准方法 |
|---|------|----------|----------|
| 1 | `trend_ma_spread_pct` (默认 + 4 per-asset) | regime | Grid search on historical regime accuracy |
| 2 | `crash_atr_pct_min` (默认 + 3 per-asset) | regime | Grid search on crash detection precision/recall |
| 3 | `crash_drawdown_30d_pct` | regime | Historical drawdown distribution analysis |
| 4 | `crash_deep_drawdown_30d_pct` | regime | 同上 |
| 5 | `recovery_rebound_pct` | regime | Historical rebound distribution |
| 6 | `recovery_quantile_max` | regime | Quantile 分布分析 |
| 7 | `low_quantile_threshold` / `high_quantile_threshold` | regime | 2y 价格分位分布 |
| 8 | `K_FLAT` | verdict_review | Grid search on HOLD accuracy vs flat band width |
| 9 | `FLAT_CEILING_PCT` | verdict_review | ATR 分布 tail analysis |
| 10 | `DEFAULT_DAILY_VOL_PCT` | verdict_review | ATR 分布 fallback 位置 |
| 11 | `VIX 分桶 18/25` | dreaming | VIX 历史分布分位数 |
| 12 | `TNX 分桶 4.0/4.5` | dreaming | TNX 历史分布分位数 |
| 13 | `LOOKBACK_DAYS` | dreaming | 信号衰减曲线 |
| 14 | `WINDOWS [7, 30]` | dreaming | Forward return 收敛窗口 |
| 15 | `MIN_RECALL` | dreaming | 样本量 vs 稳定性 tradeoff |
| 16 | Reward 函数 4 权重 | backtest_reward | Optuna 已跑 30 trial |
| 17 | Oracle accuracy 10 阈值 | backtest_reward | Historical return distribution per verdict |
| 18 | `risk_free_annual` | strategy_metrics | 余额宝实时利率 |

### 类型 B：LLM-依赖，污染受限

**共 9 个**。这些参数的"最优值"取决于 LLM 输出特性（confidence 分布、verdict 分布），需要 LLM 参与才能评估。

| # | 参数 | 所在模块 | 污染限制 |
|---|------|----------|----------|
| 1 | `buy_confidence_overdrive` (0.95) | committee | 需要 LLM 产出 confidence 分布 + 事后命中率。但可从 verdict_review.jsonl 历史数据反推 |
| 2 | `buy_confidence_downgrade_to` (0.6) | committee | 同上 |
| 3 | `alloc_cny_ceiling` (100k) | committee | 需要 LLM alloc 输出分布。可从历史 transcript 统计 |
| 4 | `worker_unavailable_confidence_floor` (0.4) | committee | 需要模拟 worker 失败场景 |
| 5 | `MIN_SCORE` (0.8) | dreaming | 需要验证 LLM 验伪前后 insight 质量 |
| 6 | `temperature` (0.2) | committee | 需要 LLM 多次采样对比一致性 |
| 7 | `max_tool_iterations` (4) | committee | 需要观察 LLM tool call 行为 |
| 8 | `max_debate_rounds` (1/4) | committee | 需要 LLM 辩论输出质量评估 |
| 9 | Dreaming score 权重 (0.7/0.3) | dreaming | score 公式里的 hit_rate 和 sample 权重 |

---

## 3. 解耦难度

### 3.1 当前 sweep workflow 基础设施

**已有的**：
- `scripts/backtest_committee.py` — 逐日跑历史 committee，blind 未来 ✅
- `scripts/backtest_runner.py` — workspace 隔离，不污染真实 memory/ ✅
- `scripts/run_walk_forward.py` — walk-forward + paper trading simulator ✅
- `experiments/train_config.py` — Optuna 参数空间定义 ✅
- `scripts/rl_train.py` — Optuna 训练入口 ✅
- `scripts/holdout_validate.py` — hold-out 验证 ✅
- `core/backtest_reward.py` — reward function ✅
- `core/strategy_metrics.py` — 完整策略指标 ✅
- `jobs/verdict_review.py` — verdict 事后复盘 ✅

**缺失的**：
- **Regime 阈值没有外部注入口** — `THRESHOLDS` 和 `ASSET_OVERRIDES` 是模块级 dict 字面量，backtest 只能 monkey-patch，不能通过 env/CLI 传入
- **Dreaming 参数没有外部注入口** — `MIN_SCORE`、`MIN_RECALL`、`WINDOWS`、`LOOKBACK_DAYS` 只有 `LOOKBACK_DAYS` 有 env override
- **Reward 函数权重没有外部注入口** — `compute_strategy_reward` 里 4 个权重是硬编码的
- **verdict_review K_FLAT/FLAT_CEILING_PCT 没有外部注入口** — 硬编码常量
- **backtest_committee 的 run_committee 调用没有参数化** — `max_debate_rounds` 通过 env `INVEST_MAX_DEBATE_ROUNDS` 传入，但其他参数不行

### 3.2 粗略 effort 估计

| 改动 | 工作量 | 说明 |
|------|--------|------|
| Regime THRESHOLDS 参数化 | **2-3 小时** | 加 env override 或 CLI arg → `_per_asset_thresholds()` 读外部值 → backtest 自动生效。改动集中在 `core/regime.py` 一个文件 |
| Dreaming 参数参数化 | **1-2 小时** | `MIN_SCORE`/`MIN_RECALL`/`WINDOWS` 加 env override，跟 `LOOKBACK_DAYS` 同模式 |
| Reward 函数权重参数化 | **1 小时** | `compute_strategy_reward` 加 kwargs 或读 `TrainConfig.reward` |
| verdict_review 参数参数化 | **1 小时** | `K_FLAT`/`FLAT_CEILING_PCT` 加 env override |
| Sweep runner (批量 grid search) | **4-6 小时** | 基于现有 `rl_train.py` + `backtest_runner.py` 扩展，支持对 Type A 参数做 grid/random search + 结果聚合 |
| 完整 pipeline (参数 sweep → backtest → metrics → 比较) | **1-2 天** | 串联 sweep runner + walk-forward + strategy_metrics + 报告生成 |
| Type B 参数的 offline 评估 pipeline | **2-3 天** | 需要从 verdict_review.jsonl 提取 LLM 输出分布 + 事后命中率，构建 "准 A/B 测试" 框架 |

**总计**：让 sweep workflow 可用约 **1-2 天**；让 Type A + Type B 都可调约 **3-5 天**。

---

## 4. 数据可用性

### 4.1 Type A 参数可用数据

| 参数 | post-cutoff | 全量 2018-2023 | 都行 | 说明 |
|------|-------------|----------------|------|------|
| `trend_ma_spread_pct` | ✅ | ✅ | ✅ | 纯行情算术。post-cutoff 有 NDQ.AX + GC=F ~90 天；全量有 2y DB 数据 |
| `crash_atr_pct_min` | ✅ | ✅ | ✅ | ATR 是纯历史计算。但 crash 事件稀少（2020 COVID、2022 加息），全量更佳 |
| `crash_drawdown_30d_pct` | ⚠️ | ✅ | ⚠️ | post-cutoff 可能无 30 日跌 20% 样本。全量有 2020.03 |
| `crash_deep_drawdown_30d_pct` | ❌ | ✅ | ❌ | post-cutoff 无 30% 跌幅。需全量 |
| `recovery_rebound_pct` | ⚠️ | ✅ | ⚠️ | post-cutoff 有 2026.04 反弹样本，但少 |
| `recovery_quantile_max` | ✅ | ✅ | ✅ | 分位计算只需足够长历史 |
| `low/high_quantile_threshold` | ✅ | ✅ | ✅ | 同上 |
| `K_FLAT` | ✅ | ✅ | ✅ | HOLD 命中率随 flat band 宽度变化，可用任何有 verdict 的窗口 |
| `FLAT_CEILING_PCT` | ✅ | ✅ | ✅ | ATR tail analysis |
| `VIX/TNX 分桶` | ✅ | ✅ | ✅ | macro 数据充足 |
| `LOOKBACK_DAYS` | ✅ | ❌ | ⚠️ | post-cutoff 有 90 天 verdict；全量 2018-2023 无 verdict_review 数据 |
| `WINDOWS [7,30]` | ✅ | ❌ | ⚠️ | 同上。但全量可以用 backtest 生成 verdict 后再评估 |
| `MIN_RECALL` | ✅ | ❌ | ⚠️ | 同上 |
| Reward 权重 | ✅ | ✅ | ✅ | 纯算术，只要有 P&L 曲线 |
| Oracle accuracy 阈值 | ✅ | ✅ | ✅ | 纯算术，只要有 return 分布 |
| `risk_free_annual` | ✅ | ✅ | ✅ | 余额宝利率变化缓慢 |

### 4.2 Type B 参数可用数据

| 参数 | 污染限制 | 绕开方法 |
|------|----------|----------|
| `buy_confidence_overdrive` | 需要 LLM 的 confidence 输出分布 | **可从 verdict_review.jsonl 历史数据反推** — jsonl 已记录每条 verdict 的 confidence + 事后命中。做 confidence 分桶 → 命中率曲线 → 找拐点。不需重跑 LLM |
| `buy_confidence_downgrade_to` | 同上 | 同上。找"BUY confidence 在哪个值以上命中率下降"的拐点 |
| `alloc_cny_ceiling` | 需要 LLM 的 alloc 输出分布 | **可从 backtest transcript 统计** — `.backtest/` 目录下有历史 CIO memo，regex 提取 alloc_cny 分布 |
| `worker_unavailable_confidence_floor` | 需要模拟 worker 失败 | **可从 verdict_review.jsonl 里筛含 [WORKER_UNAVAILABLE] 的记录** — 数量少但存在 |
| `MIN_SCORE` | 需要评估 insight 质量 | **可做 retrospective 分析** — 看 score 在 0.7-0.9 区间的 insight 事后命中率差异 |
| `temperature` | 需要多次采样 | **污染最重** — 无法用历史数据校准，需要 A/B 测试 |
| `max_tool_iterations` | 需要观察 tool call 行为 | **可从 llm_telemetry.jsonl 统计** — 已记录每次 tool call 的 iteration |
| `max_debate_rounds` | 需要比较多轮辩论质量 | **可从 backtest 对比** — 有 `INVEST_MAX_DEBATE_ROUNDS` env，跑 1 vs 3 vs 4 轮 backtest 比较 |
| Dreaming score 权重 | 需要 insight 质量评估 | **可做 retrospective** — 看不同权重下 insight 的事后验证率 |

---

## 5. 优先级建议

基于 **"诚实可校准 + 影响显著 + 解耦成本低"** 三维评分：

### 优先级 1：`trend_ma_spread_pct`（默认 + per-asset）

- **为什么值得调**：这是 regime 分类的**最上游参数**，直接影响 uptrend/downtrend/range_bound 三路分流。Quant prompt 策略提示、Dreaming regime 分桶、verdict_review regime 标记全部依赖它。当前默认 3% + per-asset 覆盖（GC=F 5%, NDQ.AX 4%）全靠经验，无量化验证。
- **用什么数据**：DB 全历史（2018-2023 + post-cutoff），对每个资产算 MA20/MA120 spread 的历史分布。
- **用什么验证方法**：Grid search [2%, 8%] step 0.5% → 对每个阈值计算 regime 分类的"事后 30d return 按 regime 分桶的分离度"（uptrend 后平均涨多少 vs downtrend 后平均跌多少）。分离度最大的阈值 = 最优。
- **预期产出**：每个资产的最优 MA spread 阈值 + 分离度提升量化。

### 优先级 2：`K_FLAT`（HOLD "没动" 判定乘数）

- **为什么值得调**：HOLD 是委员会最常见的 verdict（>70%），K_FLAT 直接决定"HOLD 命中"的判定标准。太松（K_FLAT 大）→ HOLD 命中率虚高 → Dreaming 把坏 HOLD 当好模式学；太紧 → HOLD 全部判"踏空" → 过度保守。当前 K_FLAT=1.0 无验证。
- **用什么数据**：verdict_review.jsonl（已有每条 verdict 的事后 return + ATR），全量可用。
- **用什么验证方法**：Grid search [0.5, 2.0] step 0.25 → 对每个 K_FLAT 重算 HOLD 命中率 → 画"HOLD 命中率 vs K_FLAT"曲线 → 找拐点（曲线斜率突变处）。
- **预期产出**：最优 K_FLAT 值 + "HOLD 判定标准太松/太紧"的量化诊断。

### 优先级 3：`crash_atr_pct_min`（默认 + per-asset）

- **为什么值得调**：Crash 判定是整个系统的**免责机制** — crash 期间的 verdict 不进 Dreaming 学习、不计命中率。如果 crash 阈值太松（经常误判 crash），大量样本被免责剔除，Dreaming 学不到东西；太紧，真 crash 时系统还在当正常行情学。当前默认 5% 对黄金 3.5% 的覆盖是经验判断。
- **用什么数据**：DB 全历史 ATR 分布 + 历史 crash 事件标注（2020.03 COVID、2022 加息、2026.04 关税）。
- **用什么验证方法**：对每个资产画 ATR 历史分布 → 标注已知 crash 事件 → 找"crash 事件的 ATR 最小值"作为下界 → 选"不漏报任何已知 crash 的最小阈值"。
- **预期产出**：每个资产的 crash ATR 阈值 + 历史 crash 事件召回率。

### 优先级 4：`buy_confidence_overdrive`（BUY→ACCUMULATE 降级线）

- **为什么值得调**：这是防止 LLM 过度自信的**唯一防线**。当前 0.95 意味着"只有 confidence ≥ 0.95 的 BUY 才降级"——但 commit 注释说 ">0.95 的 BUY 历史命中率反而低于 0.7-0.8"，这暗示阈值可能应该更低。
- **用什么数据**：verdict_review.jsonl（已记录每条 verdict 的 confidence + 事后命中）。
- **用什么验证方法**：Confidence 分桶 [0.5, 0.6, ..., 0.95, 1.0] → 每桶 BUY 命中率 → 画"命中率 vs confidence"曲线 → 找"命中率开始下降"的拐点。拐点 = 最优阈值。
- **预期产出**：BUY confidence 的最优降级阈值 + "LLM 过度自信"的量化画像。

### 优先级 5：Oracle accuracy 阈值（BUY +5%/-3% 等 10 个值）

- **为什么值得调**：这组阈值定义了"什么算 verdict 方向正确"，是 DSPy 训练的 label 标准。如果 BUY 的 +5% 门槛太高（实际市场 30 天涨 5% 不常见），大量正确 BUY 被标为"中性"→ 训练信号被稀释。
- **用什么数据**：verdict_review.jsonl 的 forward return 分布（7d/30d）。
- **用什么验证方法**：对每个 verdict 画 forward return 分布 → 用分位数重新定义阈值（如 BUY 的 +1 阈值 = return 分布的 60th 分位，-1 阈值 = 20th 分位）。
- **预期产出**：基于实际 return 分布的 oracle 阈值 + 训练信号密度提升量化。

---

## 附录：不建议优先调的参数

| 参数 | 原因 |
|------|------|
| `temperature` | 污染最重，需要 A/B 测试，无法 retrospective |
| `max_debate_rounds` | Optuna 30 trial 已确认是 placebo（confirmed_effective=False） |
| `cio_confidence_cap` | Optuna 30 trial 已确认是 placebo |
| `regime_uptrend` / `regime_atr` | Optuna 30 trial 已确认是 placebo（弱） |
| 交易摩擦参数 | 实测值，银行/券商费率表决定，非校准对象 |
| `risk_free_annual` | 余额宝实时利率，直接查即可 |
| 渲染常数 (pnl_snapshot) | 非决策参数 |

---

## 附录：关键文件索引

| 文件 | 内容 |
|------|------|
| `core/regime.py` | Regime 分类 Single Source of Truth（THRESHOLDS + ASSET_OVERRIDES） |
| `core/committee/cio_parse.py:84-87` | Committee sanity check 阈值（THRESHOLDS build）|
| `core/backtest_reward.py` | Reward 函数 + Oracle accuracy |
| `core/strategy_metrics.py` | 策略指标计算 |
| `jobs/dreaming.py:60-73` | Dreaming 系统参数 |
| `jobs/verdict_review.py:244-246` | HOLD 判定参数 |
| `utils/market_metrics.py` | 技术指标计算参数 |
| `utils/exchange_fee.py` | 交易摩擦参数 |
| `experiments/train_config.py` | Optuna 参数空间（5 个 hyperparameter） |
| `scripts/backtest_committee.py` | 历史回测入口 |
| `scripts/rl_train.py` | Optuna 训练入口 |
