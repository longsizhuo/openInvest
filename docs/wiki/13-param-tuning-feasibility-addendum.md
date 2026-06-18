---
type: wiki-chapter
title: 参数调优可行性诊断 — 补充文档
tags: [param-tuning, optuna, regime, backtesting]
intent: 参数耦合分析与调优可行性
documents:
  endpoints: []
  config_keys:
    - regime.crash_atr_pct_min
    - regime.crash_drawdown_30d_pct
    - regime.trend_ma_spread_pct
    - regime.recovery_rebound_pct
    - regime.recovery_quantile_max
    - verdict.buy_confidence_overdrive
  symbols:
    - compute_strategy_reward
---

# 参数调优可行性诊断 — 补充文档

> 纯只读，不改代码。2026-05-27。

---

## 1. 参数耦合分析：28 个 Type A 参数 → 9 个独立调参单元

### 簇定义规则

"耦合"指：改其中一个参数的最优值，依赖其他参数的取值。如果两个参数在同一语义概念里共同定义一个行为边界，它们属于同一簇。

### 簇清单

#### 簇 1：Crash 触发器（3 参数，紧密耦合）

| 参数 | 值 |
|------|-----|
| `crash_atr_pct_min` | 5.0% (默认) + per-asset |
| `crash_drawdown_30d_pct` | 20.0% |
| `crash_deep_drawdown_30d_pct` | 30.0% |

**耦合逻辑**：三个参数定义 crash 的两条触发路径（急跌 = ATR AND 跌幅，深跌 = 跌幅 alone）。调 `crash_atr_pct_min` 会改变急跌路径的触发率，间接影响深跌路径的"独占样本量"。三个必须联合 sweep。

**不可拆的原因**：如果单独调 `crash_atr_pct_min` 固定另外两个，急跌路径可能过度触发（ATR 低阈值 + 低跌幅门槛 = 大量假 crash），把本该学的样本免责剔除。

#### 簇 2：Trend 趋势判定（1 参数 + per-asset 覆盖，半独立）

| 参数 | 值 |
|------|-----|
| `trend_ma_spread_pct` | 3.0% (默认) + GC=F 5%, NDQ.AX 4%, BTC/ETH 8% |

**耦合逻辑**：与簇 1 松耦合 — 趋势判定和 crash 判定是不同的 regime 分支，不直接竞争样本。但 `trend_ma_spread_pct` 决定 range_bound 的宽度，间接影响 crash/recovery 路径的优先级判定（crash 最高、recovery 次之、趋势再次之）。

**可独立调的理由**：per-asset 覆盖机制已经隔离了资产间的干扰。默认值只影响未覆盖资产。与簇 1 的耦合是弱耦合（优先级排序，非数值依赖）。

#### 簇 3：Recovery 判定（2 参数，紧密耦合）

| 参数 | 值 |
|------|-----|
| `recovery_rebound_pct` | 10.0% |
| `recovery_quantile_max` | 0.50 |

**耦合逻辑**：recovery = 反弹 ≥ X% AND 分位 < Y。两个参数是 AND 关系 — 调 X 必须同时调 Y，否则要么过度触发（X 低 + Y 高）要么从不触发（X 高 + Y 低）。

#### 簇 4：价格分位边界（2 参数，对称约束）

| 参数 | 值 |
|------|-----|
| `low_quantile_threshold` | 0.20 |
| `high_quantile_threshold` | 0.80 |

**耦合逻辑**：两者定义"低位"和"高位"的分界线，中间是"中段"。语义上是对称的 — 如果 low 调到 0.15，high 应该相应调到 0.85 保持中段宽度。但实际耦合不强（且 2026-06 拆方向锁后两者已停用——strategy hint 不再读这组阈值）。

**可独立调**：弱耦合，可以分别优化。但如果追求中段宽度一致，联合 sweep 更好。

#### 簇 5：HOLD 判定（3 参数，紧密耦合）

