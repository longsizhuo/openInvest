---
type: wiki-chapter
title: Web API 参考
tags: [api, rest, fastapi, openapi]
intent: API Contract
schema_source:
  - src/openinvest/connectors/web_api/models.py:PortfolioResponse
  - src/openinvest/connectors/web_api/models.py:HoldingsListResponse
  - src/openinvest/connectors/web_api/models.py:TotalValueResponse
  - src/openinvest/connectors/web_api/models.py:ConfigResponse
documents:
  endpoints:
    - GET /api/portfolio
    - GET /api/holdings
    - GET /api/portfolio/total_value
    - GET /api/cash
    - GET /api/gold
    - GET /api/ndq
    - GET /api/config
    - PUT /api/config
  config_keys: []
  symbols: [PortfolioManager]
---

# Web API 参考

> ⚠️ **2026-07-05 起 Web API 已 deprecated**：存量端点只服务 remote hub 模式
> （`INVEST_API_BASE` 转发）与内部触发，**不再新增端点**；GUI 壳层已退役，后端
> 不再 serve 静态文件。新功能一律走 CLI（`openinvest`）/ MCP（`openinvest-mcp`）。
> 待 MCP 覆盖 remote 场景后本 API 退役。
>
> FastAPI 暴露的 40+ REST 端点 + SSE。CF Access 边缘鉴权。
> 这一章是按"读 / 写 / 委员会 / 系统 / 透明化"分组的端点速查。

[← 05-data-model](05-data-model.md) · [Wiki 索引](README.md) · [07-extending →](07-extending.md)

---

## 1. 启动

```bash
# console script（hub 部署用；host/port 走 INVEST_WEB_HOST / INVEST_WEB_PORT env）
openinvest-web

# Swagger 自动生成
open http://127.0.0.1:8765/docs

# OpenAPI schema
curl http://127.0.0.1:8765/openapi.json
```

（开发仓形态 `uvicorn openinvest.connectors.web_api:app` 也可；旧
`uvicorn connectors.web_api:app` 是兼容 shim，文档不再教。）

生产部署：见 [08-deployment.md](08-deployment.md)。

---

## 2. 读端点（GET）

### 持仓 / 现金

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/portfolio` | 简化持仓快照（兼容老前端）|
| GET | `/api/holdings` | 完整 holdings 列表 + cash dict（v2 主接口）|
| GET | `/api/portfolio/total_value?base=CNY` | 多币种折算总市值 |
| GET | `/api/cash` | 仅返回 cash dict |
| GET | `/api/gold` | 黄金持仓 + 实时价 + 浮盈（兼容旧接口）|
| GET | `/api/ndq` | NDQ.AX 持仓 + 实时行情（兼容旧接口）|

`/api/holdings` 响应示例：
```json
{
  "cash": {"CNY": 50000, "AUD": 1000},
  "holdings": [
    {
      "symbol": "NDQ.AX",
      "kind": "etf",
      "units": 50,
      "unit_label": "股",
      "avg_cost": 38.50,
      "cost_currency": "AUD",
      "channel": "CommSec",
      "display_name": "BetaShares Nasdaq 100 ETF",
      "quote": {
        "price": 42.30,
        "currency": "AUD",
        "is_stale": false,
        "extra": {"day_change_pct": 1.2}
      },
      "market_value": 2115.00,
      "pnl": 190.00,
      "is_tracking_only": false
    }
  ]
}
```

### 历史 / 流水

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/history?limit=200&symbol=NDQ.AX` | 交易流水 |
| GET | `/api/daily?since=N` | 最近 N 天每日决策快照 |
| GET | `/api/pnl_history?since=N` | PnL 时序点（jobs/pnl_snapshot 写）|
| GET | `/api/pnl_chart.svg` | 后端自家 SVG（无 JS 也能看）|

### 策略 / 配置

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/strategy` | 完整策略 + target_assets |
| GET | `/api/config` | 可经 API 配置的白名单 tunable 当前生效值 + 是否被 override + 元信息（ADR-017）|
| GET | `/api/symbols/search?q=apple&limit=8` | yfinance Search 搜 symbol（原 GUI 新增资产用，存量保留）|
| GET | `/api/regime/{symbol}` | 该 symbol 当前 regime + 算法输入 |
| GET | `/api/regime_rules` | 全部硬规则 + 4 角色 prompt 全文 |

---

## 3. 写端点（POST/PUT/DELETE）

### 现金

| Method | Path | Body |
|--------|------|------|
| POST | `/api/cash/{currency}/deposit` | `{amount, source?}` |
| POST | `/api/cash/{currency}/withdraw` | `{amount, source?}` |

```bash
curl -X POST http://127.0.0.1:8765/api/cash/CNY/deposit \
  -H "Content-Type: application/json" \
  -d '{"amount": 1000, "source": "工资"}'
