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
6. [~~加新 Web 端点~~（deprecated，改加 CLI 子命令 / MCP 工具）](#6-加新-web-端点deprecated)
7. [~~加新 GUI 路由 / tab~~（GUI 已退役 2026-07-05）](#7-加新-gui-路由--tab已退役)

---

## 1. 加新资产

### 场景 A：用户想追踪一只股（CLI / MCP 操作，不改代码）

`uvx openinvest buy --symbol NDQ.AX --units 0 ...`（或对 agent 说"track NDQ.AX"，
走 MCP `buy` 工具）→ portfolio.md `holdings` list 自动追加。

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

### 2.x 新闻/事件源自配（无需改代码）

事件层三类源，按你的市场自由配：

1. **RSS 自配**：`services/news_sources/rss_feeds.yml` 就是普通 feed 列表——加你市场的源即可。
   没有原生 RSS 的站点推荐自建 [RSSHub](https://github.com/DIYgod/RSSHub)
   （`docker run -d -p 1200:1200 diygod/rsshub`，5000+ 路由含财新/华尔街见闻/雪球等），
   feed URL 指向自己的实例。泛市场 feed 会过持仓别名/macro 关键词预过滤（`event.rss_prefilter_enabled`）。
2. **中文快讯 wire（内置）**：watched 含 A 股 symbol（.SS/.SZ/.BJ）时自动激活
   akshare 东财快讯 + 新浪 7×24（`news_sources/akshare_news.py`）；境内部署可照样式加财联社电报。
3. **agent 投喂（最通用）**：任何市场/语言，宿主 agent 搜到直接 `ingest_event` 喂进管道——
   这是海外/小众市场的首选通道，见 SKILL.md 投喂纪律。

## 3. 加新 agent 角色

例：加一个 "ESG Analyst"，看可持续投资指标。

### 改动清单

1. **新建 `capabilities/committee/esg_analyst.py`**
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

4. **改 `connectors/web_api/routers/regime.py:get_regime_rules()` + `connectors/web_api/models.py:RegimeRulesResponse`** 注册新角色到 `/api/regime_rules`（存量端点维护，非新增）

5. **更新 [02-agents.md](02-agents.md)** 角色矩阵

6. **同步 Coordinator 路径**：`skills/invest/scripts/run.sh prepare_committee` 也要 spawn 第 5 个 subagent
   → 这是双路径的成本，详见 [04-execution-paths.md](04-execution-paths.md)
   （Direct 路径 `run_committee` 复用 `core/committee/`，第 4 步生效后自动跟上，不用单独改）

工时：~半天（含两条路径同步）。

---

## 4. 加新 connector

例：Telegram bot。

### 改动清单

> 先问一句：**真的要写 connector 吗？** 多数聊天/agent 场景直接接 MCP
> （`openinvest-mcp`）就够了，零代码。只有目标平台不支持 MCP 才写。

1. **新建 `src/openinvest/connectors/telegram_bot.py`**
   - @cmd 装饰器 + dispatch 结构（历史上 NapCat QQ bot 用这个模式，已于 2026-07-05 删除；
     考古可看 git history 里的 `connectors/napcat_bot.py`）
   - 收到 Telegram 消息 → 解析 `/balance` → 调 `core/portfolio_manager.PortfolioManager` 获取数据
   - 不要自己改 portfolio dict，必须走 `pm.cash_amount(...)` / `pm.holdings.find(...)`

2. **systemd unit `systemd/invest-telegram.service`**
   ```ini
   [Service]
   ExecStart=/path/python -m openinvest.connectors.telegram_bot
   ```

3. **`.env.example` 加 `TELEGRAM_BOT_TOKEN`**

4. **写 README 章节**说明触发协议

工时：~半天。

**关键约束**：connector 必须只做协议转换，业务逻辑全部 forward 给 `core/`。
违反 → connector 间行为飘移（早期 NapCat bot 直接改 dict 的教训——该 connector
本身已删除，教训保留）。

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

工时：~1-2 小时。

---

## 6. 加新 Web 端点（deprecated）

> ⚠️ Web API 已 deprecated（2026-07-05）：存量端点只服务 remote hub 模式与内部触发，
> **不再新增端点**。新功能面向用户/agent 的入口是：
>
> 1. **CLI 子命令**：`src/openinvest/cli.py` 注册 + `src/openinvest/skill_cmds/` 实现
> 2. **MCP 工具**：`src/openinvest/connectors/mcp_server.py` 加工具定义
> 3. 更新 `skills/invest/SKILL.md` 让非 Claude agent 也能调
>
> 只有维护存量 hub 端点（bug fix / schema 修正）才碰 `connectors/web_api/`，
> 测试在 `tests/test_web_api.py`，改完重启 `invest-web.service`。

---

## 7. 加新 GUI 路由 / tab（已退役）

> ⚠️ 2026-07-05 GUI 已退役，invest-gui 仓库封存待重做（重做走独立前端连 MCP）。
> 本节内容已删除；历史设计语言存档见 [10-design-system.md](10-design-system.md)。

---

## 通用规则（适用所有改动）

### 不要做的事

- ❌ 在 connector 里直接改 portfolio dict（必须走 `with_portfolio_tx()`）
- ❌ 加新字段不更新 Pydantic schema（写盘会被 validation 拒）
- ❌ 改 `_render_portfolio_body_v2()` 的输出格式（LLM 在读它，breaking change 影响 prompt）
- ❌ 加新用户功能只碰 Web API 不补 CLI / MCP（Web API 已 deprecated，agent 用不上）
- ❌ 改完不写测试（166 测试是底线，新功能至少 3 测试覆盖 happy / error / 并发）

### 必做的事

- ✅ 加新代码必须更新对应子目录 README（如 `capabilities/README.md`）
- ✅ 加新概念必须更新本 wiki 对应章节（避免 docs 飘）
- ✅ 加新决策必须开 ADR
- ✅ 跑 `uv run pytest tests/ -v` 必须全绿
- ✅ commit message 写"为什么"不只是"做了什么"

---

## 下一步

→ [09-troubleshooting.md](09-troubleshooting.md) — 改坏了去哪查

→ [adr/](adr/) — 看历史决策怎么记录
