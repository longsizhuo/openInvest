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