| 参数 | 值 |
|------|-----|
| `K_FLAT` | 1.0 |
| `FLAT_CEILING_PCT` | 8.0% |
| `DEFAULT_DAILY_VOL_PCT` | 2.0% |

**耦合逻辑**：HOLD 命中 = `|return| < min(K_FLAT × ATR% × sqrt(days), FLAT_CEILING_PCT)`。K_FLAT 是乘数，FLAT_CEILING 是封顶，DEFAULT_DAILY_VOL 是 ATR 失败时的回退。三者共同定义"多大算没动"。

**K_FLAT 特殊性**：它是乘数，线性影响 flat band 宽度。但 FLAT_CEILING 是 min() 封顶 — 当 K_FLAT × ATR × sqrt(days) > 8% 时，K_FLAT 再大也没用。所以 K_FLAT 的有效范围取决于 ATR 的实际分布。

#### 簇 6：VIX/TNX 分桶（2 组 × 2 阈值 = 4 参数，分组内耦合）

| 参数 | 值 |
|------|-----|
| VIX low/mid 边界 | 18 |
| VIX mid/high 边界 | 25 |
| TNX low/mid 边界 | 4.0 |
| TNX mid/high 边界 | 4.5 |

**耦合逻辑**：VIX 的两个阈值是一组（定义 3 个桶），TNX 的两个阈值是一组。VIX 和 TNX 之间无耦合（独立的宏观维度）。

**可拆为 2 个独立单元**：VIX 分桶 + TNX 分桶。

#### 簇 7：Dreaming 入选门槛（2 参数，弱耦合）

| 参数 | 值 |
|------|-----|
| `MIN_RECALL` | 3 |
| `MIN_SCORE` | 0.8 |

**耦合逻辑**：候选必须同时满足 count ≥ MIN_RECALL AND score ≥ MIN_SCORE。MIN_SCORE 内部含 sample = min(count/10, 1.0)，所以 MIN_RECALL 间接影响 score 的下界。但耦合较弱 — MIN_RECALL 是硬门槛，MIN_SCORE 是软评分。

**ADR 008 说明**：MIN_SCORE = 0.8 来自 OpenClaw 同款，ADR 008 里没有说"故意不调"，但也没有说要调。

#### 簇 8：Dreaming 回看窗口（2 参数，独立于其他簇）

| 参数 | 值 |
|------|-----|
| `WINDOWS` | [7, 30] |
| `LOOKBACK_DAYS` | 90 |

**耦合逻辑**：LOOKBACK_DAYS 决定摄入多少历史 verdict，WINDOWS 决定看事后几天的市场表现。两者独立 — LOOKBACK_DAYS 太小会丢样本，WINDOWS 太大会引入噪音，但互不影响。

**可拆为 2 个独立单元**。

#### 簇 9：Oracle accuracy 阈值（10 参数，5 对内部耦合）

| Verdict | +1 阈值 | -1 阈值 |
|---------|---------|---------|
| BUY | +5% | -3% |
| ACCUMULATE | +3% | -3% |
| HOLD | \|return\| ≤ 3% | \|return\| ≥ 8% |
| TRIM | -3% | +5% |
| SELL | -5% | +3% |

**耦合逻辑**：每对 (+1, -1) 阈值定义一个 verdict 的"方向判定空间"，中间是中性区。BUY 的 +5 和 -3 是 AND 关系（改一个会改变中性区宽度）。但 5 个 verdict 之间**无耦合** — BUY 的阈值不影响 SELL 的判定。

**可拆为 5 个独立单元**（每 verdict 一对）。

### 簇汇总