```

### Holdings CRUD（v2 通用）

| Method | Path | 用途 |
|--------|------|------|
| POST | `/api/holdings` | 新增持仓（任意 yfinance symbol）|
| PUT | `/api/holdings/{symbol}` | 部分字段更新 |
| DELETE | `/api/holdings/{symbol}` | 删除（units > 0 时拒绝）|

```bash
# 加 AAPL 追踪仓
curl -X POST http://127.0.0.1:8765/api/holdings -d '{
  "symbol": "AAPL",
  "kind": "stock",
  "units": 0,
  "unit_label": "股",
  "avg_cost": 0,
  "cost_currency": "USD",
  "channel": "Robinhood",
  "is_tracking_only": true
}'
```

### 智能持仓导入（自由文本 / CSV）

| Method | Path | 用途 |
|--------|------|------|
| POST | `/api/holdings/import` | `{content, commit}` — 自由文本/CSV → 后端 LLM 解析成结构化持仓 |

`commit:false` 只解析返回预览（`parsed.{cash,holdings}`），`commit:true` **非破坏写入**：
只新增 portfolio 里还没有的 symbol、cash 只填当前为 0 的币种，已存在的跳过
（`summary.{added_holdings,skipped_holdings,cash_set,cash_skipped}`）。无 LLM key → 400。
单一可信源 `services/holdings_import.py`，CLI `openinvest import`、onboarding 共用（原 GUI「导入持仓」已随 GUI 退役）。

```bash
curl -X POST http://127.0.0.1:8765/api/holdings/import \
  -d '{"content": "510300 ETF 3000股 成本4.2元（支付宝）\n余额宝 5万", "commit": false}'
```

### 旧的专用端点（兼容保留）

```
POST /api/deposit     POST /api/withdraw
POST /api/gold/buy    POST /api/gold/sell    POST /api/gold/set    POST /api/gold/offset
```

新代码请用 holdings 通用接口，专用接口仅给老前端兼容。

### 策略写

| Method | Path | 用途 |
|--------|------|------|
| PUT | `/api/strategy/allocations` | 改 stock/cash 目标比例 |
| POST | `/api/strategy/asset` | 加 target_asset |
| PUT | `/api/strategy/asset/{symbol}` | 改 target_asset cap / 费率 |
| DELETE | `/api/strategy/asset/{symbol}` | 删 target_asset |
| PUT | `/api/config` | 设一条白名单 config override（body `{key, value}`，落盘持久，优先级 > env；ADR-017）|
| DELETE | `/api/config/{key}` | 删一条 config override，回退 env/yaml/默认 |

> **config-via-API（ADR-017）**：白名单 `API_SETTABLE`（`core/config/_loader.py`）只放用户安全
> 的行为开关（`verdict.concentration_lens_enabled` / `verdict.risk_profile` /
> `verdict.gold_defense_dca_enabled` / `dreaming.llm_verify_enabled`）。落盘
> `memory/.state/config_overrides.json`，`load_config()` 在 env 之上合入，web/cron/skill 三进程共读。
> 机密 + 部署引导仍只走 env；`locked.py` 永不暴露。CLI 同款：`skill config [--set K V] [--clear K]`。

---

## 4. 委员会

### 触发 + 状态

| Method | Path | 用途 |
|--------|------|------|
| POST | `/api/committee/run` | 异步触发，立即返回 task_id（done 后 `result.by_asset[sym]` 含 verdict + **cio_memo**）|
| GET | `/api/committee/{task_id}` | 单次状态快照 |
| GET | `/api/committee/live/{task_id}` | **SSE 直播**（25s keepalive 防 CF 5min idle 超时）|
| GET | `/api/committee_sessions?limit=N` | 历史决议归档（按时间倒序）|
| GET | `/api/committee_sessions/{date}/{symbol}` | 单条决议完整 markdown |
| POST | `/api/committee/prepare` | Coordinator 路径 RPC：返回自包含 brief（6 段 prompt 内联），远端客户端 spawn subagent 用。body `{symbol}` |
| POST | `/api/committee/save` | Coordinator 路径 RPC：transcript 解析 + 防御降级后处理 + 落盘。body `{symbol, transcript}` |

```bash
# 触发
curl -X POST http://127.0.0.1:8765/api/committee/run \
  -d '{"symbols": ["NDQ.AX"], "max_debate_rounds": 4}'
# → {"task_id": "abc123...", "status": "queued", "poll_url": "..."}

