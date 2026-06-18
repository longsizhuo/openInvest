---
type: wiki-chapter
title: Web API 参考
tags: [api, rest, fastapi, openapi]
intent: API Contract
schema_source:
  - connectors/web_api/models.py:PortfolioResponse
  - connectors/web_api/models.py:HoldingsListResponse
  - connectors/web_api/models.py:TotalValueResponse
  - connectors/web_api/models.py:ConfigResponse
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

> FastAPI 暴露的 40+ REST 端点 + SSE。同源部署，CF Access 边缘鉴权。
> 这一章是按"读 / 写 / 委员会 / 系统 / 透明化"分组的端点速查 + 前端类型同步流程。

[← 05-data-model](05-data-model.md) · [Wiki 索引](README.md) · [07-extending →](07-extending.md)

---

## 1. 启动

```bash
# 本地开发
uv run uvicorn connectors.web_api:app --host 127.0.0.1 --port 8765

# Swagger 自动生成
open http://127.0.0.1:8765/docs

# OpenAPI schema（前端 gen-types 拉这个）
curl http://127.0.0.1:8765/openapi.json
```

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
| GET | `/api/symbols/search?q=apple&limit=8` | yfinance Search 搜 symbol（GUI 新增资产用）|
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

**可选应用层 token（2026-06，远端模式）**：设 `INVEST_API_TOKEN` 后，
**非 loopback** 来源访问 `/api/*`（`/api/health` 豁免探活）必须带
`Authorization: Bearer <token>`（`secrets.compare_digest` 恒时比较）。
不设 = 行为完全不变，上面的边缘鉴权模型照旧。loopback 豁免保证
Caddy→127.0.0.1 链路和本机 GUI 不受影响；token 用于"绑 0.0.0.0 / 内网直连 /
没有 CF 的局域网 hub"场景。token 永不进日志与响应体。

详见 [08-deployment.md#cloudflare-access](08-deployment.md#cloudflare-access)。

---

## 8. 前端类型同步

### 自动生成

```bash
# 在 invest-gui 仓库
pnpm gen-types
# = openapi-typescript http://127.0.0.1:8765/openapi.json -o src/lib/api-types.ts
```

→ 后端改了 endpoint / Pydantic model → 前端跑 `pnpm gen-types` 一行同步类型。

### 工作流

1. 后端改 endpoint / 改 Pydantic schema
2. 本地起 uvicorn 暴露 :8765
3. invest-gui 仓库跑 `pnpm gen-types`
4. TS 编译报错 = 前端代码该改的字段
5. 改完 commit 类型产物（`src/lib/api-types.ts`）
6. CI 不强制跑 gen-types（避免后端没起时阻塞构建）

### `api-client.ts` 包装

封装了 `fetcher`（给 SWR）+ `postJSON`（给 mutation）+ `ApiError` 类。
所有路由代码用 hook 调用，不直接 `fetch()`。

```typescript
import useSWR from "swr";
import { fetcher, type HoldingsListResponse } from "../lib/api-client";

const { data, error, isLoading } = useSWR<HoldingsListResponse>(
  "/api/holdings",
  fetcher,
  { refreshInterval: 30_000 },
);
```

---

## 9. 错误协议

后端 raise `HTTPException(status_code=N, detail=msg)`。前端 `ApiError`：

```typescript
class ApiError extends Error {
  status: number
  detail: string
}

try {
  await postJSON(...)
} catch (err) {
  setError(err instanceof ApiError ? err.detail : String(err))
}
```

常见错误码：
- `400` 输入校验失败（金额 ≤ 0 / 币种格式错）
- `404` symbol 不存在 / task_id 不存在
- `409` 持仓 symbol 已存在（POST holdings 时）
- `503` 外部依赖失败（IMAP / yfinance）

---

## 10. CORS / 同源

**生产**：完全同源（`/api/*` 和 `/*` 在同一 host）→ 不需要 CORS 头。

**开发**：Vite dev server :5173 调本机 :8765 → 后端用 `INVEST_WEB_DEV_CORS=1` env 放行：

```bash
INVEST_WEB_DEV_CORS=1 uv run uvicorn ...
```

→ 后端会注入 CORS middleware 仅放行 `http://localhost:5173`。

---

## 11. Skill-parity 端点（远端模式 hub-and-spoke）

`scripts/skill.py` 设了 `INVEST_API_BASE` 时，CLI 子命令经
`scripts/remote_dispatch.py` 转发到这些端点。**输出形状与本地 CLI 完全一致**
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

## 下一步

→ [07-extending.md](07-extending.md) — 想加一个新端点该改哪几处

→ [08-deployment.md](08-deployment.md) — 生产部署完整链路

→ [09-troubleshooting.md](09-troubleshooting.md) — endpoint 失败怎么排查