| # | 簇名 | 参数数 | 独立单元数 | 耦合强度 |
|---|------|--------|-----------|---------|
| 1 | Crash 触发器 | 3 | 1 | 紧密 |
| 2 | Trend 趋势 | 1 (+4 per-asset) | 1 | 弱（与簇 1） |
| 3 | Recovery 判定 | 2 | 1 | 紧密 |
| 4 | 价格分位 | 2 | 1-2 | 弱（对称约束） |
| 5 | HOLD 判定 | 3 | 1 | 紧密 |
| 6 | VIX/TNX 分桶 | 4 | 2 | 组内紧密，组间独立 |
| 7 | Dreaming 门槛 | 2 | 1 | 弱 |
| 8 | Dreaming 回看 | 2 | 2 | 独立 |
| 9 | Oracle 阈值 | 10 | 5 | 对内耦合，对间独立 |
| **合计** | | **28** (不含 per-asset) | **15-16** | |

**结论**：28 个 Type A 参数拆成 **15-16 个独立调参单元**。真正紧密耦合、必须联合 sweep 的是簇 1（crash 三参数）、簇 3（recovery 两参数）、簇 5（HOLD 三参数）。其余可以独立优化。

---

## 2. "Do-Not-Tune" 列表

### 明确不应被校准的参数（有设计意图或 ADR 约束）

| 参数 | 依据 | 为什么不动 |
|------|------|-----------|
| `K_FLAT = 1.0` | `jobs/verdict_review.py:241-243` 注释：**"固定的是规则（K_FLAT 常数）— 绝不让系统自学习这把'给自己打分的尺子'，否则会 reward hacking"** | 这是评分尺子的乘数。如果允许系统自调 K_FLAT，等于让 agent 自己决定"多大算没动"，会把及格线挪到对自己有利的位置。这是 anti-reward-hacking 设计承诺。 |
| `CAUTION_MIN_BASE_DOWN = 0.15` | ADR 008（2026-05-27 Accepted）：**"lift-based caution 评分（原理正确修正，非 reward hacking）"**，试金石验证通过 | ADR 008 明确定义了 0.15 的含义（"该 regime 的 30d 真实下行基率必须 ≥ 此值，否则踏空是单向上涨基率假象"）。调这个等于推翻 ADR 008 的试金石验证。 |
| `CAUTION_LIFT_FULL = 0.20` | ADR 008 同上 | 同上。0.20 是"lift 达到此值算满分 quality"的定义。 |
| Dreaming score 权重 `0.7 / 0.3` | `jobs/dreaming.py:474-476` 注释：**"命中率已是方向正确性的唯一可信度量"** + **"不再用 abs(avg_return) 加分——对 HOLD 而言市场动得越大恰恰说明 HOLD 越错"** | 0.7 hit_rate + 0.3 sample 是经过推理设计的，不是拍的。hit_rate 的主导地位有明确的逻辑论证（HOLD 的反直觉特性）。调权重 = 改变"什么算可信模式"的定义。 |
| `FLAT_CEILING_PCT = 8.0%` | `jobs/verdict_review.py:243` 注释：**"防 atr 异常时尺子失控"** | 这是 K_FLAT 的安全阀。如果 K_FLAT 不动，FLAT_CEILING 也不应单独调（它们是簇 5 的一部分，但 K_FLAT 是 do-not-tune，所以整个簇 5 都锁死）。 |
| `DEFAULT_DAILY_VOL_PCT = 2.0%` | `jobs/verdict_review.py:246` 注释：**"atr_pct 拉取失败时的兜底"** | 工程容错值，不是决策参数。调它不影响正常路径，只影响 ATR 拉取失败的边缘 case。 |
| CIO prompt v1 现金机会成本规则 | ADR 007 + training_report：**"v1 prompt 改动是 unblock 关键（reward 0 → 0.4），prompt 内容是真天花板"** | 这不是数值参数，是 prompt 文本。training_report 明确说"prompt 是真天花板，hyperparameter 顶不破"。调数值参数不动 prompt 是当前共识。 |
| CIO zero-shot（不用 few-shot） | ADR 007 Accepted：**"CIO 保持 zero-shot...few-shot 这条路已验证不通"** | 同上。ADR 钉死了不走 few-shot。 |