# SSE 直播
curl -N http://127.0.0.1:8765/api/committee/live/abc123
# event: progress
# data: {"phase": "round_1_done", "round": 1, ...}
# event: progress
# data: {"phase": "round_2_done", ...}
# event: done
# data: {"result": {...}}
```

POST body schema：
```typescript
{
  symbols?: string[]  // 不传 = 跑 strategy.target_assets 全部
  max_debate_rounds?: number  // 默认 4，1-8
  note?: string
}
```

详见 [02-agents.md](02-agents.md) 看每个 phase 含义。

---

## 5. 透明化端点（v3）

### LLM Telemetry

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/llm/usage?since=N` | 历史 LLM 调用列表（token/latency/cost）|
| GET | `/api/llm/summary?period=7d` | 周期聚合（总 token / 总成本 / 角色分布）|

### Tool Audit Trail

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/agents/run/{run_id}/tool_calls` | 一次 run 里所有 tool 调用 + 入参 / 出参 / 耗时 |

### Verdict 命中率

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/verdict_review/summary` | 1d / 7d / 30d 命中率 × verdict 类型 |
| GET | `/api/verdict_review/data` | 原始数据点 |
| GET | `/api/verdict_review/report` | docs/verdict_accuracy.md 完整 markdown |

### 纪律台账（ADR-023）

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/discipline` | 委员会纪律量化：不作为率 + 拦冲动次数 + 反事实损益 |

返回 `{summary:{inaction:{total_verdicts,by_verdict,hold,hold_rate}, interventions:{total,by_family,...}}, markdown}`。
诚实定位（不吹 alpha，量化「少做错事」），见 [adr/023](adr/023-honest-positioning-not-alpha.md)。CLI `openinvest discipline` / MCP `discipline` 消费（原 GUI「纪律」页已退役）。

### Decision Accounting（issue #133 Decision 9）

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/decisions?days=90` | 统一决策视图：决议↔规则干预↔用户执行↔事后结果 读时 join + 采纳率汇总 |
| POST | `/api/decisions/execution` | 宿主 Agent 回写执行/拒绝+原因（`executions.jsonl` 追加账本，幂等 ADR-016）|

`decision_id = "<date>/<symbol>"`（committee md 天然主键）；`trades.db` 现有 `verdict_id`
列填同一格式即完成硬关联；无显式关联时按「决议日起 7 天内同标的同向成交」自动匹配。
数据源全是既有账本（committee md / interventions / verdict_review / trades.db / executions），
不物化新视图文件。CLI 等价：`decisions` / `record_execution`。

### 数据源健康

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/data_sources/health` | yfinance / DB / commsec 全部数据源最后成功时间 + is_stale |

### CommSec 手动导入（替代旧 cron）

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/commsec/preview?lookback_days=180` | 预览邮件拉到的成交（不写）|
| POST | `/api/commsec/apply` | 用户确认后写入 |

详见 [05-data-model.md](05-data-model.md) 关于为什么改手动模式。

---

