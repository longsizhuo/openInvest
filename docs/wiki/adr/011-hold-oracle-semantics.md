---
type: adr
title: "ADR-011 — HOLD Oracle 语义：hold_wrong 阈值该设多宽"
tags: [oracle, hold, backtest, threshold, verdict]
intent: 决策参数
documents:
  endpoints: []
  config_keys:
    - oracle_accuracy.hold_neutral
    - oracle_accuracy.hold_wrong
  symbols:
    - verdict_oracle_accuracy
    - compute_strategy_reward
status: proposed
date: "2026-05-27"
supersedes: []
superseded_by: []
---

# ADR-011 — HOLD Oracle 语义：hold_wrong 阈值该设多宽

**日期**: 2026-05-27
**状态**: Proposed
**关联**: [010-param-management](010-param-management.md) · `core/config/defaults.yaml` oracle_accuracy

## 背景

Oracle 用来评估委员会 verdict 的事后准确性。对 HOLD 的判定：

```python
# core/backtest_reward.py
if v == "HOLD":
    if abs(r) <= hold_neutral:   return 1   # hit
    if abs(r) >= hold_wrong:     return -1  # wrong
    return 0                                 # neutral
```

当前值：`hold_neutral=3.0`, `hold_wrong=8.0`。

数据（276 条 verdict_review.jsonl，119 条 HOLD）：
- hit (|ret| <= 3%): 35 条 (29.4%)
- neutral (3% < |ret| < 8%): 52 条 (44.0%)
- wrong (|ret| >= 8%): 32 条 (26.6%)

问题：hold_wrong=8 意味着"HOLD 之后 30 天涨/跌 >= 8%"算判错。
但涨 9% 时说"别动"真的是错吗？

## 决策

### HOLD 的语义定义

**HOLD = "不操作"，不是"市场不会动"。**

委员会说 HOLD，意思是"基于当前信息，不值得付出交易成本去调仓"。
它**不预测**市场方向——它评估的是"动"的风险/收益是否超过交易摩擦。

因此：
- HOLD 之后涨 9% → 这是"踏空"，不是"判错"。HOLD 没说"不该涨"。
- HOLD 之后跌 9% → 这是"躲跌失败"，更接近"判错"。
- HOLD 之后原地不动 → 这是理想情况。

### 但有个边界：HOLD 不能完全免责

如果 HOLD 之后跌 15%，说"我只是没操作"是推卸责任——委员会有义务在
风险信号明确时建议 TRIM/SELL，而不是什么都不做。

所以 hold_wrong 需要区分方向：
- **涨太多（> hold_wrong）→ 不算 wrong**：踏空是遗憾，不是错误。
  HOLD 的语义是"不动"，市场涨了不违反这个语义。
- **跌太多（< -hold_wrong）→ 算 wrong**：委员会有风控职责，
  大跌时 HOLD 等于失职。

### 新阈值方案

```python
if v == "HOLD":
    if abs(r) <= hold_neutral:       return 1   # hit：市场没动，HOLD 正确
    if r <= -hold_wrong:             return -1  # wrong：大跌，HOLD 失职
    return 0                                    # neutral：其他情况（含涨太多）
```

关键变化：**只在下跌方向判 wrong，上涨方向一律 neutral。**

候选阈值（待 sweep 验证，不在此 ADR 决定）：
- `hold_wrong=8`（当前值）：ret <= -8% = wrong
- `hold_wrong=10`：ret <= -10% = wrong
- `hold_wrong=12`：ret <= -12% = wrong

敏感性分析（当前数据）：

| hold_wrong | wrong 数 | wrong% | 说明 |
|------------|----------|--------|------|
| 8（当前，双向） | 32 | 26.9% | 含涨 8%+ 也算错 |
| 8（只下跌） | ~18 | ~15% | 去掉"涨太多"的 false wrong |
| 10（只下跌） | ~12 | ~10% | |
| 12（只下跌） | ~7 | ~6% | |

注：精确数字需要改代码后重跑，上表是估算。

### 为什么不直接改

1. 改 oracle 函数语义（从双向判错→单向判错）会影响 reward 计算，
   进而影响 Dreaming 的 insight 学习。需要确认 `compute_strategy_reward`
   和 `verdict_oracle_accuracy` 的调用方不受意外影响。
2. 阈值具体选几，需要 sweep 验证（ADR-010 Rule 2: train+holdout 双指标）。
3. 这个改动和其他 oracle 阈值（accumulate_negative 等）是独立的，
   不需要捆绑。

## 证据

- 119 条 HOLD 中，|ret| >= 8% 的 32 条：涨 8%+ 和跌 8%+ 大约各半
- 涨 8%+ 时判 wrong = 惩罚了"没踏空"的 HOLD，语义上不合理
- ACCUMULATE wrong 分析显示：downtrend 中 ACCUMULATE wrong 率仅 8%，
  说明委员会在下跌趋势中判断质量好——HOLD 在大跌时应该有更高标准

## 执行

1. 改 `core/backtest_reward.py:verdict_oracle_accuracy()` 的 HOLD 分支：
   只在 `r <= -hold_wrong` 时判 -1，`r >= +hold_wrong` 时判 0
2. 更新 `tests/test_backtest_reward_v2.py` 覆盖新语义
3. 重跑 sweep 验证 hold_wrong 阈值选择
4. 更新 `defaults.yaml`（如果阈值变化）

## 边界

- 本 ADR 不改 hold_neutral（|ret| <= 3% = hit，这个语义没争议）
- 本 ADR 不改其他 verdict 的 oracle 语义（BUY/ACCUMULATE/TRIM/SELL 的
  正反向判定语义是清晰的）
- 本 ADR 不决定 hold_wrong 的具体数值——只决定语义方向（单向 vs 双向）