### 待用户决定的参数

| 参数 | 当前值 | 倾向 | 不确定原因 |
|------|--------|------|-----------|
| `MIN_SCORE = 0.8` | OpenClaw 同款 | **倾向于 do-not-tune** | 来源明确（OpenClaw），但无自身验证。如果 OpenClaw 的 0.8 是基于不同架构（非 LLM committee），这个值可能不适合 openInvest。ADR 008 没有直接约束它。 |
| `MIN_RECALL = 3` | 无依据 | **倾向于可调** | 3 次出现的模式统计上不稳定（binomial 95% CI 很宽），但 LLM 验伪 prompt 里写 "count≥6" 与之矛盾。可能应该统一到 5 或 6。 |
| `LOOKBACK_DAYS = 90` | 设计意图"只学近期" | **倾向于可调** | 90 天是经验值。太短（<60）样本不够；太长（>180）可能学到已过时的行为模式。但"只学近期"的原则本身是有道理的。 |
| `WINDOWS = [7, 30]` | "1d 是 horizon 假象" | **倾向于 do-not-tune** | 有明确论证：1d 内 HOLD 几乎不动会被自动判命中，是假象。7d/30d 的选择是合理的。但可以考虑加 60d 或 90d 窗口（不替换现有窗口）。 |
| `VIX 分桶 18/25` | 标准经验值 | **可调** | 18/25 是 VIX 的经典分界线（恐慌/极度恐慌），有金融学共识。但 openInvest 的 regime 标签是给 Dreaming 聚合用的，不一定需要跟教科书一致。 |
| `TNX 分桶 4.0/4.5` | 经验值 | **可调** | 4.0/4.5 不是经典分界线（10Y UST 没有标准的"高中低"定义）。是拍的。 |

---

## 3. 之前那次 Optuna 30-trial 的完整 Setup

### 3.1 训练数据

| 项目 | 值 |
|------|-----|
| **训练期** | 2024-01-02 → 2024-04-30（`train_start` / `train_end`） |
| **验证期** | 2024-05-01 → 2024-06-30（每 5 个 trial 在 val 上测一次） |
| **Hold-out** | 2024-11-18 → 2024-12-31（不重叠，最终验证） |
| **资产** | NDQ.AX, GC=F |
| **决策频率** | 每 7 天一个 decision_date（step_days=7） |
| **初始资金** | ¥100,000 |
| **LLM** | deepseek-v4-flash (non-thinking mode) |

**数据时期问题**：训练期 2024-01-02 → 2024-04-30 在 DeepSeek 训练数据 cutoff 之前。这意味着 LLM 对这个时期可能有记忆，verdict 不是真正的 out-of-sample。但 hold-out 2024-11-18 → 2024-12-31 也在 cutoff 之前。

**确认**：这次 Optuna 的训练数据**全部在 DeepSeek 训练数据内**，存在 LLM 记忆污染风险。

### 3.2 目标函数

```python
reward = compute_strategy_reward(metrics)
       = annualized_return_pct / 100
       - 0.5 * max_drawdown_pct / 100
       + 0.5 * alpha_vs_yuebao_pct / 100
       + 0.2 * max(0, sharpe_ratio - 1.0)
```

**来源**：`core/backtest_reward.py:compute_strategy_reward`

**评估指标**：完整的 walk-forward paper trading P&L 曲线 → `strategy_metrics` 计算年化收益/回撤/Sharpe/超额收益 → 合成单数值 reward。

**是否干净**：**是的，目标函数干净**。它基于完整的 P&L 曲线（每天 mark-to-market），不是基于 LLM 命中率。Reward 衡量的是"如果每次都听 AI 的，账户实际赚多少"，不涉及 verdict 方向判定的对错。

