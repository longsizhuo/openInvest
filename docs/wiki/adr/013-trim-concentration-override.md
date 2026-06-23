---
type: adr
title: "ADR-013: SOLVENCY=strong 时集中度不触发 TRIM（确定性后处理）"
tags: [concentration, trim, solvency, sanity-check, cio]
intent: 决策参数
schema_source:
  - core/committee/cio_parse.py:parse_cio_memo
  - utils/portfolio_summary.py:portfolio_summary_text
documents:
  endpoints: []
  config_keys:
    - verdict.concentration_lens_enabled
  symbols:
    - parse_cio_memo
    - portfolio_summary_text
status: accepted
date: "2026-05-28"
supersedes: []
superseded_by:
  - 019-remove-solvency-concentration-override
---

# ADR-013: SOLVENCY=strong 时集中度不触发 TRIM（确定性后处理）

> ⚠️ **2026-06-23（[ADR-019](019-remove-solvency-concentration-override.md)）已移除本 ADR 的 solvency 自动触发。**
> Sanity check 4 不再因 `SOLVENCY_BUFFER_LEVEL=strong` 改写减仓——它在 parse 层静默
> 翻转 CIO 的减仓、掩盖真实集中度风险。集中度是否构成约束现在**只由显式开关
> `verdict.concentration_lens_enabled`（ADR-017）控制**。下文 solvency 相关部分保留作
> 历史记录；`TRIM_REASON` 提取、lens-off 硬兜底、TRIM 路径化待办仍有效。

## 状态

Superseded（solvency 触发部分）— 2026-05-28 立，2026-06-23 由 ADR-019 移除

## 背景

2026-05-28 黄金 GC=F 委员会输出 TRIM，原因是 Risk Officer 报集中度 > 60%。
但实际上用户有 ¥4M 家族 backup（SOLVENCY_BUFFER_LEVEL=strong），投资账户
归零不影响生存。集中度高只意味着"账户内偏科"，不意味着"真实财富风险"。

PR #14（已合入）在 CIO prompt 层加了"零花钱账户 TRIM 约束"，但这是纯 prompt
约束——LLM 可能忽略。本次改用确定性后处理兜底。

## 决策

### 核心原则

当 SOLVENCY_BUFFER_LEVEL=strong（真实财富兜底充足，投资账户归零不影响生存）时：
- "账户内集中度高" **不应触发减仓（TRIM）**，只应 **限制加仓（BUY/ACCUMULATE）**
- 减仓的理由是真实财富风险；兜底充足时真实财富风险不存在
- 但账户太偏单一资产时，不该再往里加

### 实现方式：确定性后处理（不依赖 prompt）

1. **CIO prompt** 加格式要求：TRIM 时必须输出 `TRIM_REASON: concentration | stop_loss | bearish`
2. **`parse_cio_memo()`** 新增 `TRIM_REASON` 提取 + Sanity check 4：
   ```
   if SOLVENCY=strong and verdict=TRIM and TRIM_REASON=concentration:
       → 强制改写 HOLD + 记录 override 原因
   ```
3. **`portfolio_summary_text()`** 加 `backup_cny` 参数，附注真实财富占比
4. 跟现有的 BUY→ACCUMULATE（Sanity 1）、WORKER→HOLD（Sanity 3）同款机制

> **2026-06-17（ADR-017）**：Sanity check 4 现在**双触发**——`solvency_strong` **或**
> `concentration_lens_enabled=False`（用户经 `/api/config` 关掉集中度 lens，单资产/刻意集中
> 策略）时，**无条件** force-HOLD 掉 `TRIM_REASON=concentration`（lens-off 那条不依赖 solvency）。
> 这把"我有家族兜底（emergency_buffer）"和"别因集中度唠叨我减仓"两件事**解耦**：以前只能
> 靠填假的 `emergency_buffer_cny` 触发 strong 来规避，现在有真 toggle。`out["_concentration_lens"]`
> 标记区分两种触发（`disabled` vs `sanity4_solvency`），供 intervention 反事实账本分桶
> （`config_concentration_lens_off` vs `sanity4_solvency_concentration`），别混淆统计。
> prompt 层（`agents/cio.py` / `agents/risk_officer.py`）同步软抑制超配规则。详见
> [017-config-via-api](017-config-via-api.md)。
>
> **2026-06-23（ADR-019）后续**：上面的"双触发"已收敛为**单触发**——solvency 那条删了，
> Sanity 4 现在只在 lens-off 时触发；`_concentration_lens` 只产出 `disabled`，rule 只产出
> `config_concentration_lens_off`（历史 `sanity4_solvency_concentration` 仍由 `rule_family()`
> 并入 `trim_blocked` 桶）。

### 与 PR #14 的关系

PR #14 在 prompt 层加了"零花钱账户 + 浮亏 < 5% 不允许 TRIM"约束。本次的
Sanity check 4 是后处理兜底，两者不冲突：
- PR #14 的约束依赖 LLM 遵守（可能失败）
- Sanity check 4 是确定性的（LLM 输出 TRIM + concentration 就会被覆盖）
- 两者可以共存：PR #14 拦浮亏 < 5% 的 TRIM，Sanity 4 拦 SOLVENCY=strong 的 concentration TRIM

### 不改的部分

- SOLVENCY=weak/unknown 时的逻辑不变（账户可能就是全部身家，集中度该触发减仓）
- stop_loss / bearish 原因的 TRIM 不受影响（这些是真实风险信号）

## 待办：TRIM 路径化（概率表升级）

TRIM 信号当前是纯静态快照决策，缺"卖出后预期路径 / 买回点"：
- CIO 输出 schema 无 TRIM 专属的 re-entry 字段
- `parse_cio_memo()` 不提取 `EXECUTION_PLAN`（全丢弃）
- 回测/复盘只看事后价格方向，不评估"是否在合适价位买回"
- 全库搜索 `buy.?back` / `re.?entry` / `price.?path` 等零结果

现有概率表是 30 天单点分布（`(asset, regime)` → P(涨>5%) / P(跌>5%)），
需升级成 **路径分布**（多时间步联合分布）才能支撑"卖出后 X 天内有 Y% 概率
跌到 Z 价位"的路径化决策。这是大工程，以后做。

## 相关文件

| 文件 | 改动 |
|---|---|
| `utils/portfolio_summary.py` | `backup_cny` 参数 + 真实财富占比附注 |
| `agents/skills/cio/SKILL.md` | `TRIM_REASON` 输出字段 |
| `core/committee.py`（现 `core/committee/cio_parse.py`）| `parse_cio_memo()` TRIM_REASON 提取 + Sanity 4 |
| `core/committee_runner.py` | wealth_view 提前加载 + 传 backup_cny |
| `jobs/daily_report.py` | cron 路径传 backup_cny |
| `scripts/skill_cmds/committee_cmds.py:cmd_run_committee` | skill 路径传 solvency_strong + backup_cny（入口 façade 仍是 `scripts/skill.py`） |
