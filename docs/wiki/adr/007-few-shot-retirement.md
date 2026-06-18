---
type: adr
title: "ADR 007 — few-shot 路线退役，CIO 保持 zero-shot"
tags: [few-shot, cio, dspy, committee, verdict]
intent: 决策参数
documents:
  endpoints: []
  config_keys: []
  symbols: [build_cio_prompt]
status: accepted
date: "2026-05-27"
supersedes: []
superseded_by: []
---

# ADR 007 — few-shot 路线退役，CIO 保持 zero-shot

**日期**: 2026-05-27
**状态**: Accepted
**关联**: [004-v1-fallback-retirement](004-v1-fallback-retirement.md) · `agents/cio.py`（docstring 记录了 v2 不接入）· `experiments/archive_few_shot_failed/`

## 背景

为了让 CIO 不那么过度保守，曾尝试用 DSPy 给 CIO 注入 few-shot demos（从历史
oracle 标注的 trainset 里学"该出手时出手"）。共做了 4 个版本：

| 版本 | trainset | 结果 |
|------|----------|------|
| v1 | 66 样本，2024-05~11 单 regime | verdict 塌缩 + symbol 丢失等 6 个结构问题，**从未接入** |
| v2 / v2.2 | 5565 样本，2024-05~2026-05 跨 regime，5 verdict | MIPROv2 优化 = **+0.0pp**（baseline 0.67 已接近天花板）；random demos 把 reasoning 里 REGIME mention 从 64% 压到 1.8% |
| v3 | v2 经 path_c alpha-oracle 重标 | 塌缩成 3 档（BUY 52% / ACC,TRIM=0%）；sandbox holdout（2025-11~2026-02，200 样本）：**optimized 比 zero-shot −1pp、比 random demos −5pp、alpha 跑输买入持有**。结论：保留 zero-shot |
| v4 | Phase 1.5（2023-24 纯牛市）经 path_c 重标 | 塌缩**更重**：BUY 59.5% / ACC,TRIM=0%。**未跑 holdout**——见下方根因，机理已锁定 |

## 根因（不是数据问题，是 oracle 结构缺陷）

path_c oracle = "标 forward-30d 组合终值最大的 verdict"，alloc 映射
`BUY=+1.0 / ACCUMULATE=+0.5 / HOLD=0 / TRIM=−0.1 / SELL=−0.3`。

- fwd>0 时：BUY(1.0) 的终值**恒 ≥** ACCUMULATE(0.5) → ACCUMULATE **数学上永远不可能**是 per-sample 最优。
- fwd<0 时：减仓里 SELL(−0.3) 比 TRIM(−0.1) 减得多、终值更高，且 fwd=0 / asset_pct 边界一律 HOLD tiebreaker → TRIM 也**永远不可能**胜出。
- ⇒ oracle 只会吐 BUY / HOLD / SELL 三档，与喂什么数据无关。Phase 1.5 是纯牛市，只是把 BUY 占比从 52% 进一步推到 59.5%。

few-shot 的另一独立伤害（v2 实测）：random/optimized demos 都会**压制 CIO 的 REGIME 推理**（mention 64%→1.8%），即使命中率没降，reasoning 质量也掉。

## 决策

1. **CIO 保持 zero-shot**（`agents/cio.py:build_cio_prompt` 走纯 `load_skill("cio")`）。
   verdict 命中率天花板实测 ~67%，few-shot 顶不破。
2. **反保守改走 Dreaming insight 注入**（caution/reliable pattern，见 [03-dreaming](../03-dreaming.md)），
   不再走 few-shot。Dreaming 的踏空告诫（如"震荡市 HOLD 30d 踏空率高"）是压低过度 HOLD 的正确杠杆。
3. **不再投入 few-shot / DSPy oracle 重训**，除非先解决 path_c 的结构缺陷
   （需要重新设计 alloc 模型让 ACCUMULATE/TRIM 在某些 state 下可成为最优，
   或换非-terminal-value 的 oracle 目标）。在那之前，这条路视为已验证不通。

## 证据留痕

- v1–v3 产物 + sandbox A/B：`experiments/dspy_optimized_v*.json`、`experiments/sandbox/sandbox_v2_results.json`（v3 holdout 三 arm 对比）。
- v4 产物：`experiments/archive_few_shot_failed/`（trainset + stats，未编译 holdout）。
