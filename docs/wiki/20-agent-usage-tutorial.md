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

## 1. 安装（以 Claude Code 为例；其他 agent 见第 5 节）

```
/plugin marketplace add longsizhuo/openInvest
/plugin install invest@openinvest
```

装完得到三样东西，全部零配置：

| 组件 | 作用 |
|---|---|
| **MCP server**（18 工具，自动注册） | agent 能调什么：status / live_prices / decisions / explain_decision / record_execution / buy / sell / run_committee … |
| **invest skill** | agent 怎么编排委员会（Coordinator 协议、决策纪律） |
| **invest-setup skill** | 首次 5 问 onboarding |

后端从 PyPI 分发（[`openinvest`](https://pypi.org/project/openinvest/)）：首次调用时 `uvx` 自动拉包（只需要 `uv`），数据放 `~/openInvest`（memory/ db/ .env）。更新跑 `run.sh update`。
**首次 MCP 连接可能超时**（在等 PyPI 下载）——重试一次即可，之后 uvx 走缓存秒连。

不用 plugin 也可以只挂 MCP：

```bash
claude mcp add openinvest -e INVEST_HOME=~/openInvest -- uvx openinvest-mcp
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

## 5. 其他 agent（Codex / Hermes / OpenClaw / 任意 MCP client）

> 接入原则见 [#133](https://github.com/longsizhuo/openInvest/issues/133)：openInvest
> 是 agent 的投资 runtime，MCP 是第一接口；对话/记忆/通知渠道由宿主 agent 自己负责。

- **Codex**：`codex plugin marketplace add longsizhuo/openInvest`（同一套 skill）
- **Gemini / Cursor / 任意脚本**：CLI Direct 路径——`run.sh run_committee SYM` 一键
  拿 verdict（需 DEEPSEEK_API_KEY）；全部子命令见 [SKILL.md](../../skills/invest/SKILL.md)
- **任意 MCP client**：stdio server 是标准 MCP，不挑 client

### Hermes Agent（Nous Research）

MCP 声明式接入（`~/.hermes/config.yaml`）：

```yaml
mcp_servers:
  openinvest:
    command: uvx
    args: ["openinvest", "mcp"]
    env:
      INVEST_HOME: ~/openInvest
```

Hermes 自动发现 18 个工具并与内置工具并列注册。skills 一条命令装：

```bash
hermes plugins install longsizhuo/openInvest --enable
```

装完以 `openinvest:invest` / `openinvest:invest-setup` 命名空间加载（Hermes plugin
skill 是显式加载制，不进自动索引）。注意 `hermes skills install` 路线会被其社区源
安全扫描拦截（金融写操作 skill 必触发 dangerous verdict）——走 plugin 路线。

**信息联动**：若装有 [Longbridge](https://github.com/longbridge/skills) 等行情/新闻
skill，看到与持仓相关的新闻时用 `ingest_event` 喂进事件账本——券商级信息源
接入决策管道，零额外集成。

### OpenClaw

MCP 声明式接入（`~/.openclaw/openclaw.json`，JSON5）：

```json5
{
  mcp: {
    servers: {
      openinvest: {
        command: "uvx",
        args: ["openinvest", "mcp"],
        env: { INVEST_HOME: "~/openInvest" },
      },
    },
  },
}
```

旧版本没有 MCP client 时，fallback 走 workspace `skills/` 拷贝（skills 经 uvx 驱动后端）：

```bash
git clone https://github.com/longsizhuo/openInvest ~/openInvest
cp -r ~/openInvest/plugin/skills/* ~/.openclaw/workspace/skills/   # workspace 路径以你的配置为准
```

注意别用 `openclaw plugins install`——截至 2026-07（openclaw-python 0.8.x）它是
未实现的 stub，返回假 success 但什么都不装。

### 每日日报 cron（宿主 agent 侧）

服务器内置的 `daily_report` cron 已于 2026-07-12 默认停用（`jobs/daily_report.yml`
`enabled: false`）——按 [#133](https://github.com/longsizhuo/openInvest/issues/133)
的分工，定时触发和通知渠道归宿主 agent，openInvest 只提供能力面：

```bash
uvx openinvest daily_report   # 完整日报管道，stdout = 邮件正文同源 markdown，不发邮件
```

**报告格式由后端统一保证**（同一条 `assemble_full_report` 管道：多资产委员会 +
Gemini 第二意见 + 翻译官人话解读 + 纪律台账）——所有用户、所有 agent 的 cron
拿到的日报一模一样，不需要在 prompt 里教 agent 怎么写报告。

以 Hermes 为例（`--no-agent` = 脚本 stdout 原样投递，Hermes 侧零 LLM token）：

```bash
mkdir -p ~/.hermes/scripts
cat > ~/.hermes/scripts/daily_invest.sh <<'EOF'
#!/usr/bin/env bash
INVEST_HOME=~/openInvest exec uvx openinvest daily_report
EOF
hermes cron create "0 2 * * 1-5" --name daily-invest-report \
  --no-agent --script daily_invest.sh --deliver <渠道>
```

注意：

- cron 按服务器本地时区解析（UTC 机器上北京 10:00 = `0 2 * * 1-5`）；`--deliver`
  换成你配好的渠道（telegram / discord / signal / platform:chat_id）
- 后端跑委员会走 Direct 路径，`INVEST_HOME/.env` 里要有 `DEEPSEEK_API_KEY`（同原 cron）
- 重跑会重新跑整个委员会（消耗 token）——语义与原 cron / GH Actions `workflow_dispatch` 一致
- 熔断（数据全废）或未配置 target_assets 时 stdout 是结构化 JSON——收到 JSON = 该排查了
- 想要 LLM 二次加工（翻译 / 只推增量），改 `--skill openinvest:invest` + 一句话
  prompt（"跑 `daily_report` 子命令，把输出按 X 加工后投递"），报告主体仍由后端生成
- OpenClaw 等其他带 cron 的 agent 同理——能定时跑 shell 就能用

## 6. ~~Web GUI~~（已退役 2026-07-05）

GUI 壳层已退役：`run.sh gui` 已删除，后端不再 serve 网页面板。看持仓 / 录入 /
跑委员会全部走上面的 CLI / MCP / skill 路径。前端重做时会以独立仓库直连 MCP。

## 下一步

→ [04-execution-paths.md](04-execution-paths.md) — Coordinator vs Direct 双路径原理

→ [06-api.md](06-api.md) — REST / MCP 全部端点

→ [05-data-model.md](05-data-model.md) — 账本都存在哪
