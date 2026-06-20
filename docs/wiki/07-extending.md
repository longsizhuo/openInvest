---
type: wiki-chapter
title: 扩展指南（cookbook）
tags: [extending, cookbook, architecture, development]
intent: 扩展开发指引
documents:
  endpoints:
    - POST /api/holdings
    - GET /api/data_sources/health
    - GET /api/regime_rules
  config_keys: []
  symbols:
    - get_quote
    - run_committee
    - with_portfolio_tx
    - RegimeRulesResponse
---

# 扩展指南（cookbook）

> 「我想加 X」该改哪几个文件——按真实场景分。
> 每条都给具体路径 + 改动范围估计。

[← 06-api](06-api.md) · [Wiki 索引](README.md) · [08-deployment →](08-deployment.md)

---

## 目录

1. [加新资产](#1-加新资产)
2. [加新数据源](#2-加新数据源)
3. [加新 agent 角色](#3-加新-agent-角色)
4. [加新 connector（如 Telegram bot）](#4-加新-connector)
5. [加新 cron job](#5-加新-cron-job)
6. [加新 Web 端点](#6-加新-web-端点)
7. [加新 GUI 路由 / tab](#7-加新-gui-路由--tab)

---

## 1. 加新资产

### 场景 A：用户想追踪一只股（GUI 操作，不改代码）

直接 GUI Dashboard 「新增资产」按钮 → 输入 symbol → 选追踪仓 → 提交。
后端 `/api/holdings` POST → portfolio.md `holdings` list 自动追加。

→ **0 代码改动**，任意 yfinance symbol 都行。

### 场景 B：加一类全新资产（如基金 `005827.SS`）

如果它在 yfinance 上 → 同场景 A，0 改动。

如果它**不在 yfinance**（如银行理财、私募）→ 走「加新数据源」（下一节）。

### 场景 C：加一个新的"行情代理"逻辑

比如黄金现在用 `proxy_kind: gold_cny_per_gram`（GC=F + USDCNY 反推克价）。
要加一个新的 proxy（如 `silver_cny_per_gram`）：

1. **改 `utils/quotes.py`**：在 `get_quote()` 里加一个 case
   ```python
   if h.get("proxy_kind") == "silver_cny_per_gram":
       return _silver_quote(h)  # 新写一个反推函数
   ```
2. **写 `utils/silver_price.py`**：仿 `utils/gold_price.py` 模式
3. **改 `core/portfolio_manager.py:HOLDING_TEMPLATES`**：加一个 silver 模板
4. **可选**：改 GUI HoldingDialog 启发式 kind 判断

工时：~1 小时。

---

## 2. 加新数据源

例：接天天基金 API 拉公募基金净值（yfinance 没有）。

### 改动清单

1. **新建 `services/eastmoney_fund.py`**
   ```python
   def fetch_fund_nav(code: str) -> Optional[float]:
       url = f"http://fundgz.1234567.com.cn/js/{code}.js"
       # ... 拉 + 解析 ...
       return nav
   ```

2. **改 `utils/quotes.py:get_quote()` 加路由**
   ```python
   if h.get("proxy_kind") == "eastmoney_fund":
       nav = fetch_fund_nav(h["symbol"])
       return Quote(price=nav, currency="CNY", ...)
   ```

3. **改 `connectors/web_api/routers/observability.py:get_data_sources_health()`**
   注册新数据源进健康面板（让用户能看到"天天基金 API 上次拉到几点"）

4. **测试 `tests/test_eastmoney_fund.py`**
   ```python
   def test_fetch_fund_nav_real_code():
       nav = fetch_fund_nav("005827")
       assert nav > 0
   ```

5. **更新 `docs/wiki/05-data-model.md` 的 proxy_kind 列表**

工时：~2-3 小时（含测试）。

---

## 3. 加新 agent 角色

例：加一个 "ESG Analyst"，看可持续投资指标。

### 改动清单

1. **新建 `agents/esg_analyst.py`**
   ```python
   PROMPT_ESG_ANALYST = """
   你是 ESG 分析师...
   输出格式：
   ESG_SCORE: 0-100
   ESG_FLAGS: [list of red flags]
   """

   def build_esg_prompt(asset, mode="opening"):
       # mode: "opening" or "rebuttal"
       ...
   ```

2. **改 `core/committee/debate.py:run_committee`** 注册到 Round 1 + Round 2..N 并行循环
   ```python
   esg_agent_r1 = _create_agent(
       build_esg_prompt(asset, "opening"),
       role="esg", asset=sym, round_label="opening",
   )
   quant_r1, risk_r1, esg_r1 = _parallel_ask([
       (quant_agent_r1, quant_input_r1),
       (risk_agent_r1, risk_input_r1),
       (esg_agent_r1, esg_input_r1),
   ])
   ```

3. **改 CIO prompt** 让它综合 ESG transcript

4. **改 `connectors/web_api/routers/regime.py:get_regime_rules()` + `connectors/web_api/models.py:RegimeRulesResponse`** 注册新角色到 `/api/regime_rules`

5. **改 GUI `routes/committee/AgentsTab.tsx`** 让"4 角色 + 规则" tab 改名"5 角色"

6. **更新 [02-agents.md](02-agents.md)** 角色矩阵

7. **同步 Coordinator 路径**：`skills/invest/scripts/run.sh prepare_committee` 也要 spawn 第 5 个 subagent
   → 这是双路径的成本，详见 [04-execution-paths.md](04-execution-paths.md)
   （Direct 路径 `run_committee` 复用 `core/committee/`，第 4 步生效后自动跟上，不用单独改）

工时：~半天（含两条路径同步）。

---

## 4. 加新 connector

例：Telegram bot。

### 改动清单

1. **新建 `connectors/telegram_bot.py`**
   - 模仿 `napcat_bot.py` 的结构（@cmd 装饰器 + dispatch）
   - 收到 Telegram 消息 → 解析 `/balance` → 调 `core/portfolio_manager.PortfolioManager` 获取数据
   - 不要自己改 portfolio dict，必须走 `pm.cash_amount(...)` / `pm.holdings.find(...)`

2. **systemd unit `systemd/invest-telegram.service`**
   ```ini
   [Service]
   ExecStart=/path/uv run python -m connectors.telegram_bot
   ```

3. **`.env.example` 加 `TELEGRAM_BOT_TOKEN`**

4. **写 README 章节**说明触发协议

工时：~半天。

**关键约束**：connector 必须只做协议转换，业务逻辑全部 forward 给 `core/`。
违反 → connector 间行为飘移（早期 napcat 改 dict 的教训）。

---

## 5. 加新 cron job

例：每周日发周报邮件。

### 改动清单

1. **新建 `jobs/weekly_email.py`**
   ```python
   def run() -> Dict[str, Any]:
       # 拉最近 7 天 verdict + PnL + 数据源健康
       # 渲染 markdown
       # services/notifier.send_email(...)
       return {"status": "success", "email_sent": True}
   ```

2. **新建 `jobs/weekly_email.yml`**
   ```yaml
   name: weekly_email
   description: 每周日 09:00 发周报
   schedule: "0 9 * * 0"
   timezone: Asia/Shanghai
   entry: jobs.weekly_email:run
   enabled: true
   ```

3. **测试 `tests/test_weekly_email.py`**

4. **写 `jobs/README.md` 加一行**

5. **GUI `/system` Cron Jobs tab 自动展示**（无需改前端）

工时：~1-2 小时。

---

## 6. 加新 Web 端点

例：`GET /api/portfolio/by_currency?currency=CNY` 只返回该币种的资产。

### 改动清单

1. **在 `connectors/web_api/routers/` 选对应域的 router 加 endpoint + 在 `connectors/web_api/models.py` 加 Pydantic 响应模型**
   ```python
   class ByCurrencyResponse(BaseModel):
       currency: str
       cash: float
       holdings: List[HoldingV2]
       total: float

   @app.get("/api/portfolio/by_currency", response_model=ByCurrencyResponse)
   async def by_currency(currency: str = Query(...)):
       pm = _new_pm()
       # ... filter holdings by cost_currency ...
       return ByCurrencyResponse(...)
   ```

2. **测试 `tests/test_web_api.py`**

3. **重启 `invest-web.service`**（systemd 不会自动 hot reload）

4. **前端同步类型**：
   ```bash
   cd invest-gui && pnpm gen-types
   # 检查 TS 编译，加新组件用它
   ```

5. **更新 [06-api.md](06-api.md) 端点表**

工时：~1 小时。

---

## 7. 加新 GUI 路由 / tab

### 场景 A：加全新顶级路由（如 `/reports`）

1. `invest-gui/src/routes/Reports.tsx` 新建
2. `invest-gui/src/main.tsx` 加 `<Route path="reports" element={<Reports />} />`
3. `invest-gui/src/App.tsx` 顶导加 link
4. 决定属于「核心日常」还是「次级」（参考 [10-design-system.md](10-design-system.md)）

### 场景 B：在 `/committee` 加新 tab

1. `invest-gui/src/routes/committee/MyNewTab.tsx` 新建
2. `invest-gui/src/routes/committee/index.tsx` 在 tabs 数组加一项
3. 注意：tab 数已经 7 个，加之前思考"是否真的需要独立 tab"

### 场景 C：在 `/system` 加新 tab

同 B，改 `routes/System.tsx`。

工时：~30 分钟（不含具体 tab 内容）。

---

## 通用规则（适用所有改动）

### 不要做的事

- ❌ 在 connector 里直接改 portfolio dict（必须走 `with_portfolio_tx()`）
- ❌ 加新字段不更新 Pydantic schema（写盘会被 validation 拒）
- ❌ 改 `_render_portfolio_body_v2()` 的输出格式（LLM 在读它，breaking change 影响 prompt）
- ❌ 加 endpoint 不更新前端类型（运行时报 `undefined` 字段）
- ❌ 改完不写测试（166 测试是底线，新功能至少 3 测试覆盖 happy / error / 并发）

### 必做的事

- ✅ 加新代码必须更新对应子目录 README（如 `agents/README.md`）
- ✅ 加新概念必须更新本 wiki 对应章节（避免 docs 飘）
- ✅ 加新决策必须开 ADR
- ✅ 跑 `uv run pytest tests/ -v` 必须全绿
- ✅ commit message 写"为什么"不只是"做了什么"

---

## 下一步

→ [09-troubleshooting.md](09-troubleshooting.md) — 改坏了去哪查

→ [adr/](adr/) — 看历史决策怎么记录
