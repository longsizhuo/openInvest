---
type: reference
title: Memory 目录布局 (OpenClaw-style)
tags: [memory, portfolio, schema, dreaming, concurrency]
intent: 记忆目录布局与数据模型
schema_source:
  - core/memory_store.py:MemoryStore
  - core/portfolio_manager.py:PortfolioManager
documents:
  endpoints:
    - GET /api/committee_sessions
    - GET /api/llm/usage
    - GET /api/committee/live/{task_id}
  config_keys: []
  symbols:
    - MemoryStore
    - PortfolioManager
---

# Memory 目录布局 (OpenClaw-style)

仿 [OpenClaw 2026.4.9](https://github.com/openclaw/openclaw) 的 Markdown + frontmatter 持久化设计。
Memory 整个目录 git-ignored（含个人资产、工资、交易历史），新部署需自己跑迁移脚本生成。

## 目录结构

```
memory/
├── MEMORY.md                  # 索引文件（人类 + agent 都读）
├── DREAMS.md                  # Dreaming 写出的叙事性梦日记（P3 后启用）
├── user.md                    # 用户身份 / 风险偏好 / 月薪月支
├── strategy.md                # 投资策略 / target_assets 数组
├── portfolio.md               # 当前持仓（v2 schema：cash dict + holdings list）
├── portfolio_history.jsonl    # 交易流水 (append-only)
├── llm_usage.jsonl            # LLM 调用 telemetry（v3 透明化）
├── insights/                  # Deep Sleep 通过阈值门的长期洞察
│   └── *.md
├── daily/                     # 每日委员会决议（v3 多资产 = 多文件）
│   └── YYYY-MM-DD/<SYMBOL>.md
├── .committee/                # 委员会异步任务状态（task_id → status.json）
│   └── <task_id>/
├── .runs/                     # （预留）run/session 数据模型
├── .dreams/                   # Dreaming 子系统私有
│   ├── short-term-recall.json # Light Sleep 摄入信号
│   ├── candidates.json        # REM Sleep 候选模式
│   └── events.jsonl           # 三阶段审计日志
└── .state/                    # 简单 KV (已处理邮件 ID 等)
    └── processed_emails.json
```

## portfolio.md 文件格式（v2，2026-05+）

```markdown
---
name: portfolio
type: portfolio
schema_version: 2
updated: 2026-05-06T18:03:15+08:00
cash:
  CNY: 50000.00
  AUD: 1000.00
  USD: 0.00
holdings:
  - symbol: NDQ.AX
    kind: etf
    units: 50
    unit_label: 股
    avg_cost: 38.50
    cost_currency: AUD
    channel: CommSec
    display_name: BetaShares Nasdaq 100 ETF
    proxy_kind: direct
  - symbol: GC=F
    kind: metal
    units: 124.0
    unit_label: 克
    avg_cost: 1008.79
    cost_currency: CNY
    channel: 浙商积存金
    display_name: 伦敦金 (浙商积存金)
    yfinance_proxy: GC=F
    proxy_kind: gold_cny_per_gram
    sell_fee_pct: 0.0038
---

# 当前持仓
- CNY 现金: ¥50,000 / AUD 现金: $1,000
- NDQ.AX: 50 股 @ A$38.50 (CommSec)
- 黄金: 124 g @ ¥1008.79/g (浙商积存金)
```

- **frontmatter**：结构化数据的 source of truth（代码读写）
- **body**：自然语言版本（agent 直接看，每次写入由模板重新渲染）

### v2 schema 核心设计

| 字段 | 类型 | 含义 |
|------|------|------|
| `cash` | `dict[str, float]` | 多币种现金（CNY / AUD / USD / ...）|
| `holdings` | `list[dict]` | 任意 yfinance symbol 的持仓 |
| `holdings[].symbol` | str | yfinance ticker（NDQ.AX / GC=F / AAPL）|
| `holdings[].kind` | str | etf / stock / metal / crypto / bond / fund / other |
| `holdings[].units` | float | 持仓数量（unit_label 单位）|
| `holdings[].unit_label` | str | 股 / 克 / oz / 个 |
| `holdings[].avg_cost` | float | 加权均价 |
| `holdings[].cost_currency` | str | 均价币种 |
| `holdings[].channel` | str | 渠道（CommSec / 浙商积存金）|
| `holdings[].yfinance_proxy` | str? | 行情代理（如黄金用 GC=F + USDCNY 反推）|
| `holdings[].is_tracking_only` | bool? | 仅追踪不计 PnL |

### v1 → v2 迁移

旧 v1 schema（`cash_cny` / `aud_cash` / `ndq_shares` / `gold_grams` 扁平字段）依然能跑：
`PortfolioManager` 在 read-time 自动 fallback，写入时清理旧字段并标记 `schema_version: 2`。

## 类型分类

| type | 含义 | 更新频率 | 例子 |
|------|------|---------|------|
| `user` | 用户身份与偏好 | 几乎不变 | `user.md` |
| `strategy` | 投资策略配置 | 偶尔（NapCat 命令调） | `strategy.md` |
| `portfolio` | 当前持仓 | 高频（每次交易后） | `portfolio.md` |
| `log` | 日志 | append-only | `daily/<date>/<symbol>.md`, `*.jsonl` |
| `insight` | 长期洞察 | Deep Sleep 写入 | `insights/*.md` |
| `committee` | 委员会任务状态 | 异步任务期间高频 | `.committee/<task_id>/status.json` |

## 初始化

```bash
# 从旧 user_profile.json 迁移（兼容 v0.1 用户）
python scripts/migrate_profile.py

# v1 portfolio.md → v2 (cash dict + holdings list)
python scripts/migrate_portfolio_to_holdings.py

# 升级单资产 → 多资产
python scripts/upgrade_to_multi_asset.py

# 导入实际黄金交易历史（按需，给原作者用的）
python scripts/import_gold_trades.py

# 手动导入 CommSec 邮件成交（替代旧 cron 自动模式，2026-05+）
python scripts/import_commsec.py --lookback 30 --apply
```

## 并发安全

`core.memory_store.MemoryStore` 用 `fcntl.LOCK_EX` 文件锁保证：
- 同进程多线程（agent ThreadPool）安全
- 跨进程（scheduler runner + napcat_bot 同时跑）也安全

`PortfolioManager.with_portfolio_tx()` 在锁内提供 RMW（read-modify-write）闭包：
所有写操作（deposit / gold_buy / gold_sell / record_external_trade）必须走它，
异常时整个 tx 回滚不落盘。

## Dreaming 整合

`jobs/dreaming.py` 每天 03:00 跑三阶段（实际实现见 `jobs/dreaming.py`）：

1. **Light Sleep** — 读 `memory/.dreams/verdict_review.jsonl`（委员会 verdict + 事后行情，由 `jobs/verdict_review.py` 产），每条补决议日 regime → `.dreams/short-term-recall.json`。**2026-05-26 换源**：从学"用户成交"改成学"委员会自己的 verdict vs 实际盘"（交易量小学不到东西）
2. **REM Sleep** — 按 `(asset, verdict, regime)` 聚合事后命中率；HOLD 用波动率感知阈值 + 机会成本方向区分（踏空 vs 躲跌）→ `.dreams/candidates.json`
3. **Deep Sleep** — 阈值门 `score≥0.8 / count≥3` 通过的 → 写 `insights/*.md` + 更新 `MEMORY.md` 索引

> 完整设计见 [docs/wiki/03-dreaming.md](wiki/03-dreaming.md)。改源/阈值/窗口请同步更新该文档，避免脱节。

详见 [OpenClaw Dreaming Guide](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e)。

## v3 透明化产物（2026-05）

每次委员会跑会落盘到：

| 文件 | 内容 | 端点 |
|------|------|------|
| `daily/<date>/<symbol>.md` | 完整 4 角色 transcript + CIO verdict | `/api/committee_sessions` |
| `llm_usage.jsonl` | 每次 LLM 调用的 token / latency / cost | `/api/llm/usage` |
| `.committee/<task_id>/tool_calls.jsonl` | agent 调 tool 的 audit trail | `/api/agents/run/{run_id}/tool_calls` |
| `.committee/<task_id>/status.json` | 异步任务 progress（SSE 推送源）| `/api/committee/live/{task_id}` |
