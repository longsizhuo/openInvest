---
type: adr
title: ADR-019 — 移除 SOLVENCY 集中度自动兜底（集中度只由 lens 控制）
status: accepted
date: 2026-06-23
tags: [concentration, trim, solvency, lens, cio, adr]
intent: 决策参数
schema_source:
  - core/committee/cio_parse.py:parse_cio_memo
  - core/config/tunable.py:VerdictConfig
  - core/config/_loader.py:API_SETTABLE
documents:
  endpoints:
    - GET /api/config
    - PUT /api/config
  config_keys:
    - verdict.concentration_lens_enabled
  symbols:
    - parse_cio_memo
supersedes: []
superseded_by: []
---

# ADR-019 — 移除 SOLVENCY 集中度自动兜底（集中度只由 lens 控制）

**日期**: 2026-06-23
**状态**: Accepted
**关联**: [013-trim-concentration-override](013-trim-concentration-override.md) ·
        [017-config-via-api](017-config-via-api.md) ·
        [018-dca-dip-reserve](018-dca-dip-reserve.md)

## 背景

ADR-013（2026-05-28）给 `parse_cio_memo()` 加了 Sanity check 4：当
`SOLVENCY_BUFFER_LEVEL=strong`（用户有家族兜底，投资账户归零不影响生存）时，
把 `TRIM_REASON=concentration` 的减仓**自动改写成 HOLD**。立意：兜底充足 ⇒ 账户内
集中度高不是"真实财富风险"。

ADR-017（2026-06-17）又加了显式开关 `verdict.concentration_lens_enabled`，让用户
**主动声明**"别拿集中度烦我"，不必再靠填假的 `emergency_buffer_cny` 触发 strong。

于是同一个问题（"集中度该不该约束这笔钱"）有了**两套答案**：solvency 自动推断 vs
lens 显式声明。这次裁决：**砍掉自动那套，只留显式那套。**

## 决策

**移除 Sanity check 4 的 `solvency_strong` 触发。集中度是否构成约束，只由
`verdict.concentration_lens_enabled` 一个显式开关说了算。**

`parse_cio_memo()` 不再接受 `solvency_strong` 参数；Sanity check 4 仅在用户关掉
lens（`concentration_lens_enabled=False`）时 force-HOLD 掉 `TRIM_REASON=concentration`。

### 为什么

1. **写死的自动兜底会掩盖真问题。** solvency 自动改写只在 parse 层事后动手，
   prompt 层并不知情：CIO 据理力争减仓（如 81% 集中 + downtrend），裁决却被悄悄
   翻成 HOLD ¥0，连"原想减 ¥40,000"的意图都不留痕。用户看到的是"无风险的 HOLD"，
   分不清是"真没风险"还是"风险被藏了"——万一有真问题就看不到了。
2. **solvency ≠ 集中度。** 家族兜底解决的是"会不会破产"（生存维度），解决不了
   "组合 81% 押一个下行资产"（分散度维度）。把两者画等号是 ADR-013 的逻辑硬伤。
3. **显式优于隐式。** 是否忽略集中度该是**用户的声明**（lens），不是系统替用户
   猜的推断（solvency）。与 ADR-018 同哲学：把资金语义做成显式 config，而非写死假设。

### 两套设计的对称性（为什么 lens 比 solvency 好）

| | solvency 自动兜底（已删） | 集中度 lens（保留） |
|---|---|---|
| 触发 | 系统从 wealth_context 自动推断 | 用户显式开关（GUI/API/env） |
| prompt 层 | 不知情 → CIO 仍据理力争减仓 | 关时同步软抑制，CIO 不再以集中度减仓 |
| 一致性 | parse 层事后翻转 → 自相矛盾 | prompt + parse 两层一致 |
| 可见性 | 静默掩盖真实减仓意图 | 用户主动免责，留痕可审计 |

## 影响

- **默认行为变了**：lens 默认 ON。集中度高的真实减仓现在**会如实显示**，不再被
  solvency 静默压成 HOLD。想免疫集中度减仓的用户**改为显式关 lens**。
- `_concentration_lens` 标记只剩 `disabled`；intervention 反事实账本 rule 只产出
  `config_concentration_lens_off`（历史 jsonl 里的 `sanity4_solvency_concentration`
  由 `rule_family()` 继续并入 `trim_blocked` 桶，聚合不丢）。
- 邮件渲染：lens 关拦下减仓时显示「减仓未执行（集中度 lens 已关）」+ CIO 原始
  减仓金额留痕，替换旧的「集中度不构成真实风险，兜底充足」措辞。

## 如何操作集中度 lens（三层对等，ADR-017）

| 途径 | 怎么做 |
|---|---|
| GUI | invest-gui「设置 → 委员会配置 → 集中度 lens」开关 |
| API | `PUT /api/config` body `{"key":"verdict.concentration_lens_enabled","value":false}` |
| CLI/agent | `skill config --set verdict.concentration_lens_enabled false` |
| env（bootstrap 默认）| `.env`: `INVEST_VERDICT_CONCENTRATION_LENS_ENABLED=false` |

关掉 = 单资产 / 刻意集中 / 全可投资金池不因持仓集中度被建议减仓（止损 / 回撤 /
波动 / 估值风险仍照看；`CONCENTRATION_PCT` 数字仍如实显示，只是不据它减仓）。

## 不改的部分

- stop_loss / bearish 等真实风险 TRIM 不受影响。
- lens 关时的硬兜底（Sanity 4 本身）保留：防 LLM 不听 prompt、把超配换标签成
  bearish 偷偷减仓——但这只在用户**主动**关 lens 时触发，是执行用户选择，不是掩盖。

## 相关文件

| 文件 | 改动 |
|---|---|
| `core/committee/cio_parse.py` | 删 `solvency_strong` 参数 + Sanity 4 的 solvency 触发；marker 固定 `disabled` |
| `core/committee/debate.py` / `core/runner/coordinator.py` | 不再计算 / 传 `solvency_strong` |
| `core/runner/intervention.py` | 集中度拦截 rule 固定 `config_concentration_lens_off` |
| `jobs/daily_report.py` / `jobs/daily_report_builder.py` | 邮件 override 措辞改「减仓未执行（lens 已关）」 |
| `core/config/tunable.py` | `forced_hold_confidence_ceiling` 注释 |