**关键区别**：
- ✅ Reward = 真实 P&L 曲线 → 不受 LLM 命中率定义影响
- ❌ 如果用 verdict 命中率当目标 → 受 K_FLAT / oracle 阈值 / HOLD 判定定义污染
- 结论：**Optuna 的目标函数是干净的，placebo 结论有效**

### 3.3 搜索空间

| 参数 | 范围 | 类型 | 消费路径 |
|------|------|------|----------|
| `regime_uptrend` | 3.0 ~ 6.0 | float | monkey-patch `regime.THRESHOLDS["trend_ma_spread_pct"]` |
| `regime_atr` | 2.5 ~ 5.0 | float | monkey-patch `regime.THRESHOLDS["crash_atr_pct_min"]` |
| `max_rounds` | 1 ~ 3 | int | env `INVEST_MAX_DEBATE_ROUNDS` → `backtest_committee.py` |
| `cio_confidence_cap` | 0.70 ~ 0.95 | float | env `INVEST_CIO_CONFIDENCE_CAP` → `parse_cio_memo()` |
| `alloc_aggressiveness` | 0.05 ~ 0.30 | float | env `INVEST_ALLOC_AGGRESSIVENESS` → `parse_cio_memo()` |

**采样器**：TPE (Tree-structured Parzen Estimator)，seed=42
**方向**：maximize
**Trial 数**：30

### 3.4 最佳 Trial 结果

**Trial #20**：
```
regime_uptrend:      4.625
regime_atr:          3.682
max_rounds:          2
cio_confidence_cap:  0.754
alloc_aggressiveness: 0.245

Reward:              0.4223
Annualized Return:   +34.4%
Max Drawdown:        15.4%
Sharpe Ratio:        1.37
```

### 3.5 判定"Placebo"的依据

**统计依据**：
```
30 trials:
  mean:   0.3952
  median: 0.4165
  stdev:  0.0371
  min:    0.2621
  max:    0.4223
```

**Top 5 几乎平局**（reward 差距 < 0.002，即 < 0.5%）：

| Trial | Reward | regime_uptrend | regime_atr | max_rounds | cio_cap | alloc_agg |
|-------|--------|---------------|------------|------------|---------|-----------|
| #20 | 0.4223 | 4.63 | 3.68 | 2 | 0.754 | 0.245 |
| #4 | 0.4223 | 4.84 | 2.85 | 1 | 0.792 | 0.164 |
| #29 | 0.4210 | 5.59 | 2.72 | 1 | 0.773 | 0.224 |
| #1 | 0.4210 | 3.47 | 2.65 | 3 | 0.850 | 0.227 |
| #9 | 0.4205 | 4.99 | 3.28 | 2 | 0.837 | 0.096 |

**逐参数 placebo 判定**：

| 参数 | 判定 | 依据 |
|------|------|------|
| `regime_uptrend` | **placebo** | Top 5 里 3.47 和 5.59 都能拿到 0.421，跨度覆盖搜索范围的 86% |
| `regime_atr` | **placebo** | Top 5 里 2.65 和 3.68 都能拿到 0.42，跨度覆盖搜索范围的 41% |
| `max_rounds` | **placebo** | 1/2/3 都能进 Top 5，reward 差距 < 0.001 |
| `cio_confidence_cap` | **placebo** | 0.754 和 0.850 都能拿到 0.42+，跨度覆盖搜索范围的 63% |
| `alloc_aggressiveness` | **placebo** | 0.096 和 0.245 都能拿到 0.42，跨度覆盖搜索范围的 60% |

**Bottom 5 检查**：最差 trial #25 reward=0.2621，与 best 差 0.16（38%）。但 #25 的 params (4.46, 3.06, 3, 0.73, 0.19) 与 #27 (4.46, 3.06, 3, 0.73, 0.25) 几乎相同，说明这个区域可能是 Optuna 采样到的局部坏区（LLM 随机性导致的噪音），不是参数本身的锅。

