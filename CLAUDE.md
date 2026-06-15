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

> 2026-06-15：`core/committee_runner.py` 已按职责拆成 `core/runner/` 包（event_brief / loaders / intervention / session / coordinator），`committee_runner.py` 留薄壳 façade re-export 全部符号——`from core.committee_runner import X` 对所有历史 X 仍可用，entry 零改。下面写新位置；旧 façade 路径仍有效。
>
> 2026-06-15（#57）：`core/committee.py` 同款拆成 `core/committee/` 包（agent_io / cio_parse / views / loaders / debate / persist），`__init__.py` 留薄壳 façade re-export 全部历史符号（含下划线名 + 模块常量）——`from core.committee import X` 与 `core.committee.X` 属性访问对所有历史 X 仍可用，entry / service / 测试 / 脚本零改。注意 `run_committee` 在 `debate.py` 命名空间内解析 `_create_agent` / `_persist`，monkeypatch 要钉 `core.committee.debate.*` 而非 façade 属性。

> 2026-06-15：`connectors/web_api/routers/system.py`（#55 漏下的 catch-all）按域拆成 `insights` / `observability` / `verdict_review` / `committee_sessions` / `regime` / `state` 六个 router，装配处 `connectors/web_api/__init__.py` 的 `include_router` 循环同步换成这 6 个模块。所有 @router.get path 逐字不变（含 `/api/regime/{symbol:path}`），/openapi.json paths 零漂移。旧 `system.py` 已删除（全仓无外部 import，纯 router 容器无需保留 façade）。

| 层 | 文件 | 职责 | 禁止 |
|---|---|---|---|
| **Entry** | `jobs/daily_report.py`, `connectors/web_api.py:_run_committee_task`, `scripts/skill.py:cmd_run_committee`（入口 façade，实现在 `scripts/skill_cmds/committee_cmds.py:cmd_run_committee`） | 触发 + 该路径独有的事（cache 检查 / SSE 推送 / 邮件 / Gemini / Dreaming） | ❌ 直接调 `core.committee` 任何函数（必经 `run_committee_session`）|
| **Orchestrator** | `core/runner/session.py:run_committee_session` | **三路径单一可信源**: 解析 symbols + 跨资产 macro 共享 + event_brief 三选一（override/event_ids/multi 召回）+ wealth view + 并行 dispatch + 聚合返回 | ❌ 邮件 / Gemini / SSE 等 cron/web/skill 特定逻辑 |
| **Service** | `core/runner/session.py:run_committee_for_symbol` | 单资产端到端 prep + 调原语 + 持久化 transcript | ❌ 跨层直接 IO（必经 PortfolioManager / MemoryStore）|
| **Primitive** | `core/committee/debate.py:run_committee` | 纯函数：prompt 编排 + 4 角色辩论 + LLM 调用 | ❌ 读 user.md / portfolio.md（输入必经参数传入）|

### Shared Input Loaders（单一可信源）

加新的 cross-entry 参数（如 `event_brief`, `wealth_context_view`, `prior_insights`）时**只改 Orchestrator**：

1. `core/runner/session.py:run_committee_session()` 加内部步骤读 loader（或加 `<name>_override` kwarg）
2. `core/runner/loaders.py:load_<name>()` 实现 IO 读取 + graceful 退化空字符串
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

## 发版（release-please）

**3 条独立版本线，靠 commit 改的路径自动分流**：

| Component | Tag 格式 | 控制文件 | 触发路径 |
|---|---|---|---|
| 后端 | `v0.1.x` | `pyproject.toml` `version` | 改 `core/` / `agents/` / `jobs/` / `connectors/` / `services/` / `scripts/` 等根目录代码 |
| invest skill | `invest-skill-v0.9.x` | `skills/invest/SKILL.md` `version:` | 改 `skills/invest/**` |
| invest-setup skill | `invest-setup-skill-v0.1.x` | `skills/invest-setup/SKILL.md` `version:` | 改 `skills/invest-setup/**` |

**改代码时不需要做任何事**——`release-please.yml` 监听 `main` push，自动开 Release PR、生成 `CHANGELOG.md`、merge 后打 tag。

### 唯一要守的纪律：commit message 严格 conventional commits

| 想要 | commit 怎么写 | bump |
|---|---|---|
| 新功能 → minor bump | `feat(scope): ...` | 0.1.0 → 0.2.0 |
| Bug fix → patch bump | `fix(scope): ...` | 0.1.0 → 0.1.1 |
| 破坏性变更 → major | `feat(scope)!: ...` 或 footer 加 `BREAKING CHANGE:` | 0.1.0 → 1.0.0 |
| 不进 CHANGELOG | `chore:` / `test:` / `ci:` / `build:` | 不 bump |
| 进 CHANGELOG 但不 bump | `docs:` / `refactor:` / `perf:` | 取决于类型 |

**误用 `feat:` 写琐碎改动 = 假版本跳跃**。fix 一个 typo 别 `feat:`。

### 一次 commit 跨组件（推荐拆开）

`feat(skill+web-api): ...` 这种 commit 触摸了 `skills/invest/` 和 `connectors/`——会同时触发 invest-skill 和后端两个 Release PR，但 CHANGELOG 文案是同一条。**推荐拆成两个 commit**，scope 清晰，CHANGELOG 也清晰。

### SKILL.md 的 version 字段是 release-please 管的

不要手改 `skills/invest/SKILL.md` 第 3 行的 `version: 0.9.0 # x-release-please-version`——release-please 靠那个注释锚定位置，merge Release PR 时会自动同步。手改了下次发版会被覆盖。

### 第一次发版会怎样

`main` 第一次 push 后，release-please 会扫所有历史 commit，开 3 个巨大的 Release PR（CHANGELOG 含全部历史 `feat`/`fix`）。可以在 PR 里手动编辑 `CHANGELOG.md` 截到合理起点再 merge。或者先 `bootstrap-sha: <current-main-HEAD>` 跳过历史——见 [release-please 文档](https://github.com/googleapis/release-please/blob/main/docs/manifest-releaser.md#bootstrapping)。

---

## 关键文件速查

```
skills/invest/SKILL.md             agent 触发指引（写"agent 怎么用"，不是"用户怎么用"）
scripts/skill.py           CLI 入口薄壳 façade（保留 main + ROOT；cmd 实现已拆到 scripts/skill_cmds/ 包）
scripts/skill_cmds/        skill 各 cmd 实现子包（_helpers/analysis/committee/portfolio/lifecycle 5 模块）
connectors/web_api/         FastAPI 端点包（GUI + CLI 共享）；app factory 在 __init__.py，端点按域拆在 routers/（insights/observability/verdict_review/committee_sessions/regime/state/read/write/...），响应模型在 models.py
core/portfolio_manager.py  持仓 façade，with_portfolio_tx fcntl 锁
core/committee/            委员会编排（包：agent_io/cio_parse/views/loaders/debate/persist + __init__ façade）
db/trades_db.py            内部账本 SQLite WAL（不连真实支付）
docs/wiki/                 完整文档
docs/wiki/adr/             关键决策记录（v1 退场 / daily_report 拆 / 双路径）
```
