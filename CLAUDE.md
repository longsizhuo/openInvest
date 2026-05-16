# CLAUDE.md — openInvest 项目级指引

> 这份文件给 Claude（或任何 AI agent）改代码时看。**核心产品哲学先看完，再动手**。

## 核心产品哲学

openInvest 有三个调用层，每层服务不同对象：

| 层 | 服务对象 | 目的 |
|----|---------|------|
| **GUI**（invest-gui，挂 :8765 静态文件） | **小白用户** | 可视化看持仓 / 批量录入 / 系统状态 |
| **CLI / Skill**（`scripts/skill.py` + `skills/invest/scripts/run.sh`）| **AI agent**（Claude / Gemini / Cursor / Cline / Codex / 任意脚本）| Agent 跑全链路：查询 + 记账 + 改持仓 + 触发委员会 |
| **Web API**（`connectors/web_api.py`，挂 :8765 `/api/*`）| **共享底层** | GUI 和 CLI 都通过它写数据 |

### 关键原则

1. **Agent 必须拥有全部功能**——CLI 不能"只能读不能写"。任何用户能用 GUI 做的事，agent 都能用 CLI 做。
2. **GUI 只是面对用户的展示层**——不是必需。fork 用户可以完全不部署 GUI，只用 CLI 走 agent 路径。
3. **CLI / GUI 都共用 Web API 底层**——所有写操作走 `with_portfolio_tx`（fcntl 锁 + atomic write）保证一致性。

### 写代码时

- 加新功能，问自己：**agent 怎么调？** 如果只是给 GUI 加按钮，没补 CLI 子命令 / Web API 端点，**你做错了**——agent 用不上
- CLI 子命令缺写操作时，**优先补 CLI 子命令**而不是只让 agent 调 web API curl
- 不要在 SKILL.md 写"CLI 只读 / 写操作走 web API"——这是反产品哲学的措辞

## 测试纪律

- **CI 自动跑**（`.github/workflows/ci.yml`）—— pytest 全套 + smoke import + 脱敏字段 grep
- **不要靠 dev 自己想起来跑 pytest**——commit 前 CI 会跑，红就别合
- 加新模块要在 `ci.yml` 的 smoke import 步骤同步加 import 检查

## 公开数据红线

按金融视角 review 决定：

1. 公开 URL（`docs/accuracy_summary.json` / pnl-data 分支 / outperform feed）**绝对不能含** symbol / threshold / verdict 原文 / 任何可反推持仓的字段
2. 命中率统计页 n < 30 不展示具体数字（防小样本被截图误传）
3. README outperform 同时展示 winning + losing 事件（不只 winning，避免 survivorship bias）
4. PATCH executed 必须同步 cash + holdings（账本一致性）

## 双路径架构（Coordinator vs Direct）

- **Coordinator 路径**：Claude Code 用 `prepare_committee` + spawn 4 subagent。**不需要 DEEPSEEK_API_KEY**
- **Direct 路径**：任意 agent（Gemini / Cursor / 普通脚本）用 `run_committee SYM` 一键 = 后端 DeepSeek 跑 4 角色辩论。**需要 DEEPSEEK_API_KEY**

详见 `docs/wiki/04-execution-paths.md` + `skills/invest/references/two-paths.md`。

## 分层契约（防漂移）

跨 entry 漂移的根因：多个 entry 直接调 core 原语，各自负责"准备参数"，新加参数
时漏 1 处。2026-05-15 wealth_context_view 漂移就是典型 — prompt 层接了 + e2e
测试手动传了，但 daily_report / scripts.skill 没人准备 → 三个月没用到。

### 强制 4 层（2026-05-16 三路径统一架构）