**核心结论**：Reward landscape 几乎平（stdev/mean = 9.4%），5 个参数都不是 reward 的主驱动因子。**Prompt 内容才是天花板**。

### 3.6 重要 Caveat（placebo 结论的适用边界）

1. **训练数据在 DeepSeek 训练数据内** — LLM 可能有记忆，verdict 质量不代表真实 out-of-sample
2. **只有 2 个资产**（NDQ.AX + GC=F）— 对多资产组合可能不同
3. **训练窗口只有 4 个月**（2024-01 → 2024-04）— 样本量有限
4. **TPE 采样器 + 30 trial** — 对 5 维空间的覆盖可能不够（每个维度平均只有 ~2 个有效采样点）
5. **Prompt v1 是在 Optuna 之前改的** — baseline 已经被 prompt 改动 shift 了，Optuna 搜的是 prompt v1 之后的微调空间

**结论的有效范围**：在 prompt v1 + 2 资产 + 4 月训练窗口 + DeepSeek v4-flash 的组合下，5 个 hyperparameter 是 placebo。换 prompt/资产/模型后需要重新验证。

---

## 4. Type B 参数的 Retrospective 可行性细分

### 4.1 "事后口径"型（改了能用已有 jsonl 重算）

这些参数只影响**评估/判定逻辑**，不影响 LLM 的 verdict 输出。改了之后可以用已有的 `verdict_review.jsonl` + `backtest/` 数据重算，不需要重跑 LLM。

| 参数 | 为什么是"事后口径" | 可用数据 | retrospective 方法 |
|------|-------------------|----------|-------------------|
| `buy_confidence_overdrive` (0.95) | 这个参数在 `parse_cio_memo()` 里做后处理 — LLM 已经输出了 confidence，这里只是决定"是否降级"。改阈值不影响 LLM 看到什么 prompt。 | verdict_review.jsonl（每条有 confidence + verdict + 事后 return） | 用已有 jsonl，对每条 BUY verdict 按不同 confidence 阈值做降级 → 重算命中率 → 找拐点 |
| `buy_confidence_downgrade_to` (0.6) | 同上。降级后的目标值是后处理。 | 同上 | 同上。对每个候选降级值，重算降级后的 verdict 分布 + 命中率 |
| `alloc_cny_ceiling` (100k) | 在 `parse_cio_memo()` 里做 clamp — LLM 已经输出了 alloc_cny，这里只是决定"是否截断"。 | backtest transcript（`.backtest/<date>/<sym>.md` 含 CIO memo 原文，regex 提取 alloc_cny） | 用已有 transcript，对不同 ceiling 值重算"多少次被 clamp" + clamp 后 P&L 变化 |
| `worker_unavailable_confidence_floor` (0.4) | 在 `parse_cio_memo()` 里做后处理 — 检测 `[WORKER_UNAVAILABLE]` 标记后降级。 | verdict_review.jsonl（少数含 worker 失败的记录） | 筛含 worker 失败的记录，对不同 floor 值重算 P&L |
| `MAX_DEBATE_ROUNDS` | 这个参数**影响 LLM 调用量**（round 1 vs round 3），但 verdict_review.jsonl 不记录 round 数。 | 需要重跑 backtest with 不同 rounds | **部分可 retrospective**：可以用 Optuna 数据（trial 1/2/3 分别是 rounds 1/2/3）直接比。但样本少（30 trial / 3 rounds ≈ 10 per bucket）。 |
| `max_tool_iterations` (4) | 影响 LLM 的 tool call 轮次，间接影响 LLM 看到的信息量。 | llm_telemetry.jsonl（记录 iteration 字段） | **部分可 retrospective**：看历史 telemetry 里"iteration 到 4 时 LLM 是否还想继续调 tool"。但改了 iteration 上限会改变 LLM 行为。 |

