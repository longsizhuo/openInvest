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

---

## 4. 委员会

### 触发 + 状态

| Method | Path | 用途 |
|--------|------|------|
| POST | `/api/committee/run` | 异步触发，立即返回 task_id |
| GET | `/api/committee/{task_id}` | 单次状态快照 |
| GET | `/api/committee/live/{task_id}` | **SSE 直播**（25s keepalive 防 CF 5min idle 超时）|
| GET | `/api/committee_sessions?limit=N` | 历史决议归档（按时间倒序）|
| GET | `/api/committee_sessions/{date}/{symbol}` | 单条决议完整 markdown |

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

## 下一步

→ [07-extending.md](07-extending.md) — 想加一个新端点该改哪几处

→ [08-deployment.md](08-deployment.md) — 生产部署完整链路

→ [09-troubleshooting.md](09-troubleshooting.md) — endpoint 失败怎么排查
