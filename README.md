<div align="center">
  
<img width="120" height="120" alt="owl-02-lineart-gold" src="https://github.com/user-attachments/assets/e4c1efa0-026a-4777-ae62-48b9b0be435c" />

# openInvest

**基于 Coordinator-Worker 架构的自托管 AI 投资决策委员会系统。多大语言模型角色信息隔离质询，提供可审计的投资决策追踪流水（Audit Trail）。**

[![Python](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Claude Code](https://img.shields.io/badge/Skill-Claude%20Code-D97757?logo=anthropic&logoColor=white)](https://claude.com/claude-code)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/longsizhuo/openInvest?style=social)](https://github.com/longsizhuo/openInvest)
[![zread](https://img.shields.io/badge/Ask_Zread-_.svg?style=for-the-badge&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4OTEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTISOWwgNS42MDAxIDUuNjAxNTYgNS4zMTM1NiA1LjYwMTU2IDQuOTYwMVYyLjI0MDFDNS42MDE1NiAxLjg4NjY0IDUuMzE1MDIgMS42MDAxIDQuOTYxNTYgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8pGF0aCBkPSJNNC45NjE1NiAxMC4zOTk5SDIuMjQxNTZDMS44ODgxIDEwLjM5OTkgMS42MDE1NiAxMC42ODY0IDEuY0AxNTYgMTEuMDM5OVYxMy43NTk5QzEuNjAxNTYgMTQuMTE0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8+CjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8pGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8+CjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff)](https://zread.ai/longsizhuo/invest)

[📚 完整架构文档](docs/wiki/README.md)

</div>

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

*   **基准对比组合 (Benchmarks)**：系统跨越 4 大象限（AI投顾 / 公募基金 / 储蓄理财 / 大盘指数）引入 8 条标准控制基准。严格的对比方法论与数据清洗逻辑参阅 [docs/wiki/03-benchmarks.md](docs/wiki/README.md)。
*   **系统统计自我披露**：本系统是**消除人类投资认知偏差、强化推理透明度**的审计工具，而非收益放大黑盒。最新自动审计（`docs/verdict_accuracy.md`）：方向性 Verdict（剔除 HOLD）真实命中率 **42.2%**（n=56，**低于随机**）；`HOLD`（不作为）占 **56%**；含 HOLD 的 7d 命中率 70.7% 系"HOLD 算 hit"灌水。即系统价值在透明/纪律（多数时候不作为、低换手），**不在方向预测**。完整流水详见 [docs/verdict_accuracy.md](docs/verdict_accuracy.md)。

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
│  (RSI/多周期分位/技术指标) │  │ (资产集中度/VaR/浮盈缓冲) │
│                          │  │                          │
│ 🛑 状态盲区：不知晓持仓   │  │ 🛑 状态盲区：不知晓技术面 │
└────────────┬─────────────┘  └────────────┬─────────────┘
│                             │
└─────────────┬───────────────┘
│
▼ 3. Round 2 Rebuttal 交叉质询层
│ 相互注入对方 Round 1 报告进行修正
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
5.  **CIO (首席投资官)**：汇总经过多轮过滤的质询追踪，输出结构化 `Verdict`（BUY / ACCUMULATE / HOLD / TRIM / SELL）与置信度。系统保持强克制，**不具备任何自动自动下单原语**，最终执行交由人类审计。

---

## 核心设计理念

*   **Coordinator-Worker 架构**：各 Agent 具备独立的上下文字段。状态隔离逻辑在框架层通过 Python 硬编码限制，防范多角色长文本在同一上下文下的内部注意力污染。
*   **Markdown 即数据库 (Markdown-as-a-Database)**：系统采用 Frontmatter (YAML) + Body (Markdown) 作为存储与运行时的**唯一事实来源**。结合 `fcntl.flock` 文件锁与临时文件原子替换（Atomic Write）机制，利用 Git 原生支持提供不可篡改的**投资决策审计追踪流**。
*   **三阶段 Dreaming 记忆固化**：基于类似 OpenClaw 风格。每日凌晨系统自动解冻历史决策，对比真实市场 Outcome 进行复盘归纳（浅睡 $\rightarrow$ REM $\rightarrow$ 深睡），将行为洞察固化回长期记忆，防止大模型发生跨日长窗口上下文漂移。

---

## 生产环境快速部署

### 1. 接入 Claude Code 运行时（推荐）
通过官方 Marketplace 动态加载轻量化 Skill，宿主 Agent 在首次运行时会自动拉取核心代码并完成 `uv sync` 环境依赖对齐：
```bash
/plugin marketplace add longsizhuo/openInvest
/plugin install invest@openinvest

```

### 2. 独立 Skill 原生导入

```bash
git clone [https://github.com/longsizhuo/openInvest.git](https://github.com/longsizhuo/openInvest.git) ~/openInvest
bash ~/openInvest/skills/install.sh

```

在支持 Skill 的 AI 终端中发送 `帮我初始化 invest`。系统将触发交互式 Bootstrap 原语，指导完成以下初始化：

1. 检测 `memory/` 状态存储路径及 `.env` 环境变量契约。
2. 5 维度画像引导（合规法律名 / 风险容量 / 偿债结构 / 初始持仓资产 / 可选第三方密钥）。
3. 运行静态数据迁移，即刻生成首份实时资产暴露 memo。

> 💡 **零成本运行声明**：在内置 Skill 交互模式下，委员会的底层推理完全依托宿主 Agent（如 Claude Code）的推理管道，**无需消耗您个人的第三方 API Key 额度**。仅在配置 Cron 独立日报任务或运行外部服务调用时，才需声明底层 API 供应。

更多部署矩阵（Docker 容器化、本地独立 Web GUI 调试端口）参阅 [QUICK_START.md](https://www.google.com/search?q=docs/QUICK_START.md)。

### 3. 零成本自托管（GitHub Actions，无需服务器）

不想挂机器跑 cron？fork 一份，每天由 GitHub Actions 自动跑委员会并把报告发到你邮箱。

> ⚠️ **fork 必须设为 private**：运行状态（持仓、决议）会被 commit 回你的 fork，含真实持仓数据。public fork 会泄露隐私。

1. **Fork 本仓库**（Settings → 改为 Private）。
2. 本地 `帮我初始化 invest` 生成 `memory/`，然后 `git add -f memory/ && git commit && git push` 推到你的私有 fork（Actions 靠它读你的持仓）。
3. fork 的 **Settings → Secrets and variables → Actions** 填：

   | Secret | 说明 |
   |---|---|
   | `LLM_API_KEY` 或 `DEEPSEEK_API_KEY` | 跑委员会的 LLM Key |
   | `EMAIL_SENDER` / `EMAIL_PASSWORD` | Gmail 发信地址 + [应用专用密码](https://support.google.com/accounts/answer/185833) |
   | `DIGEST_EMAIL_TO` | 收件邮箱 |

4. **Actions 标签页 → 启用 workflow**。默认每天 10:00（北京）跑；也可在 `daily-report` 里手动 **Run workflow** 立即触发。

每次运行后更新的 `memory/` 自动 commit 回 fork —— 你的决策历史天然进 git，可回溯。

---

## 底层 LLM Provider 配置契约

系统默认采用高性价比的 DeepSeek 系列端点。如需替换为任何标准的 OpenAI 兼容架构（如通义千问、智谱等），必须严格遵循 `.env` 的配置契约（务必确保 `LLM_MODEL` 的官方真实 ID 映射，不可仅改 URL）：

```env
# === 选项 A: DeepSeek (架构默认) ===
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx──────────────
LLM_BASE_URL=[https://api.deepseek.com](https://api.deepseek.com)
LLM_MODEL=deepseek-v4-flash

# === 选项 B: 通义千问 (Aliyun DashScope 兼容模式) ===
LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_BASE_URL=[https://dashscope.aliyuncs.com/compatible-mode](https://dashscope.aliyuncs.com/compatible-mode)
LLM_MODEL=qwen-max

# === 选项 C: 智谱 AI (GLM API 兼容端点) ===
LLM_API_KEY=xxxxxxxxxxxxxxxx.xxxxxxxxx
LLM_BASE_URL=[https://open.bigmodel.cn/api/paas](https://open.bigmodel.cn/api/paas)
LLM_MODEL=glm-4-flash

```

*注：系统内部维护向下兼容逻辑，原旧版 `DEEPSEEK_*` 专属环境变量无缝回落至新的 `LLM_*` 通用标准空间，生产环境无须强制迁移。*

---

## 运行时委员会行为调整原语

openInvest 提供了极高内聚的运行时参数配置开关。依据 [ADR-017](https://www.google.com/search?q=docs/wiki/adr/017-config-via-api.md)，以下 Tunable 变量在 Web GUI / API / CLI / env 四个通道具备完全等价的覆盖优先级，并持久化于 `memory/.state/config_overrides.json` 中：

| 统一配置键 (Config Key) | 数据类型 & 默认值 | 工程行为后果描述 |
| --- | --- | --- |
| `verdict.concentration_lens_enabled` | `bool` (`true`) | **持仓集中度过滤器**。默认为开：当单一标的暴露过高时强行触发安全减仓阈值。若关闭，则单资产或全额风险池将**不因过度集中而被 CIO 建议减仓**（但波动率、估值风险、最大回撤风控依旧生效）。详见 [ADR-019](https://www.google.com/search?q=docs/wiki/adr/019-remove-solvency-concentration-override.md) |
| `verdict.risk_profile` | `str` (`"steady"`) | 风险偏好特征描述符。`steady`（稳健稳健）/ `aggressive`（在 Downtime 阶段允许激活高顺势加仓弹性）。 |
| `verdict.gold_defense_dca_enabled` | `bool` (`true`) | 黄金防御机制。在 VIX / ATR 骤增阶段，强行将单次大额加仓原语拆分为多期 DCA（分批放行）。 |
| `dca.auto_dca_enabled` | `bool` (`false`) | 全自动定期定投决策开关。 |
| `dca.auto_dca_amount_cny` | `float` (`0.0`) | 触发自动定投时的单期基准人民币资本配置额度。详见 [ADR-018](https://www.google.com/search?q=docs/wiki/adr/018-dca-dip-reserve.md) |

### 参数运行时重写（以关闭集中度 Lens 为例）

```bash
# 途径 1: 使用 CLI 原语重写
uv run python scripts/skill.py config --set verdict.concentration_lens_enabled false

# 途径 2: 通过运行时 REST API 注入
curl -X PUT localhost:8765/api/config -d '{"key":"verdict.concentration_lens_enabled","value":false}'

# 途径 3: 宿主 GUI 交互控制
# 前往 invest-gui「Settings → Committee Configuration」面板直接热切换

```

---

## 严谨性声明与回测局限说明

1. **无商业顾问要约**：本系统仅为大语言模型驱动的决策辅助工具。输出的 Markdown 备忘录属于基于确定性输入的模拟推理，不构成任何特定的资产配置与投资建议。
2. **回测时间锁与过拟合防范**：`scripts/backtest_runner.py` 内置硬编码安全阀。**默认全面拒绝 `decision_date > 2024-06-30` 的任何回测请求**（若强行越界，需显式声明 `--allow-lookahead`）。由于目前主流基础模型的训练语料库截止于 2024 年中，对其后时段的回测将不可避免地混入**模型训练的先知偏差（Lookahead Bias）**。任何调参、超参数 Sweep（Optuna 实验）及 Prompt 评估必须严格在 2024-06-30 之前的历史区间内运行。

---

## 核心系统代码库纵览

```
agents/      # 多角色决策 Prompt 与行为契约定义
core/        # 核心协调器（Coordinator）编排层、持久化文件文件锁与状态总线
jobs/        # 定时自动化任务体系（基于 APScheduler，含 event_watch 感知层）
connectors/  # 外部协议桥接（FastAPI Web API / 终端交互 Skill 适配）
services/    # 基础公共服务群（新闻多源同步 / 结构化事件清洗 / 消息分发）
db/          # 高性能 SQLite WAL 预写日志数据库群（交易流水/Insight记忆/市场特征）
docs/wiki/   # 完整的架构设计记录（ADR）与分布式设计原理解析
```

---

## 开源致谢

* [MiMo](https://mimo.mi.com/) — 感谢 MiMo 量化实验室提供的生产级高性能推理算力赞助（支持 `mimo-v2.5-pro` 长期并发回测）。
* [OpenClaw Dreaming Guide](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e) — 系统三阶段记忆固化与蒸馏架构的基础理论来源。
