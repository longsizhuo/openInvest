<div align="center">

<img src="docs/logo.svg" alt="openInvest logo" width="120" height="120"/>

# openInvest


**自部署的 AI 投资委员会工具。4 个独立 LLM 角色互相 challenge，给出投资建议——决策权归你。**

[![Python](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Claude Code](https://img.shields.io/badge/Skill-Claude%20Code-D97757?logo=anthropic&logoColor=white)](https://claude.com/claude-code)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](#)
[![Stars](https://img.shields.io/github/stars/longsizhuo/openInvest?style=social)](https://github.com/longsizhuo/openInvest)

[⚡ 示例 memo](examples/sample_memo.md) · [🪄 装上](#装上) · [📚 完整文档](docs/wiki/README.md) · [🤝 贡献](CONTRIBUTING.md)

</div>


## 实盘 PnL（live）

<div align="center">
  <img src="https://raw.githubusercontent.com/longsizhuo/openInvest/pnl-data/docs/pnl_chart.svg" alt="PnL chart" width="100%"/>
  <sub>每 2h 自动更新到 <a href="https://github.com/longsizhuo/openInvest/tree/pnl-data">pnl-data 分支</a> · 上半 = 30 天趋势 · 下半 = vs 8 个基准的累计涨幅</sub>
  <br/>
  <sub>📌 <b>图中数据为作者本人账户</b>，仅作方法论展示。你跑起来后看到的是<b>你自己的</b>持仓曲线。</sub>
</div>

<!-- OUTPERFORM_FEED_START — jobs/pnl_snapshot 每 2h 追加 -->
<!-- OUTPERFORM_FEED_END -->

**对比的基准**：AI 投顾 / 公募基金 / 储蓄理财 / 大盘指数 4 类共 8 条。完整对比方法论 + 数据源说明见 [docs/wiki/03-benchmarks.md](docs/wiki/README.md)。

**实盘命中率公开**（自我披露不利数字）：方向性 verdict 历史命中率 25%，HOLD 占 84%——见 [docs/verdict_accuracy.md](docs/verdict_accuracy.md)。这工具不会让你致富，它只是把决策过程透明化。


---
- **Web GUI 是 beta**——主面板 / 决策回放 / 实时同步可能不工作。
  推荐入口：通过 Claude Code / Cursor / Cline 等 AI agent 跑 `invest` skill，
  让 AI 带你看持仓、跑委员会、查决策。GUI 只做辅助调试。
---

## 它在做什么

每天早上，4 个独立 LLM 各自看不同维度，互相 challenge 后给出建议：

- **Macro Strategist** 看宏观（VIX / 利率 / 汇率）
- **Quant Analyst** 看技术面（RSI / 多周期分位 / 趋势），不知道你的持仓
- **Risk Officer** 看风控（集中度 / 浮盈缓冲 / 尾部损失），不知道技术信号
- **Round 2**：Quant 和 Risk 互看对方报告，调整观点
- **CIO** 综合所有人发言，输出 BUY / ACCUMULATE / HOLD / TRIM / SELL + 置信度

输出一份 Markdown memo。系统不会自动下单——决策仍归你。

---

## 安装

最简单的方式：装成 Claude Code 的 skill，让 Claude 帮你 onboard：
```bash
Hi, Claude, 帮我安装https://github.com/longsizhuo/openInvest/tree/main/skills/invest 这个Skill
```
也可以手动导入：
```bash
git clone https://github.com/longsizhuo/openInvest.git ~/openInvest
bash ~/openInvest/skill/install.sh
```

回 Claude Code 对话里说："帮我初始化 invest"。Claude 会：

1. 检测 memory / .env 缺失
2. 用 5 个问题问你的情况（姓名 / 风险偏好 / 月收入 / 当前持仓 / 可选 API key）
3. 写入配置 + 跑数据迁移
4. 直接验证持仓

之后任何时候说"看看我的持仓" / "分析一下黄金" / "该不该加仓 X"，Claude 会调委员会给你 memo。

> 💡 **API key 是可选的**。Skill 模式下委员会用 Claude 跑，不需要 API 消耗。
> 想后台自动跑（cron 日报 / 任意非 Claude agent 调用）才需要注册。

**其他装法**（Docker / 手动 Python / 自带 GUI）：见 [QUICK_START.md](docs/QUICK_START.md)。

---

## 配置其他 LLM Provider

默认用 DeepSeek。要换千问 / 智谱 / Kimi 等任何 OpenAI 兼容接口，改 `.env`：

```env
# === DeepSeek（默认） ===
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash

# === 千问（Aliyun DashScope） ===
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode
LLM_MODEL=qwen-max

# === 智谱 AI ===
LLM_API_KEY=xxx
LLM_BASE_URL=https://open.bigmodel.cn/api/paas
LLM_MODEL=glm-4-flash
```

注意：**model name 必须改**（不是只改 API key + base_url），否则会 400
"model not found"。每家 provider 的 model 名都不一样（`qwen-max` / `glm-4-flash` /
`moonshot-v1-8k` 等），上对应官网查。

`LLM_*` 系列变量是新的通用配置（推荐）；老的 `DEEPSEEK_*` 保留向后兼容，
现存 `.env` 不需要迁移。两组都没设时 `LLM_API_KEY` 会自动回落到 `DEEPSEEK_API_KEY`。

---

## 设计理念

三个核心选择，每个都有具体技术后果。详见 [docs/wiki/01-architecture.md](docs/wiki/README.md)：

1. **Coordinator-Worker，不是 prompt 塞 4 个人格** — 4 个独立 LLM session，按 DAG 跑，信息隔离在 `core/committee/` 显式控制
2. **Markdown 就是数据库** — frontmatter + body，Python 和 LLM 看到的永远一致；fcntl + atomic write 双保险
3. **OpenClaw 风格 Dreaming Memory** — 三阶段记忆整合，把跨日交易模式凝固成 insight 注入下次决策

---

## 架构与扩展

```
agents/    4 个角色的 prompt
core/      Coordinator-Worker 编排 + memory store
jobs/      APScheduler cron tasks（含 event_watch 事件感知层）
connectors/web_api.py + skill/ 两条调用入口
services/  news_sources / event_normalizer / event_notifier / notifier
db/        SQLite WAL（trades / insights / market data / events）
docs/wiki/ 完整架构文档 + ADR
```

**事件层（第一层）**（实现中，默认关）：盘中每 30 分钟扫多源新闻（DDGS + RSS + yfinance），flash 归一化成结构化事件落 `db/events.db`，命中用户持仓 / target_assets 则邮件通知 + 触发委员会重跑。开关：`jobs/event_watch.yml` 的 `enabled: true` + env `DEEPSEEK_API_KEY` + `EMAIL_SENDER`。详见 [ADR-006](docs/wiki/adr/006-event-layer.md)。

详见：
- [架构总览](docs/wiki/01-architecture.md) | [4 角色 prompt 解析](docs/wiki/02-agents.md) | [Dreaming](docs/wiki/03-dreaming.md)
- [双执行路径 — Coordinator vs Direct](docs/wiki/04-execution-paths.md)
- [数据模型 v2](docs/wiki/05-data-model.md) | [Web API](docs/wiki/06-api.md)
- [扩展指南](docs/wiki/07-extending.md) | [故障排查](docs/wiki/09-troubleshooting.md)
- [ADR — 关键决策记录](docs/wiki/adr/)

---

## 免责

LLM-driven 决策辅助工具。**不构成投资建议**。LLM 会出错、会过度自信、会漏看东西。

系统不会自动下单。建议先用 `what_if` 在小金额上跑两周再上真仓。

公开命中率数据 / PnL 曲线 / 跑赢事件均为作者本人账户历史记录，**过去表现不预示未来收益**。

**回测局限**：`scripts/backtest_runner.py` 跑的 paper-trading walk-forward 默认拒绝
decision_date > 2024-06-30 的回测（强制跑加 `--allow-lookahead`）。原因：
DeepSeek-Chat 训练数据估算截止约 2024-06-30，模型已经"见过"那段时间的市场，
对 2024 下半年的 backtest verdict 含 **LLM 训练数据 lookahead bias**——结果
**仅作上限估计**，训练 / 调参信号请用 2024-06-30 之前的日期范围。

---

## 致谢

- [OpenClaw Dreaming Guide](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e) — 三阶段记忆架构
- [Claude Code](https://claude.com/claude-code) — Skill 模式 Coordinator 实现

PR / Issue 欢迎。
