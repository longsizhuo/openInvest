---
type: adr
title: "ADR-005: daily_report.py 调度/采集/渲染分离"
tags: [daily-report, refactor, pure-functions, architecture, testing]
intent: 架构决策
schema_source:
  - jobs/daily_report_builder.py:classify_asset_freshness
  - jobs/daily_report_builder.py:format_staleness_warning
  - jobs/daily_report_builder.py:assemble_full_report
  - utils/portfolio_summary.py:portfolio_summary_text
documents:
  endpoints: []
  config_keys: []
  symbols:
    - classify_asset_freshness
    - format_staleness_warning
    - assemble_full_report
    - portfolio_summary_text
status: accepted
date: 2026-05-10
supersedes: []
superseded_by: []
---

# ADR-005: daily_report.py 调度/采集/渲染分离

**状态**: 已接受  
**日期**: 2026-05-10  
**作者**: Dev-Core Sprint 2

---

## 背景

`jobs/daily_report.py` 的 `run()` 函数在重构前有约 400 行，混合了四类职责：

1. **价格采集**：`_get_last_close()` 拉 yfinance / DB 行情
2. **熔断判断**：staleness 分类 + 硬熔断逻辑
3. **LLM 调用**：`run_macro_view()` + `run_committee()`
4. **报告渲染**：把委员会结果拼成 markdown + 邮件

这种耦合导致：
- 报告渲染逻辑无法独立单测——任何测试都要 mock LLM 和 yfinance
- `_portfolio_summary()` 和 `_format_staleness()` 是**有价值的纯函数**，却藏在 cron 逻辑里无法复用
- 新加渲染字段（如追踪仓显示逻辑）需要在 400 行函数里定位上下文

## 决策

**不做"一刀切"拆分**，原因：
- daily_report.py 的价格采集和熔断逻辑耦合程度较深（同一个 `asset_freshness` dict 在两处被读写）
- 完全解耦需要引入中间数据结构（"PriceContext" dataclass），工程量等价于重写
- 当前 pain point 集中在"渲染层无法单测"，精准手术更合适

**实际做法：提取纯函数到 `jobs/daily_report_builder.py`**

### 新文件 `jobs/daily_report_builder.py` 职责

| 函数 | 说明 |
|------|------|
| `classify_asset_freshness()` | stateless：(price, age_days) → fresh/stale/very_stale/missing |
| `format_staleness_warning()` | stateless：生成 LLM 上下文的陈旧告警字符串 |
| `portfolio_summary_text()` | 纯函数：给定 pm + prices + 总资产 → 多行字符串 |
| `assemble_full_report()` | 纯函数：给定委员会结果 → 完整 markdown 报告 |

### 保留在 `jobs/daily_report.py` 的职责

- `_get_last_close()`：行情采集（有网络 IO）
- `_gather_relevant_insights()`：文件 IO
- `run()`：cron trigger + 价格采集 + 熔断判断 + LLM 调用 + 邮件发送
- `_classify_freshness()`：内嵌闭包（委托给 builder.classify_asset_freshness）

## 拆分边界设计

```
daily_report.py (IO + 调度)
  └─→ 价格采集: _get_last_close(), get_gold_snapshot()
  └─→ 熔断判断: asset_freshness dict + 硬熔断阈值
  └─→ LLM 调用: run_macro_view(), run_committee()
  └─→ 调用 builder: _portfolio_summary() → portfolio_summary_text()
  └─→ 调用 builder: assemble_full_report()

daily_report_builder.py (纯函数，零 IO)
  └─→ classify_asset_freshness()
  └─→ format_staleness_warning()
  └─→ portfolio_summary_text()
  └─→ assemble_full_report()
```

## 为什么不更彻底拆分

考虑过引入 `DailyReportContext` dataclass 把"采集阶段的中间结果"传给 builder，但：

1. `asset_freshness`、`skipped_assets`、`data_warnings` 三个集合在采集阶段是活的（随 for 循环更新），最终状态是什么只有 `run()` 结束后才知道
2. 把这三个 dict/set 封装进 dataclass 等于引入有状态的中间层，测试时构造 fixture 的成本跟当前差不多
3. 当前 pain point（报告渲染无法单测）已经通过提取纯函数解决；调度层和采集层的解耦留到后续 sprint

## 测试覆盖

新增 `tests/test_daily_report_builder.py`，覆盖：
- `classify_asset_freshness()` 的全部 7 个分支
- `format_staleness_warning()` 的 5 个场景
- `portfolio_summary_text()` 的 7 个场景（含持仓浮盈、追踪仓过滤、应急金扣减）
- `assemble_full_report()` 的 4 个场景（含全跳过占位文字）

总计新增 22 个测试，全部零 mock（不需要 yfinance / LLM）。
