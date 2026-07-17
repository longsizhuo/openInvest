<div align="center">
  
<img width="120" height="120" alt="owl-02-lineart-gold" src="https://github.com/user-attachments/assets/e4c1efa0-026a-4777-ae62-48b9b0be435c" />

# openInvest

**面向现代 AI Agent 的自托管投资决策引擎。多 Agent 信息隔离与交叉质询协议，提供可审计的决策追踪流水（Audit Trail）。**

[![Python](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Agents](https://img.shields.io/badge/Agents-Claude%20Code%20%7C%20Codex%20%7C%20Hermes%20%7C%20OpenClaw-informational)](docs/wiki/20-agent-usage-tutorial.md)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/longsizhuo/openInvest?style=social)](https://github.com/longsizhuo/openInvest)
[![Glama MCP server](https://glama.ai/mcp/servers/longsizhuo/openInvest/badges/score.svg)](https://glama.ai/mcp/servers/longsizhuo/openInvest)

[📚 完整架构 Wiki](docs/wiki/README.md) · [🇺🇸 English Version](README.md)

</div>

---

## OpenInvest 是什么？

OpenInvest 是一个面向现代 AI Agent 的自托管投资决策引擎。

它提供可验证的投资委员会、基于证据的推理、长周期回测、可审计的决策记录，并通过标准接口服务于 Claude Code、Codex、Hermes、OpenClaw 等 Agent。它并非旨在取代这些 Agent，而是为了赋能它们。

---

## 生产环境性能指标 (Live Performance & PnL)

<div align="center">
  <img src="https://raw.githubusercontent.com/longsizhuo/openInvest/pnl-data/docs/pnl_chart.svg" alt="PnL chart" width="100%"/>
  <sub>数据流基于 `jobs/pnl_snapshot` 每 2 小时原子替换自动更新至 <a href="https://github.com/longsizhuo/openInvest/tree/pnl-data">pnl-data 分支</a></sub>
  <br/>
  <sub>上半图：30天净值趋势 · 下半图：相较于 8 类基准资产的净值对照（透明披露，<b>非 alpha 主张</b>——委员会的可证价值是纪律与透明，不是超额收益，见 <a href="docs/wiki/adr/023-honest-positioning-not-alpha.md">ADR-023</a>）</sub>
  <br/>
  <sub>📌 <b>注</b>：当前图表呈现为作者生产环境账户。自托管部署后，系统将依据您在 `memory/` 中定义的专属账户持仓自动渲染对应的净值曲线。</sub>
</div>

<!-- OUTPERFORM_FEED_START -->
<!-- OUTPERFORM_FEED_END -->

*   **基准对比组合 (Benchmarks)**：系统跨越 4 大象限（AI投顾 / 公募基金 / 储蓄理财 / 大盘指数）引入 8 条标准控制基准。严格的对比方法论与数据清洗逻辑参阅 [docs/wiki/README.md](docs/wiki/README.md)。

---

## 研究与证伪

**系统统计自我披露**：本系统是**消除人类投资认知偏差、强化推理透明度**的审计工具，而非收益放大黑盒。最新自动审计（`docs/verdict_accuracy.md`）：方向性 Verdict（剔除 HOLD）真实命中率 **42.2%**（n=56，**低于随机**）；`HOLD`（不作为）占 **56%**。即系统价值在透明/纪律（多数时候不作为、低换手），**不在方向预测**。完整流水详见 [docs/verdict_accuracy.md](docs/verdict_accuracy.md)。

本项目对自己的 edge 做系统性证伪，负结果照常发布。委员会读取的确定性特征、及其周边的择时信号族，均经过预注册统计闸检验——无一作为可交易 alpha 存活。

| 测试 | 结果 | 判定 |
|---|---|---|
| Q1 横截面选股 | 6 特征 mean-IC 0.025–0.067，Holm 校正后 **p=0.397** | 无显著选股信号 |
| M1 多变量 GBM（OOS） | mean OOS IC **+0.003**，p=0.925 | 特征组合亦无信号 |
| Q2 黄金 MA200 趋势 | **p_holm=0.016** 显著——但 `trend_dca` 证明是 **beta 而非可交易 alpha**：择时终值 **3.07 vs 买持 15.10**，Sharpe **+0.36 vs +0.68**，maxDD 反而更深（**−57% vs −44%**） | 统计显著，经济上不可交易 |
| 每资产多信号族 | 3 资产 × 4 信号族 × 参数网格 = 每资产 24 变体，扣成本 + DSR deflate 后**无一过 DSR > 0.95** | 任何信号族均无可交易信号 |
| 正向对照 | 作弊完美择时信号 **DSR = 1.00** | 证明 harness 能识别真信号 |

方法论：Newey-West HAC t、Deflated Sharpe（Bailey & López de Prado 2014，逐式复算）、Holm 校正、0 前视、LLM cutoff 探针。

详见 [experiments/signal-eval/README.md](experiments/signal-eval/README.md) · [docs/verdict_accuracy.md](docs/verdict_accuracy.md) · [ADR-022](docs/wiki/adr/022-backtest-memory-contamination-and-holdout-discipline.md) · [ADR-023](docs/wiki/adr/023-honest-positioning-not-alpha.md)

---

## 产品理念

多数 AI 投资产品都想成为更聪明的聊天机器人。OpenInvest 选择构建一个透明、可验证、可审计的决策引擎，接入 Claude Code、Codex、Hermes、OpenClaw 等个人 Agent——这些 Agent 的每一次进步，都自动使 OpenInvest 更强大。

分工是刻意设计的：你的 Agent 负责长期记忆、自然语言交互和用户理解；OpenInvest 负责可验证的投资委员会、基于证据的推理、长周期回测与可审计的决策记录。

```
                   User
                     │
         ┌───────────┴───────────┐
         ▼                       ▼
    Your Agent             OpenInvest
(User Understanding)   (Market Understanding)
         │                       │
         └───────────┬───────────┘
                     ▼
            Better Investment Decisions
```

> **Agent 理解你。OpenInvest 理解投资。**

### 避免占有用户
OpenInvest 刻意避免“占有”用户。大多数 AI 产品都试图占有一切——记忆、角色、聊天记录和工作区。OpenInvest 则选择退居幕后。它提供简洁的 API、CLI 命令和 agent 技能（Claude Code / Codex / Hermes / OpenClaw），让您的主智能体管理对话和上下文，而 OpenInvest 仅为底层的投资智能提供支持。

---

## 系统特性

*   **多 Agent 投资委员会**：独立分析与 Round 2 交叉质询辩论机制。
*   **Coordinator-Worker 架构**：硬编码上下文隔离，防止多角色注意力污染与幻觉。
*   **信息隔离契约**：严格阻断量化与风控分析师获取边界外的上下文。
*   **可审计决策流水**：清晰详尽的记录，还原每一次决策产生的因果关系。
*   **Markdown 即数据库**：Frontmatter (YAML) + Markdown (Body) 作为唯一运行时事实来源。
*   **长周期回测系统**：内置测试沙箱，具备严格的先知偏差（Lookahead Bias）防护。
*   **三阶段 Dreaming 记忆固化**：每日凌晨自动蒸馏复盘，将行为洞察固化回长期记忆。
*   **零成本自托管**：推理计算完全依托宿主 Agent，无需消耗个人的第三方 API 额度。
*   **Agent Skill**：轻量化 Skill 插件（Claude Code / Codex / Hermes / OpenClaw），内置交互式引导配置向导。
*   **自动化 GitHub Actions**：每日定时运行委员会并自动向指定邮箱发送决策日报。

---

## 快速开始

### 1. 接入你的 Agent（推荐）
通过所用 Agent 的插件 Marketplace 动态加载轻量化 Skill，宿主 Agent 在首次运行时会自动拉取核心代码并完成 `uv sync` 环境依赖对齐：
```bash
# Claude Code
/plugin marketplace add longsizhuo/openInvest
/plugin install invest@openinvest

# Codex
codex plugin marketplace add longsizhuo/openInvest

# Hermes Agent
hermes plugins install longsizhuo/openInvest --enable

# OpenClaw
openclaw plugins install clawhub:openinvest
```
其余任意 MCP client：按下方第 2 步注册 MCP server（完整教程见 [agent 使用教程](docs/wiki/20-agent-usage-tutorial.md)）。

### 2. 独立使用 —— MCP server 或 CLI（无需 clone）
后端已发布至 [PyPI](https://pypi.org/project/openinvest/)，`~/openInvest` 只存放你的数据：
```bash
# MCP（18 个工具，任意 MCP client；加 --http 可启动 remote streamable-HTTP server —— BETA）
claude mcp add openinvest -e INVEST_HOME=~/openInvest -- uvx openinvest-mcp

# 或直接用 CLI
INVEST_HOME=~/openInvest uvx openinvest status
```
在支持 Skill 的 AI 终端中发送 `帮我初始化 invest`（或 `set up invest`）。系统将触发交互式 Bootstrap 向导，指导完成以下初始化：
1. 检测 `memory/` 状态存储路径及 `.env` 环境变量契约。
2. 5 维度画像引导（合规法律名 / 风险容量 / 偿债结构 / 初始持仓资产 / 可选第三方密钥）。
3. 运行静态数据迁移，即刻生成首份实时资产暴露 memo。

> 💡 **零成本运行声明**：在内置 Skill 交互模式下，委员会的底层推理完全依托宿主 Agent（如 Claude Code）的推理管道，**无需消耗您个人的第三方 API Key 额度**。仅在配置 Cron 独立日报任务或运行外部服务调用时，才需声明底层 API 供应。

自托管细节参阅 [docs/QUICK_START.md](docs/QUICK_START.md)。（内置 Web GUI 已于 2026-07-05 退役——全部能力经 CLI/MCP 暴露；独立前端后续可能回归。）

### 3. Serverless 自托管（GitHub Actions）
每天由 GitHub Actions 自动跑委员会并把报告发到你邮箱。
> ⚠️ **fork 必须设为 private**：运行状态（持仓、决议）会被 commit 回你的 fork，含真实持仓数据。public fork 会泄露隐私。

1. **Fork 本仓库**（Settings → 改为 Private）。
2. 本地 `帮我初始化 invest` 生成初始 `memory/`，然后 commit 并推到你的私有 fork：
   ```bash
   git add -f memory/ && git commit -m "chore: init memory state" && git push
   ```
3. fork 的 **Settings → Secrets and variables → Actions** 填：
   *   `LLM_API_KEY` 或 `DEEPSEEK_API_KEY`：跑委员会的 LLM Key。
   *   `EMAIL_SENDER` / `EMAIL_PASSWORD`：Gmail 发信地址 + [应用专用密码](https://support.google.com/accounts/answer/185833)。
   *   `DIGEST_EMAIL_TO`：收件邮箱。
4. **Actions 标签页 → 启用 workflow**。默认每天 10:00（北京）跑；也可在 `daily-report` 里手动 **Run workflow** 立即触发。

---

## 系统拓扑与多 Agent 编排机制

`openInvest` 拒绝在单一 LLM Session 中通过多角色 Prompt 进行伪辩论。系统在 `core/committee/` 层强行实施**信息隔离契约（Information Isolation Contract）**，通过有向无环图（DAG）驱动 4 个完全独立的 LLM 进程进行多轮交叉质询：

```
                [ 宏观数据注入 ]
                       │
             ▼ 1. 宏观对齐边界 (Context)
         ┌──────────────────────────┐
         │    Macro Strategist      │ (审视 VIX / 利率分位 / 汇率动量)
         └─────────────┬────────────┘
                       │
             ▼ 2. 多维度异步质询管道 (Async DAG)
         ┌─────────────┴────────────┐
         ▼                          ▼
 ┌──────────────────────────┐  ┌──────────────────────────┐
 │      Quant Analyst       │  │       Risk Officer       │
 │   (RSI / 技术指标动量)   │  │ (资产集中度/VaR/风险敞口) │
 │                          │  │                          │
 │ 🛑 状态盲区：不知晓持仓   │  │ 🛑 状态盲区：不知晓技术面 │
 └────────────┬─────────────┘  └────────────┬─────────────┘
 │                             │
 └─────────────┬───────────────┘
 │
 ▼ 3. Round 2 Rebuttal 交叉质询层
 │ 相互注入对方 Round 1 报告进行修正与碰撞
 ▼
 ┌────────────────────────────────────────────────────────┐
 │               Chief Investment Officer (CIO)           │
 └──────────────────────────┬─────────────────────────────┘
 │
 ▼ 4. 确定性原语落盘 (Atomic Markdown)
 [ BUY / ACCUMULATE / HOLD / TRIM / SELL ]
```

1.  **Macro Strategist (宏观策略官)**：评估系统宏观环境（VIX / 期限利差 / 核心汇率矩阵），设定全局风险基调。
2.  **Quant Analyst (量化分析师)**：纯粹的数学动量与技术指标过滤器。**严格被阻断在持仓 Context 之外**，从根源消除人类在亏损持仓时的心理高估。
3.  **Risk Officer (风险控制官)**：专注于组合尾部风险（最大回撤缓冲、流动性集中度、Solvency 杠杆因子）。**严格被阻断在技术信号之外**，仅对资产暴露进行冷酷裁决。
4.  **Round 2 Rebuttal (交叉辩论机制)**：Quant 与 Risk 在第二轮被强制注入对方的 Round 1 报告，进行边界碰撞，直到两个 Agent 的信号和强度达成收敛，或触发收敛安全阀。
5.  **CIO (首席投资官)**：汇总经过多轮过滤的质询追踪，输出结构化 `Verdict`（BUY / ACCUMULATE / HOLD / TRIM / SELL）与置信度。系统保持强克制，**不具备任何自动下单原语**，最终执行交由人类审计。

这套设计背后的关键取舍以 ADR 形式记录于 [docs/wiki/adr/](docs/wiki/adr/)（当前 24 条），其中包含推翻自身早期设计的决议——[ADR-007](docs/wiki/adr/007-few-shot-retirement.md) 将 few-shot CIO 路线退役，[ADR-009](docs/wiki/adr/009-no-ta-style-analyst-agents.md) 依预注册实验否决了 TA 风格分析师扩展。

---

## 核心设计理念

*   **Coordinator-Worker 架构**：各 Agent 具备独立的上下文字段。状态隔离逻辑在框架层通过 Python 硬编码限制，防范多角色长文本在同一上下文下的内部注意力污染。
*   **Markdown 即数据库 (Markdown-as-a-Database)**：系统采用 Frontmatter (YAML) + Body (Markdown) 作为存储与运行时的**唯一事实来源**。结合 `fcntl.flock` 文件锁与临时文件原子替换（Atomic Write）机制，利用 Git 原生支持提供不可篡改的**投资决策审计追踪流**。
*   **三阶段 Dreaming 记忆固化**：基于类似 OpenClaw 风格。每日凌晨系统自动解冻历史决策，对比真实市场 Outcome 进行复盘归纳（浅睡 $\rightarrow$ REM $\rightarrow$ 深睡），将行为洞察固化回长期记忆，防止大模型发生跨日长窗口上下文漂移。

---

## 配置

系统默认采用 DeepSeek 端点，支持任何标准 OpenAI 兼容 API。LLM provider 配置与全部运行时可调参数（[ADR-017](docs/wiki/adr/017-config-via-api.md)）参阅 [docs/wiki/22-configuration.md](docs/wiki/22-configuration.md)。

---

## 严谨性声明与回测局限说明

1. **无商业顾问要约**：本系统仅为大语言模型驱动的决策辅助工具。输出的 Markdown 备忘录属于基于确定性输入的模拟推理，不构成任何特定的资产配置与投资建议。
2. **回测时间锁与过拟合防范**：`scripts/backtest_runner.py` 内置硬编码安全阀。**默认全面拒绝 `decision_date > 2024-06-30` 的任何回测请求**（若强行越界，需显式声明 `--allow-lookahead`）。由于目前主流基础模型的训练语料库截止于 2024 年中，对其后时段的回测将不可避免地混入**模型训练的先知偏差（Lookahead Bias）**。任何调参、超参数 Sweep（Optuna 实验）及 Prompt 评估必须严格在 2024-06-30 之前的历史区间内运行。

---

## 开源致谢

* [MiMo](https://mimo.mi.com/) — 感谢 MiMo 量化实验室提供的生产级高性能推理算力赞助（支持 `mimo-v2.5-pro` 长期并发回测）。
* [OpenClaw Dreaming Guide](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e) — 系统三阶段记忆固化与蒸馏架构的基础理论来源。

---

## 开源协议

本项目采用 MIT 开源协议 - 详情见 [LICENSE](LICENSE) 文件。
