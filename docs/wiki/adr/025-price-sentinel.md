---
type: adr
title: "ADR-025：价格异动哨兵——垂直线先报警后委员会"
tags: [event-layer, price-action, sentinel, alert, fomo]
intent: 纯价格异动的感知与报警时序决策
schema_source:
  - src/openinvest/jobs/price_sentinel.py:_detect_move
documents:
  endpoints:
    - POST /api/committee/run
  config_keys:
    - event.sentinel_enabled
    - event.sentinel_atr_mult
    - event.sentinel_cooldown_min
    - event.sentinel_schedule
  symbols:
    - _detect_move
    - _cooldown_ok
status: accepted
date: "2026-07-03"
supersedes: []
superseded_by: []
---

# ADR-025：价格异动哨兵——垂直线先报警后委员会

**日期**：2026-07-03
**状态**：Accepted（default-on）

## Context

2026-07-02 黄金 10 分钟垂直拉升（ADP 爆冷）无人报警。窗口修正（见 ADR-006
2026-07-03 修注）堵住了"有新闻的大事件"，但暴露了结构性缺口：**event_watch
只对新闻叙事报警**。纯价格事件（闪崩、逼空、无头条的流动性行情）30 分钟新闻
节奏天然看不见；尖峰可能在两次扫描之间涨完回落。

数据现实约束：yfinance 期货/股票分钟线延迟 ~10-15 分钟（实测 GC=F 5m 延迟
14 分钟）。**报警必然晚于行情 15-25 分钟**——本哨兵不是、也不可能是抢跑工具
（对齐 ADR-023 诚实定位）。它的价值是：

1. **系统先于用户开口**：用户看到垂直线之前/之时，邮件里已有"发生了什么 +
   最近 verdict 锚点"——FOMO 拦截从"等用户来问"变成"主动到场"。
2. **下跌侧衔接真金白银的决策**：DCA 分批触发价、快崩防御哨兵都等的是
   "跌到位有人叫醒"。上涨侧只管情绪，下跌侧管钱包。

## Decision

新增 `jobs/price_sentinel.py`（cron 默认 `*/5 0-2,8-23 * * *`，与 event_watch
同窗口、5 分钟一次），复用既有事件管道，不另造报警渠道：

1. **感知（零 LLM）**：yfinance 5m 收盘价 → 10 分钟涨跌幅；日 ATR%（
   `utils.market_metrics.compute_metrics`）归一化。触发条件
   `|move_10m| ≥ sentinel_atr_mult × 日ATR%`（默认 0.8——10 分钟走完日常
   波动的八成即"垂直线"；2026-07-02 事件为 1.5×）。ATR 缺失退绝对兜底 1.0%。
2. **时序契约（用户需求原话："先报给我，再跑 committee"）**：
   合成 `event_type=price_action` 事件入 EventStore → **先发报警邮件**
   （`send_event_alert`，claim 内嵌最近 verdict 锚点 + 现价 + ×ATR）→
   **再** `POST /api/committee/run`。委员会触发失败只记 warning，
   **绝不影响已发出的报警**；邮件失败也不阻塞委员会触发。
3. **防疲劳**：同 symbol 同方向 `sentinel_cooldown_min`（默认 120 分钟）
   静默；急涨后急跌属不同方向各自可报。冷却状态持久在
   `memory/.state/price_sentinel_cooldowns.json`。
4. **闭市保护**：最后一根 5m bar 距今 >25 分钟视为闭市/数据停滞，跳过——
   不拿昨天的尾巴当今天的异动。

## 与 event_watch 的关系

| | event_watch（新闻） | price_sentinel（价格） |
|---|---|---|
| 感官 | RSS/DDGS 拉取 + flash LLM 归一化 | yfinance 5m + 纯算术（零 LLM）|
| 触发条件 | severity≥mid ∧ 命中持仓 ∧ stance≠neutral | ATR 归一化阈值 + 冷却 |
| 报警时序 | 先拿 committee task_id 再发邮件（邮件带链接，不等 verdict）| **先邮件后触发**（报警无条件优先）|
| 事件落盘 | 同一个 EventStore | 同一个 EventStore（`price_action` 类型，无 embedding）|
| 下游 | 邮件 + 委员会重跑 | 同左（复用同两条路径）|

## Consequences

- 空扫零成本（无新事件时不发邮件、不调 LLM、不触发委员会）
- 委员会重跑本身烧 DeepSeek token——由冷却 + ATR 阈值控制频率，
  预期每 symbol 每天 0-2 次（多数日子 0 次）
- `price_action` 事件进 RAG 语料（无 embedding，只参与维度过滤不参与向量召回）
- 阈值/冷却/窗口/开关全部经 config 白名单可调（ADR-017），GUI 自动出现