## 6. 系统 / 内部状态

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/health` | 容器健康检查 |
| GET | `/api/jobs/status` | 全部 cron job 列表 + 下次时间 + enabled |
| GET | `/api/insights` | Dreaming 长期 insights |
| GET | `/api/dreams/state` | Dreaming 短期记忆状态 |
| GET | `/api/dreams/buckets` | regime bucket 分组命中率（v3）|

---

## 7. 鉴权模型

```
浏览器 → CF Access (验证邮箱) → CF proxy → Caddy → 后端 127.0.0.1:8765
```

**后端不做 auth**：
- 后端只绑 127.0.0.1，公网扫不到
- Caddy 只反代 /api/* 到 8765
- CF Access 在边缘验证 JWT（仅授权邮箱通过）
- 路径都可信 → 后端不重复验证

→ 加 JWT 校验是过度工程，但**前提是没人能直连源站 IP**。
→ 加固方案：Caddy 加 `@cloudflare` matcher 仅放行 CF IP 段（未来）。

**可选应用层 token（2026-06 引入，2026-07-05 #106 收紧）**：设
`INVEST_API_TOKEN` 后，**所有来源**（含 loopback）访问 `/api/*`
（`/api/health` 豁免探活）必须带 `Authorization: Bearer <token>`
（`secrets.compare_digest` 恒时比较）。不设 = 行为完全不变。

> 原 loopback 豁免已删：典型 Caddy/Nginx 反代下连接源恒为 127.0.0.1，
> 外网请求会被静默免密——token 形同虚设。现语义 = 设了 token 就全域当真；
> 本机 curl 自己带 `-H "Authorization: Bearer $INVEST_API_TOKEN"`，
> event_watch 内部触发从同一 .env 自动附带。token 永不进日志与响应体。

详见 [08-deployment.md#cloudflare-access](08-deployment.md#cloudflare-access)。

---

## 8. ~~前端类型同步~~（已随 GUI 退役 2026-07-05）

invest-gui 已封存，`pnpm gen-types` 工作流不再适用。存量端点的 schema 变更只做
bug fix 级修正；hub 客户端（CLI remote dispatch）靠 `services/skill_views.py`
共享形状，无需类型生成。前端重做时走独立仓库直连 MCP，不再消费 OpenAPI。

---

## 9. 错误协议

后端 raise `HTTPException(status_code=N, detail=msg)`。

常见错误码：
- `400` 输入校验失败（金额 ≤ 0 / 币种格式错）
- `404` symbol 不存在 / task_id 不存在
- `409` 持仓 symbol 已存在（POST holdings 时）
- `503` 外部依赖失败（IMAP / yfinance）

---

## 10. CORS / 同源

**生产**：API 只被 hub 客户端 / Caddy 反代消费 → 不需要 CORS 头。

**遗留开关**：`INVEST_WEB_DEV_CORS=1` 会注入 CORS middleware 放行
`http://localhost:5173`（原 Vite dev server 用；GUI 退役后仅历史遗留，别开）。

---

## 11. Skill-parity 端点（远端模式 hub-and-spoke）

CLI（`openinvest`）设了 `INVEST_API_BASE` 时，子命令经
`openinvest/remote_dispatch.py` 转发到这些端点。**输出形状与本地 CLI 完全一致**
（共享 `services/skill_views.py` / `PortfolioManager` 方法），所以 agent 协议
（SKILL.md）零感知。

| Method | Path | CLI 等价 | 说明 |
|--------|------|----------|------|
| GET | `/api/doctor` | `doctor` | hub 视角健康自检（memory/.env/LLM 可达性）|
| GET | `/api/skill/status` | `status` | 现金 + holdings + 实时价 + 总资产 |
| GET | `/api/skill/strategy` | `strategy` | strategy + Dreaming insights |
| GET | `/api/skill/history?n=` | `history -n` | trades + debates（`/api/history` 只有 trades）|
| POST | `/api/skill/what_if` | `what_if` | 情景模拟，body 字段与 CLI 参数一一对应 |
| POST | `/api/skill/buy` / `/api/skill/sell` | `buy` / `sell` | 写持仓走 `with_portfolio_tx`，history `source: skill_remote` |
| POST | `/api/skill/deposit` / `/api/skill/withdraw` | `deposit` / `withdraw` | CLI 同款输出（`/api/cash/*` 是 WriteResponse 形状，对不上 CLI 故另设）|
| POST | `/api/skill/delete_holding` | `delete_holding` | 支持 `force`（`DELETE /api/holdings` 无此语义）|

错误语义对齐 CLI：域内错误（如 what_if symbol 不在持仓）返回 **200 +
`{"status": "error", ...}`**（客户端原样打印）；参数非法 400、memory 未初始化
503、token 错 401——remote dispatch 端把它们映射回 CLI 同款 error JSON + exit 1。

部署拓扑见 [08-deployment.md](08-deployment.md) 的 hub-and-spoke 章节。

---

## MCP adapter（stdio，issue #133 Phase 3）

REST 之外的第三个 adapter（CLI / REST / MCP 同吃 service 层，零业务逻辑）：

```bash
claude mcp add openinvest -e INVEST_HOME=<数据目录> -- uvx openinvest-mcp
```

- **transport 两种**：
  - **stdio（默认）**：MCP client 按 session spawn 子进程，无端口无 daemon
  - **streamable-HTTP（`openinvest-mcp --http`，2026-07）**：remote MCP，hub 常驻
    127.0.0.1:8766（`INVEST_MCP_HOST/PORT`），spoke agent 直连 `/mcp`；鉴权复用
    `INVEST_API_TOKEN`（bearer，`/health` 豁免），stateless + json_response。
    能力差集注记：Coordinator 协议（prepare/save_committee）与 doctor/event_check
    仍只在 REST/CLI（Decision 5/6 刻意不进 MCP），详见 wiki 08 §9
  写操作与 CLI/REST 并存安全（`with_portfolio_tx` fcntl 锁同一模型）
- **18 个工具**（封闭集合，快照测试 `tests/test_mcp_server.py` 守）：status /
  strategy / history / live_prices / what_if / discipline / decisions /
  explain_decision / record_execution / ingest_event / buy / sell / deposit /
  withdraw / set_allocations / track_asset / untrack_asset /
  run_committee（Direct 路径，当天已跑读缓存）
- 刻意不把 81 个 REST 端点全暴露（撑爆 agent context）；Coordinator 委员会
  workflow 也不在这里——那是 Skill 的职责（issue #133 Decision 5/6）

## 下一步

→ [07-extending.md](07-extending.md) — 想加一个新端点该改哪几处

→ [08-deployment.md](08-deployment.md) — 生产部署完整链路

→ [09-troubleshooting.md](09-troubleshooting.md) — endpoint 失败怎么排查