| 层 | 文件 | 职责 | 禁止 |
|---|---|---|---|
| **Entry** | `jobs/daily_report.py`, `connectors/web_api.py:_run_committee_task`, `scripts/skill.py:cmd_run_committee` | 触发 + 该路径独有的事（cache 检查 / SSE 推送 / 邮件 / Gemini / Dreaming） | ❌ 直接调 `core.committee` 任何函数（必经 `run_committee_session`）|
| **Orchestrator** | `core/committee_runner.py:run_committee_session` | **三路径单一可信源**: 解析 symbols + 跨资产 macro 共享 + event_brief 三选一（override/event_ids/multi 召回）+ wealth view + 并行 dispatch + 聚合返回 | ❌ 邮件 / Gemini / SSE 等 cron/web/skill 特定逻辑 |
| **Service** | `core/committee_runner.py:run_committee_for_symbol` | 单资产端到端 prep + 调原语 + 持久化 transcript | ❌ 跨层直接 IO（必经 PortfolioManager / MemoryStore）|
| **Primitive** | `core/committee.py:run_committee` | 纯函数：prompt 编排 + 4 角色辩论 + LLM 调用 | ❌ 读 user.md / portfolio.md（输入必经参数传入）|

### Shared Input Loaders（单一可信源）

加新的 cross-entry 参数（如 `event_brief`, `wealth_context_view`, `prior_insights`）时**只改 Orchestrator**：

1. `core/committee_runner.py:run_committee_session()` 加内部步骤读 loader（或加 `<name>_override` kwarg）
2. `core/committee_runner.py:load_<name>()` 实现 IO 读取 + graceful 退化空字符串
3. `run_committee_for_symbol` 加 `<name>_override` kwarg，session 一次调好后传进来避免重复
4. `tests/test_committee_contract.py:test_run_committee_session_*` 加 SENTINEL 测试守

**三个 entry 不需要改任何代码** — session 改完三路径自动同步。

### 机器强制（不靠记忆）

- **`uv run lint-imports`**（CI 跑）：禁止 `jobs/` / `connectors/` / `scripts/` 直接 `from core.committee import ...`，必须走 `committee_runner`。例外只剩 `scripts.backtest_committee`（研究脚本，与 production 不共享 service layer）
- **`uv run pytest tests/test_committee_contract.py`**：SENTINEL 契约测试守 session 内部 wealth/event/macro 真的注入 run_committee
- 想绕过 → CI 红 → 别合

### 漂移历史

| 时间 | 漂移 | 根因 | 防御 |
|---|---|---|---|
| 2026-05-15 | wealth_context_view 三个月没进 production | entry 各自 prep, 漏一处 | import-linter + contract test 上线 |
| 2026-05-16 | daily_report cron 路径 event_brief 全漏（4 处）：run_macro_view / run_committee for-loop / Gemini prompt 均未注入 event_brief；Gemini prompt 也未注入 wealth_view | daily_report 直调原语，Gemini prompt 是硬编码 f-string，resolve_event_brief_multi 虽已存在但 cron 路径没调用 | resolve_event_brief_multi 调用注入 macro + committee；Gemini prompt 抽 build_gemini_prompt() 纯函数接 wealth_view/event_brief；4 处 SENTINEL 契约测试上线 |
| 2026-05-16 | 三路径（Skill/Web/Cron）各自手搓 multi-asset orchestrator，3 个月连续 4 次跨 entry 参数漂移事故。**根治**: 抽 `run_committee_session` 作为三路径单一可信源；service layer `run_committee_for_symbol` 同时漏接 prior_insights（Web/GUI 永远看不到 Dreaming） | 缺少统一 orchestrator，每个 entry 自己重建 macro 共享 / event multi 召回 / wealth view 注入 | run_committee_session(symbols=, ...) 主入口；run_committee_for_symbol 加 portfolio_summary_override + prior_insights_override + wealth_context_view override kwargs；3 个 entry 全部改走 session；旧 entry-specific contract test 删除，统一 session 契约守 |

新增漂移事故 → 在这表加一行 + 加新 contract test。

---

## 关键文件速查

```
skills/invest/SKILL.md             agent 触发指引（写"agent 怎么用"，不是"用户怎么用"）
scripts/skill.py           CLI 入口（doctor/init/status/run_committee/...）
connectors/web_api.py      FastAPI 端点（GUI + CLI 共享）
core/portfolio_manager.py  持仓 façade，with_portfolio_tx fcntl 锁
core/committee.py          委员会编排
db/trades_db.py            内部账本 SQLite WAL（不连真实支付）
docs/wiki/                 完整文档
docs/wiki/adr/             关键决策记录（v1 退场 / daily_report 拆 / 双路径）
```
