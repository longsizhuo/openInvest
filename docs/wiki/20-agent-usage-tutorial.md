---
type: wiki-chapter
title: 使用教程（Agent 视角从零到日常）
tags: [tutorial, plugin, mcp, skill, onboarding, decision-accounting]
intent: 新用户（人 + agent）从安装到日常使用 + 决策闭环的完整路径
documents:
  endpoints:
    - GET /api/decisions
    - POST /api/decisions/execution
  config_keys: []
  symbols:
    - run_committee
    - record_execution
---

# 使用教程

> openInvest 是给 AI agent 用的投资 runtime（[issue #133](https://github.com/longsizhuo/openInvest/issues/133)）：
> 你的 agent 负责对话和记忆，openInvest 负责投资决策。下面按最常见路径展开。

## 1. 安装（Claude Code，推荐）

```
/plugin marketplace add longsizhuo/openInvest
/plugin install invest@openinvest
```

装完得到三样东西，全部零配置：

| 组件 | 作用 |
|---|---|
| **MCP server**（14 工具，自动注册） | agent 能调什么：status / live_prices / decisions / explain_decision / record_execution / buy / sell / run_committee … |
| **invest skill** | agent 怎么编排委员会（Coordinator 协议、决策纪律） |
| **invest-setup skill** | 首次 5 问 onboarding |

后端在首次调用时自举：`git clone` 到 `~/openInvest` + `uv sync`（需要 `git` / `uv` / Python 3.13）。
**首次 MCP 连接可能超时**（在等 clone+sync）——先说一句 "set up invest" 让 skill 路径完成自举，之后 MCP 秒连。

不用 plugin 也可以只挂 MCP（已 clone 仓库的情况）：

```bash
claude mcp add openinvest -e INVEST_HOME=~/openInvest -- \
  uv --directory ~/openInvest run python -m connectors.mcp_server
```

## 2. 初始化

对 Claude 说 **"set up invest" / 帮我初始化 invest** —— 5 个问题（称呼、风险偏好、
收入、当前持仓、可选 DEEPSEEK_API_KEY）写入配置。持仓多的话直接甩券商截图，
agent 自己 OCR 后走 `import`。

## 3. 日常使用（自然语言即可，agent 自己选工具）

| 你说 | agent 调 |
|---|---|
| "看看我的持仓 / 涨了多少" | `status` |
| "黄金现在什么价" | `live_prices` |
| "纳指跌 10% 我亏多少" | `what_if` |
| "该不该买 AAPL / 跑个委员会" | Coordinator（Claude Code，免 API key）或 `run_committee`（Direct，需 DEEPSEEK_API_KEY） |
| "为什么今天是 HOLD" | `explain_decision`（完整 4 角色辩论 transcript + CIO memo） |
| "委员会都拦了我什么" | `discipline` |
| "刚买了 500 股" | `buy` / `record_trade` |

## 4. 决策闭环（Decision Accounting）

委员会给了建议之后，**告诉 agent 你做没做**——这是闭环的入口：

```
委员会：ACCUMULATE 510300.SS ¥2100
你：这次我不买了。
agent：好的，方便说下原因吗？（估值 / 资金 / 不同意？）
你：现金留着交房租。
agent → record_execution("2026-07-02/510300.SS", rejected, reason="现金留给房租")
```

之后随时问：

- **"我听了几次建议？"** → `decisions` 返回采纳率 + 每条决议的 决议↔干预↔执行↔结果 全链
- **"哪些建议我没执行、后来怎么样？"** → 同上，`outcome` 字段有事后 1d/7d/30d 收益
- **"防御规则在省钱还是费钱？"** → `discipline` 反事实账本

不声明也有兜底：7 天内同标的同向成交会被自动匹配为"已执行"。

## 5. 非 Claude 的 agent

- **Codex**：`codex plugin marketplace add longsizhuo/openInvest`（同一套 skill）
- **Gemini / Cursor / 任意脚本**：CLI Direct 路径——`run.sh run_committee SYM` 一键
  拿 verdict（需 DEEPSEEK_API_KEY）；全部子命令见 [SKILL.md](../../skills/invest/SKILL.md)
- **任意 MCP client**：stdio server 是标准 MCP，不挑 client

## 6. Web GUI（可选，纯展示层）

```bash
run.sh gui          # http://127.0.0.1:8765
```

看持仓 / 批量录入 / 委员会直播用；不部署也不影响 agent 路径。

## 下一步

→ [04-execution-paths.md](04-execution-paths.md) — Coordinator vs Direct 双路径原理

→ [06-api.md](06-api.md) — REST / MCP 全部端点

→ [05-data-model.md](05-data-model.md) — 账本都存在哪
