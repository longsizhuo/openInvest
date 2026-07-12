---
type: adr
title: "ADR-026：决策核心三层纯度契约——calc 计算层 + 输入完备 + 全量留痕"
tags: [calc, purity, functional-core, provenance, import-linter, architecture]
intent: 把散落的纯计算收编成机器强制的计算层；定义 LLM 系统下"可复现"的正确目标
schema_source:
  - src/openinvest/calc/__init__.py
  - pyproject.toml
documents:
  endpoints: []
  config_keys: []
  symbols:
    - run_committee
    - ingest_events
    - compute_regime_return_frame
---

# ADR-026：决策核心三层纯度契约

- 状态：✅ 已采纳（2026-07-13）
- 背景 research：本仓 2026-07-12 "是否应保证部分功能是 pure function" 研究

## 决策

openInvest 的可复现保证定义为**输入完备（input-completeness）+ 全量留痕**，
不是位级可复现。据此把代码分为机器强制的三层：

| 层 | 定义 | 所在 | 强制 |
|---|---|---|---|
| **T0 纯计算** | 同输入→同输出；禁网络/文件/SQLite/`.now()`/LLM；唯一放行依赖 `core.config`（确定性 yaml，视为可注入参数） | 域中立→`openinvest/calc/`（11 模块）；域绑定→原域内登记（daily_report_builder / pnl_render / review_calc / dreaming_calc / cio_parse / debate_calc / intervention_rules / event_format / decision_calc / schemas） | import-linter 第 3 契约 + CI AST 守卫 + smoke imports |
| **T1 输入完备效应层** | 全部上下文由调用方算好传参（禁读 user.md/portfolio.md）；效应仅 LLM 调用 + 落盘（persist 带 `as_of_date` 回测逃生口） | `core/committee/debate.py:run_committee` | import-linter 第 1/2 契约 + SENTINEL 契约测试 |
| **T2 命令式外壳** | 一切变量（价格 IO / 事件 RAG 召回 / 时钟 / 宿主 agent 喂料）在此发生并固化成 brief 字符串后才跨入 T1 | entries + `core/runner/` loaders + 各 IO shell | 分层契约（CLAUDE.md） |

### 为什么不追位级可复现

LLM 温度 0.2 是刻意的（wiki 04：同输入不同模型出不同 verdict = 验证信号非 bug）；
供应商模型升级也会破坏位级复现。追它会把架构推向录制-回放 mock 的复杂度。
正确的可复现单位是"输入 + transcript"——decision ledger / `explain_decision`
已经是这个形状。

### 宿主 agent 信息是变量，但只能走受控入口

agent（Hermes/Codex/OpenClaw/…）的信息获取能力不受控也**不该受控**——差异
只体现为输入质量。进入决策上下文必须过五道闸：`ingest_event` → LLM 归一化 →
severity/symbol 判级 → 幂等入账（同 url/claim 去重）→ RAG 召回 → event_brief
参数进 transcript。溯源：`events.ingested_by`（喂料 agent 身份）+
`sources.src_name`（新闻来源），坏 verdict 可反查坏输入是谁喂的。

## 实施纪律（机器强制 + 约定）

1. **强制**：`uv run lint-imports --no-cache`（第 3 契约：纯模块禁 import 内部
   IO 模块，`allow_indirect_imports=true`——函数级间接边如
   daily_report_builder→portfolio_summary→fx 放行）；CI"计算层纯度 AST 守卫"
   （外部包 yfinance/requests/urllib/sqlite3 + `.now()` 时钟不进 grimp 图，
   AST 兜底；时间是输入不是环境——纯核 `now`/`asof` 必传，IO shell 补时钟）。
2. **façade 纪律**：旧 import 路径永久保留薄壳 re-export；calc 模块 `__all__`
   必须是**完整历史导出面**（含下划线名/常量），否则 `import *` 丢符号。
   monkeypatch 钉实现命名空间（`openinvest.calc.*` / `*_calc.*`），patch façade
   属性打不到——committee 拆包同款 gotcha。
3. **方向纪律**：`calc/regime` ↛ `calc/regime_probability`（反向成环）；
   calc 任何模块 ↛ `utils.exchange_fee`（IO shell）。
4. **禁合并**：`calc/timeframe_analysis._calc_max_drawdown/_calc_volatility`
   与 `calc/market_metrics` 同名系口径不同（Series vs OHLC df），不是重复代码。
5. **同名包装惯例**：IO shell 保留与纯核同名的薄包装（render_svg /
   forward_return / _is_trading_window / _outperform_events / summarize），
   在 shell 命名空间解析 IO 依赖——历史调用方与测试 patch 零改。

## 后续

- event 召回缺 as-of-D 模式：回测今天不消费事件层所以无泄漏，**开始消费前
  必须先做**（否则 ADR-022 的坑换门重进）——见 GitHub issue。
- `pnl_snapshot._outperform_events` 存量 bug（`get_all_series()` 缺参 →
  outperform feed 静默失效）：搬迁时逐字保留未修，另行 issue。
