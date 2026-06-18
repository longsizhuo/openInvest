---
type: adr
title: "ADR-006：事件感知层（第一层）+ 轻量新闻 RAG"
tags: [event-layer, rag, news, committee, feature-flag]
intent: 事件驱动架构决策
schema_source:
  - db/event_store.py:EventStore
documents:
  endpoints:
    - POST /api/committee/run
  config_keys:
    - INVEST_EVENT_RAG_ENABLED
  symbols:
    - _resolve_event_brief
    - resolve_event_brief_multi
    - format_event_brief
status: accepted
date: "2026-05-13"
supersedes: []
superseded_by: []
---

# ADR-006：事件感知层（第一层）+ 轻量新闻 RAG

**日期**：2026-05-13（v1 默认关）；**2026-05-15 修订为默认开**
**状态**：Accepted（**default-on 自 2026-05-15**，见末尾 Default-on 决策段）

## Context

openInvest 在本次提交前是**纯定时驱动**：`daily_report` 10:00、`weekly_review` 周日，没有事件驱动入口。盘中突发事件相关时，verdict 滞后到次日，而且基于的是事件**前**的行情。

同时验证发现：`services/news.py` 是孤儿模块（0 caller），`agents/tools.py` 的 5 个 tool 不含 news，`jobs/verdict_review.py:376` 自己写"新闻/宏观叙事不在 tool 里…Macro Strategist 仅靠数值指标"。也就是说 **Macro 一直都没在看新闻** —— "加新闻"是新增能力，不是优化旧管线。

## Decision

加一个事件层，两条腿共享同一个 sqlite event store：

1. **Trigger 路径**（盘中实时）—— `jobs/event_watch.py` cron `*/30`：
   - 多源拉新闻（DDGS news / RSS / yfinance.Ticker.news）
   - flash LLM 归一化为结构化事件（一次批调用）
   - 维度命中（severity ≥ mid + 影响到 holdings/target_assets + stance ≠ neutral）→ 发 digest 邮件 + POST `/api/committee/run`
2. **RAG 路径**（committee 召回）—— `core/committee_runner.py` 准备 macro 前：
   - 维度 hard filter (entity ∩ time window ∩ severity) → 向量精排 → 时效冲突标注
   - 结构化 brief **只注入 Macro 的 prompt**（事件 RAG 严格隔离）
   - 由 env `INVEST_EVENT_RAG_ENABLED` 总开关控制（**2026-05-15 起 default-on**，
     未设/空值 = 当 true；显式 false/0/off 才关）

## 关键设计差异（vs 通用 RAG）

通用 RAG 假设语料静态、独立、语义重复度低。新闻反过来 —— 时效是第一维，多源报道是噪音，事件 > 文档。我们：

- **入库时不 embed 原文**，先 flash 抽事件结构 + claim 文本，**对 claim 哈希做 event_id**，同一事件多源 merge 到 sources 子表
- 检索：维度过滤先于向量精排（向量只是 marginal 精排）
- 召回结果显式标注 `supersedes` —— 同实体新事件覆盖旧事件

## Embedding 提供方

DeepSeek **没有**公开 embedding API（plan 早期一稿写错）。当前实现：
- 默认：deterministic hash 1024 维（够做粗去重；硬过滤负责重活）
- 升级路径：`INVEST_EMBEDDING_PROVIDER=openai` + `OPENAI_API_KEY` → text-embedding-3-small（1024 维截断）

## 跨 agent 隔离

| Role | 看事件吗 |
|------|---------|
| Macro Strategist | ✅ 看 event_brief（结构化、含 supersedes） |
| Quant Analyst | ❌ 只看技术面 |
| Risk Officer | ❌ 只看 user wealth_context |
| CIO | ❌ 只看 Macro 综合后的 KEY_TAILWIND / KEY_HEADWIND |

理由：Quant/Risk 的输入应该确定性可复现；新闻情绪一旦渗透就破坏 cross-check。Macro 看新闻、CIO 看 Macro 的总结 —— 这一层抽象本身就是过滤器。

## 落地顺序

见 PR / `feat/event-layer` 分支。

## 验证

事件 RAG 是否值得全量启用，由 `scripts/analyze_news_attribution.py` 跑出来的根因占比决定：

- `news_blindspot ≥ 30%` → 开 `INVEST_EVENT_RAG_ENABLED=true`
- `10% ~ 30%` → 只开 trigger 路径，RAG 暂留
- `< 10%` → 瓶颈不在新闻

Trigger 路径不依赖根因结论（任何场景都有价值），无条件可启用。

## 公开数据红线

事件层不动 `docs/accuracy_summary.json` / pnl-data 分支 / outperform feed 任何公开 URL。
邮件是用户私人收件箱，**不是公开数据**，所以邮件正文可以含 symbol / verdict 数值 / 持仓 P&L 等。CLAUDE.md 那条红线只约束公开 feed。

---

## Default-on 决策（2026-05-15 修订）

### Context
作者本人实测：4 步流程（dry-run / 跑 2 周看根因 / 改 .env / 改 yml）记不住。
新功能藏在 feature flag 后 + 默认关 = 没人用 = ROI 为 0。

### Decision
默认开 RAG + event_watch：
- `INVEST_EVENT_RAG_ENABLED` 未设 / 空值 → 视为 true（之前视为 false）
- `jobs/event_watch.yml: enabled: true`（之前是 false）
- `.env.example` 明确写出 `INVEST_EVENT_RAG_ENABLED=true` + 注释如何关

### Safety
fork 用户没 `DEEPSEEK_API_KEY` 时：
- `_resolve_event_brief` 任何异常 graceful 退化空字符串（已实现），委员会照常跑
- `event_watch` 在 normalize 阶段 LLM 失败 → scheduler 记 job_runs failed，
  不影响其他 jobs；用户看到 failed log 可自行关闭 yml

### 想关掉怎么做（fork 用户）
1. `.env` 加 `INVEST_EVENT_RAG_ENABLED=false` —— RAG 注入关
2. `jobs/event_watch.yml` 改 `enabled: false` —— 后台监控关