### 4.2 "影响 LLM 决策"型（改了 LLM 会出不同 verdict，必须重跑）

这些参数直接影响 LLM 看到的 prompt 或 LLM 的行为模式。改了之后 LLM 会输出不同的 verdict，历史 jsonl 里的 verdict 不再适用。

| 参数 | 为什么"影响 LLM 决策" | 污染限制 | 是否可绕开 |
|------|----------------------|----------|-----------|
| `temperature` (0.2) | 直接影响 LLM 输出的随机性。temperature 0.1 vs 0.5 会产生完全不同的 verdict 分布。 | **最重** — 无法 retrospective，必须 A/B 测试 | 无法绕开。需要在同一日期跑 temperature=0.1 和 temperature=0.5 的两次 backtest，比较 P&L。成本 = 2x LLM 调用。 |
| `MIN_SCORE` (0.8) | 改变哪些 Dreaming insight 被写入长期记忆 → 改变 `prior_insights` 注入 → 改变 LLM 的 CIO verdict。 | **中等** — insight 注入是间接的，且当前 insight 数量很少 | **部分可绕开**：如果当前 insight 数量为 0（caution 正式休眠，ADR 008），改 MIN_SCORE 对当前 verdict 无影响。但对未来有 insight 后的 verdict 有影响。 |
| Dreaming `_score` 权重 (0.7/0.3) | 改变 insight 的排序和入选 → 改变 CIO 看到的 prior_insights。 | **中等** — 同 MIN_SCORE | **部分可绕开**：同上。当前 insight 少时影响小。 |
| `LOOKBACK_DAYS` (90) | 改变 Dreaming 摄入的历史窗口 → 改变 insight 质量和数量 → 改变 CIO 行为。 | **中等** — 间接影响 | **部分可绕开**：如果当前 insight 少，改 LOOKBACK_DAYS 对 verdict 无直接影响。但对 Dreaming 本身的模式发现有影响。 |

### 4.3 汇总矩阵

| 参数 | 类型 | 可 retrospective？ | sweep 策略 |
|------|------|-------------------|-----------|
| `buy_confidence_overdrive` | 事后口径 | ✅ 用已有 jsonl 重算 | 直接进 sweep 空间 |
| `buy_confidence_downgrade_to` | 事后口径 | ✅ 用已有 jsonl 重算 | 直接进 sweep 空间 |
| `alloc_cny_ceiling` | 事后口径 | ✅ 用已有 transcript 重算 | 直接进 sweep 空间 |
| `worker_unavailable_floor` | 事后口径 | ✅ 用已有 jsonl 重算 | 直接进 sweep 空间 |
| `MAX_DEBATE_ROUNDS` | 部分事后 | ⚠️ Optuna 数据可用但样本少 | 用 Optuna 现有数据判断，不重跑 |
| `max_tool_iterations` | 部分事后 | ⚠️ telemetry 可分析但不完全 | 用 telemetry 分析，不重跑 |
| `temperature` | 影响 LLM 决策 | ❌ 必须重跑 | 归入 do-not-tune 或单独 A/B |
| `MIN_SCORE` | 影响 LLM 决策（间接） | ⚠️ 当前 insight 少时可忽略 | 低优先级，等 insight 积累后再评估 |
| Dreaming score 权重 | 影响 LLM 决策（间接） | ⚠️ 同上 | 低优先级 |

**结论**：9 个 Type B 参数中，**4 个可以干净 retrospective**（confidence_overdrive、confidence_downgrade、alloc_ceiling、worker_floor），**2 个可以部分 retrospective**（max_rounds、max_tool_iterations），**3 个必须重跑 LLM 或等数据积累**（temperature、MIN_SCORE、score 权重）。

**Sweep 策略**：先 sweep 28 个 Type A（15-16 个独立单元），再 sweep 4 个"事后口径"型 Type B。"影响 LLM 决策"型暂不碰。
