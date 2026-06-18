---
type: adr
title: "ADR 008 — caution 改用 lift-based 评分；现有数据下正式休眠"
tags: [dreaming, caution-insight, scoring, regime, backtest]
intent: 记录 caution insight lift-based 评分决策及休眠理由
documents:
  endpoints: []
  config_keys: []
  symbols: [_score, VerdictReview]
status: accepted
date: 2026-05-27
supersedes: []
superseded_by: []
---

# ADR 008 — caution 改用 lift-based 评分；现有数据下正式休眠

**日期**: 2026-05-27（2026-05-27 二次更新：采纳 lift-based 评分 + post-cutoff 验证）
**状态**: Accepted
**关联**: [007-few-shot-retirement](007-few-shot-retirement.md) · [03-dreaming](../03-dreaming.md) · `jobs/dreaming.py:_score` · `jobs/verdict_review.py`

## 背景

Dreaming 的 caution insight（"该出手却 HOLD 踏空"，用来反保守地压低 CIO 的 HOLD 倾向）
旧评分用**绝对 missed_up_rate**：`score = missed_up×0.7 + sample×0.3`。问题：在**单向上涨**
的 regime 里 HOLD 必然踏空（市场就是涨），missed_up 天然高 → 把基率假象当强信号。
Phase 1.5（纯牛市）那条 `NDQ HOLD/range_bound 30d` 踏空 61% 就是典型假 caution
（`avoided_down=0`，根本没有下行可言）。

## 关键证据（两批数据 + 确定性基率）

| 候选 | regime | missed_up | base_up | **base_down** | lift=missed_up−base_up |
|---|---|---|---|---|---|
| Phase1.5 假 caution | NDQ range_bound 30d | 0.61 | 0.75 | **0.00** | **−0.14** |
| post-cutoff "真" caution | NDQ downtrend 30d | 0.58 | 0.66 | **0.04** | **−0.08** |
| post-cutoff | NDQ range_bound 30d | 0.30 | 0.29 | 0.44 | +0.02 |

- **两条 caution 其实都是基率假象**：Phase1.5 是"牛市永远涨"，post-cutoff downtrend 是
  "急跌后 V 反弹 71% 涨"——`base_down≈0`，"踏空"是反弹必然，不是 HOLD 的错。
- **最深的发现**：有真实下行风险的时点（range_bound/uptrend，`base_down` 0.24–0.44）
  HOLD 反而是**保护性的（reliable）**。"该出手却 HOLD 踏空"在干净数据里根本不成立。

## 决策 1：采纳 lift-based caution 评分（原理正确的修正，非 reward hacking）

`jobs/dreaming.py:_score` 的 caution 分支改为：

```
base_up / base_down = 该 (asset, regime, window) 在所有 verdict 上的原始 30d 涨/跌基率
若 base_down < CAUTION_MIN_BASE_DOWN(0.15):  return 0   # regime 无真实下行 → 踏空是基率假象
lift = missed_up_rate − base_up
若 lift <= 0:                                 return 0   # HOLD 没比基率更踏空 → 非真信号
score = min(lift / CAUTION_LIFT_FULL(0.20), 1) × 0.7 + sample × 0.3
```

数据流：`verdict_review` 给每条 review 加 `directions`（每窗口原始市场方向 up/down/flat，
verdict 无关，用与 HOLD 同一条 atr flat band）→ `dreaming.rem_sleep` 按 (asset,regime,window)
聚合成 base_up/base_down 附到候选 → `_score` 算 lift。reliable 评分不变（仍 hit_rate 基）。

**试金石（证明不是为放过某条而换公式）**：
- Phase1.5 假 caution（missed_up0.61/base_up0.75/base_down0.00）→ **score 0，拒绝** ✓
- post-cutoff "真" caution（NDQ downtrend，base_down0.04）→ **score 0，也拒绝** ✓（不偏袒）
- 假想真 caution（missed_up0.70/base_up0.50/base_down0.30，lift+0.20）→ **score 1.0，接受** ✓

→ 同时拒绝两个基率 artifact、只接受"真有下行 + HOLD 比基率更踏空"的——**一视同仁，原理正确**。

## 决策 2：现有所有数据下，caution 正式休眠（信号不存在，不是门槛高）

在 Phase1.5（牛市）+ post-cutoff（V 形急跌）全部数据下，**0 条 caution 过门**——
不是 0.8 门太高，而是**这些数据里真的没有合格 caution**（没有"真实下行 + HOLD 系统性踏空后续"）。
这是**正确的休眠态**，不是机制失败。反保守继续靠 CIO 的 `CONCENTRATION_PCT` 机会成本规则
+ 已落盘的 reliable insight。**不启用任何 caution。**

## 决策 3：解锁 caution 缺的不是公式（已就位），是数据

要让 caution 真正固化，需要**含持续阴跌 / 非 V 形下行**的 regime 数据——HOLD 在真实持续
下行里没躲掉、又踏空后续（`base_down>0` 且 lift>0）。干净的（post-cutoff）只能等未来慢熊；
2018-2023 的 2022 全年阴跌正是这种 `base_down>0` 的持续下行，但在 DeepSeek 训练数据内（污染）。
**在拿到这种数据前，caution 维持休眠。**

## 决策 4（比 caution 更大的结论）："系统过度保守"假设缺乏证据

这两天追的"委员会过度保守、该出手却 HOLD"——**在干净数据里缺乏可证明的证据**：
- 有真实下行风险时（base_down 0.24–0.44），HOLD 是**对的**（保护性，avoided_down 高）。
- 急跌后的"踏空"是 V 反弹基率（base_down≈0），不是判断失误。
- lift-based 下没有一条 caution 显出正 lift。

→ **"过度保守"作为一个可证伪的缺陷，在现有数据里不成立。** 记录此条，避免未来再去
追一个数据上不存在的缺陷。若未来真要重启这条假设，先证明在 base_down>0 的 regime 里
HOLD 的 missed_up 显著 > base_up（即真正错过了可赚的钱），再谈。

## 边界
- 改了 `jobs/dreaming.py`（_score + base 率）、`jobs/verdict_review.py`（directions 字段）、
  对应测试。**未启用任何 caution、未跑写 live 的 deep_sleep、未上 live、未 commit。**
- post-cutoff 实验数据在 `memory/.backtest/`；Phase 1.5 归档在 `memory/.backtest_archive_phase15_clean_20260527/`；
  3 条 post-cutoff reliable insight 待审在 `memory/insights_postcutoff_pending_review/`（未进 live）。
