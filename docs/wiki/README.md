# openInvest Wiki

> 多资产 AI 投资委员会系统的完整文档。
> 主仓库 [longsizhuo/openInvest](https://github.com/longsizhuo/openInvest) · GUI [longsizhuo/invest-gui](https://github.com/longsizhuo/invest-gui)
> 主页 [README.md](../../README.md) · 30 分钟 fork [QUICK_START.md](../QUICK_START.md)

---

## 按读者画像选路径

### 🎯 我是用户，想用起来
1. [README.md](../../README.md) hero —— 这工具到底在做什么
2. [QUICK_START.md](../QUICK_START.md) —— 30 分钟从 clone 到第一份 AI memo
3. [09-troubleshooting.md](09-troubleshooting.md) —— 跑挂了去哪里看

### 🛠 我是开发者，想改 / 扩
1. [01-architecture.md](01-architecture.md) —— 心智模型必读
2. [05-data-model.md](05-data-model.md) —— v2 schema + concurrent RMW
3. [07-extending.md](07-extending.md) —— cookbook: 加资产 / 数据源 / agent 角色
4. [06-api.md](06-api.md) —— REST 端点 + 前端类型同步
5. [08-deployment.md](08-deployment.md) —— Caddy + CF Access + systemd

### 🧠 我是研究者，想懂"为什么这么设计"
1. [02-agents.md](02-agents.md) —— 4 LLM 角色 + cross-challenge + REGIME 硬约束
2. [03-dreaming.md](03-dreaming.md) —— OpenClaw 风格的三阶段记忆整合
3. [04-execution-paths.md](04-execution-paths.md) —— Skill vs Web/Cron 双路径
4. [adr/](adr/) —— 架构决策记录（为什么不上 Claude Agent SDK 等）

### 🎨 我是设计师 / 前端，想改 GUI
1. [10-design-system.md](10-design-system.md) —— Token 三层 + 排版 + IA
2. [invest-gui/README.md](https://github.com/longsizhuo/invest-gui#readme) —— 前端仓库结构

---

## 全部章节

| # | 章节 | 一句话 |
|---|------|--------|
| 01 | [架构总览](01-architecture.md) | 4 层（Connectors / Agents / Core / Memory）+ 数据流 + 并发模型 |
| 02 | [4 角色委员会](02-agents.md) | Macro / Quant / Risk / CIO + cross-challenge + REGIME |
| 03 | [Dreaming 记忆整合](03-dreaming.md) | Light / REM / Deep Sleep 三阶段，跨会话长期记忆 |
| 04 | [双执行路径](04-execution-paths.md) | Skill（Claude 真 subagent）vs Web/Cron（DeepSeek 多线程）|
| 05 | [数据模型](05-data-model.md) | v2 schema (cash dict + holdings list) + 迁移 + 并发安全 |
| 06 | [Web API](06-api.md) | FastAPI 40+ 端点 + Pydantic + 同源部署 |
| 07 | [扩展指南](07-extending.md) | 加新资产 / 新数据源 / 新 agent 角色 |
| 08 | [生产部署](08-deployment.md) | Caddy + Cloudflare Access + systemd + sync_gui_dist |
| 09 | [故障排查](09-troubleshooting.md) | 线上跑挂去哪查 journal / SSE 断了 / sync 失败 |
| 10 | [设计系统](10-design-system.md) | GUI token + 排版三层 + IA 决策 |

## 架构决策记录 (ADR)

记录"为什么这么做"，给将来想推翻设计的人留依据。

| ID | 决策 | 状态 |
|----|------|------|
| [001](adr/001-dual-execution-paths.md) | 委员会保留 Skill + Web/Cron 双路径 | ✅ 已采纳 |
| [002](adr/002-no-claude-agent-sdk.md) | 暂不升级到 Claude Agent SDK | ✅ 已采纳 |
| [003](adr/003-v2-data-model.md) | portfolio.md 升 v2 通用 schema | ✅ 已采纳 |

---

## 维护规则

- **本 wiki 是"为什么"的源**——具体实现细节看代码 + 子目录 README，本 wiki 只讲设计意图
- 新加一个大功能：必须同步更一页（或开新页）
- 新加一个架构决策：必须开 ADR
- 子目录 README 改名 / 重组：本 wiki 链接要跟着
- ADR 一旦"已采纳"就别改，要推翻新开一个 supersedes 它

> 老规矩：[CONTRIBUTING vs Wiki 分工](../../README.md#贡献)——CONTRIBUTING 写"怎么参与"（稳态），Wiki 写"系统是什么"（活的）。
