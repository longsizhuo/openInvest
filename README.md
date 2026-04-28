<div align="center">

# 🤖 invest — Your AI Investment Committee

### *4 个 AI 专家、1 个交易员、0 失眠夜*

**让 Coordinator-Worker 多智能体替你开投资委员会，每天 6 分钟，跨资产、跨周期、不漏一个利空。**

[![Python](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-5C2D91?logo=openai&logoColor=white)](https://deepseek.com)
[![Claude Code](https://img.shields.io/badge/Skill-Claude%20Code-D97757?logo=anthropic&logoColor=white)](https://claude.com/claude-code)
[![OpenClaw Memory](https://img.shields.io/badge/Memory-OpenClaw-success)](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e)
[![APScheduler](https://img.shields.io/badge/Cron-APScheduler-blue)](https://apscheduler.readthedocs.io/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](#)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#)

[**🚀 Quick Start**](#-quick-start) ·
[**🧠 Architecture**](#-architecture) ·
[**🛡️ Why Production-Grade**](#%EF%B8%8F-为什么是-production-grade-不是-toy-project) ·
[**🪄 Claude Code Skill**](#-claude-code-skill-把-invest-装进-claude)

</div>

---

## ⚡ 30 秒看懂这是什么

> **传统**：你订阅几个公众号 / 看 Bloomberg / 自己查 RSI → 半小时后还在纠结要不要 buy
>
> **invest**：cron 触发 → **Macro / Quant / Risk Officer / CIO 4 个 LLM 各自出报告并 cross-challenge** → 邮箱里收到 1 份带置信度的投资委员会备忘录 → 你决定是否执行

它**不是聊天机器人**，是一个**有持久状态、能记住你 90 天交易模式、跨进程并发安全的 production agent system**。

```
                  ┌──────────────────────────────────────────┐
                  │   APScheduler  (cron 03:00 / 09:30 ...)  │
                  └─────────────┬────────────────────────────┘
                                │ trigger
       ┌────────────────────────▼─────────────────────────┐
       │                Investment Committee              │
       │                                                  │
       │   🌐 Macro Strategist ─┐                         │
       │   📊 Quant Analyst ────┼─→ cross-challenge ──→ 🎩 CIO Memo
       │   🛡️ Risk Officer  ───┘   (Round 2)             │
       └────────────┬─────────────────────────────────────┘
                    │ persist + email
       ┌────────────▼────────────┐  ┌──────────────────────────┐
       │  memory/.committee/     │  │  📧 Gmail report         │
       │  memory/daily/*.md      │  │  📱 NapCat /cmd 接口     │
       │  memory/.dreams/*       │  │  🪄 Claude Code Skill    │
       └─────────────────────────┘  └──────────────────────────┘
```

---

## 🌟 核心卖点

<table>
<tr>
<td width="50%" valign="top">

### 🧠 真 · 多智能体投资委员会

不是"prompt 里塞 4 个 persona"，是 **真 · Coordinator-Worker 模式** —— 4 个独立 LLM session，信息严格隔离：

- **Macro Strategist** 看宏观（VIX / TNX / USDCNY），跨资产共享
- **Quant Analyst** 看技术面（RSI / 多周期分位 / 趋势），不知道用户持仓
- **Risk Officer** 看风控（集中度 / 浮盈缓冲 / 尾部损失），不知道技术信号
- **Round 2 cross-challenge**：Quant 和 Risk 互看对方报告调整观点
- **CIO** 综合所有，给 BUY/ACCUMULATE/HOLD/TRIM/SELL + confidence

</td>
<td width="50%" valign="top">

### 💤 OpenClaw-style Dreaming Memory

借鉴 [OpenClaw](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e) 的"AI 也要做梦"理念，每天凌晨 03:00 跑三阶段记忆整合：

- **Light Sleep** — 摄入近 90 天交易 + 多 symbol 行情上下文，提取信号
- **REM Sleep** — 找跨时间重复模式，输出候选 insight
- **Deep Sleep** — 阈值门 (`score≥0.8` / `count≥3`) 通过 → 写入 `insights/` + 索引

LLM 不再"金鱼记忆"——你 6 个月前的过度集中持仓，今天的 Risk Officer 看得见。

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📝 Markdown-as-Truth 持久化

抛弃 `user_profile.json` 单文件 + 全量加载。改用 **frontmatter + Markdown** 双向通道：

- **Frontmatter** = 结构化数据（代码读写）
- **Body** = 自然语言版（LLM 直接读）
- 同一份 `portfolio.md` —— Python 改完，LLM 拿到的也是最新状态
- `fcntl.flock` + atomic `tmp → fsync → os.replace` 双保险

```yaml
---
cash_cny: 18290.51
gold_grams: 123.92
gold_avg_cost_cny_per_gram: 1008.79
ndq_shares: 128
---
# 当前持仓
- CNY 现金: ¥18,290.51
- 黄金: 123.92 克 ...
```

</td>
<td width="50%" valign="top">

### 🪄 双 LLM 模式 · 一份代码

同一套 `agents/`、同一套 `core/committee.py`、同一套 prompts —— 跑哪个 LLM 看你心情：

- **DeepSeek** (cron 模式)：每天 daily_report 自动跑，省 token
- **Claude** (skill 模式)：在 Claude Code 对话里随时召唤委员会，4 个 agent **真 async 并行**，工作流和 cron 完全等价

```bash
# Claude Code 里直接：
~/.claude/skills/invest/run.sh prepare_committee NDQ.AX
# Coordinator-Worker fan-out → 4 个 worker 并行 → CIO 综合
```

> 💡 这是 [Claude Code v2.1.88 Coordinator Mode](https://claude.com/claude-code) 的标准实现样本之一。

</td>
</tr>
</table>

---

## 🛡️ 为什么是 Production-Grade，不是 Toy Project

> 大多数 AI agent demo 写完就丢，跑两天就开始崩。invest 经过完整 audit + 5 轮硬化 commit，专治 LLM 系统的常见死法：

| 死法 | 我们的修法 | 出处 |
|---|---|---|
| 💥 进程被 kill 时 `portfolio.md` 写到一半，状态损坏 | **Atomic write**: `tmp + fsync + os.replace` 三步走 | `core/memory_store.py:_atomic_write_text` |
| 💥 NapCat 存款 + scheduler 扣款并发，有一笔凭空消失 (TOCTOU) | **单锁 RMW + `transaction()` context manager**，50 线程压测 0 丢失 | `core/memory_store.py:transaction` |
| 💥 DeepSeek 偶发 429/5xx，CIO 在空字符串上编 memo | **指数退避 + jitter retry**，区分 transient vs auth 错误 | `core/committee.py:_ask` |
| 💥 yfinance 拉不到价 → 估值返回 0 → Risk Officer "集中度爆表"建议清仓 | **`Optional[float]` + 跳过该资产**，scheduler return 标 `degraded` | `jobs/daily_report.py:_get_last_close` |
| 💥 BetaShares scraper 403 反爬 → NDQ 价完全拿不到 | **fallback yfinance**，scrape 失败保 close 价 | `utils/exchange_fee.py` |
| 💥 数据陈旧 5 天但 LLM 不知道，编今天策略 | **Staleness 阈值检测** + 注入 LLM 上下文"⚠️ 数据陈旧 N 天" | `INVEST_PRICE_STALE_DAYS` env |
| 💥 邮件 SMTP 失败静默 return，user 永远不知道日报没收到 | **`EmailDeliveryError` raise**，scheduler `job_runs` 表自动记录 | `services/notifier.py` |
| 💥 LLM 失败事件无审计，事后查不到 | **全部落 `.dreams/events.jsonl`**：`price_fetch_failed` / `price_stale` / `email_delivery_failed` | `core/memory_store.py:dream_event` |

📊 **并发压测证据**：

```
50 线程并发 cash_cny += 1 → 最终 delta = 50.0 ✅ 0 lost updates
20 轮 scheduler 扣款 + napcat 存款 race → delta 精确 = -37880 ✅ 0 lost updates
```

---

## 🚀 Quick Start

```bash
# 1. Clone + 装依赖
git clone https://github.com/longsizhuo/invest.git
cd invest && uv sync --frozen --python 3.13

# 2. 初始化你的持仓 / 策略 (memory/)
cp user_profile.example.json user_profile.json
# 编辑：填邮箱、初始现金、target_assets、风险偏好
python scripts/migrate_profile.py    # JSON → memory/{user,strategy,portfolio}.md

# 3. 凭据
cp .env.example .env
# 至少填 DEEPSEEK_API_KEY + EMAIL_SENDER + EMAIL_PASSWORD (Gmail App Password)

# 4. 选一种方式跑
python -m jobs.daily_report      # 跑一次完整委员会 (~6 min)
python -m scheduler.runner        # 全套 cron 持续跑 (推荐生产)
docker compose up -d              # 容器化部署
```

🪄 **想在 Claude Code 里互动地用？**

```bash
bash skill/install.sh                                       # 一行装
~/.claude/skills/invest/run.sh status                       # 看持仓 / 浮盈
~/.claude/skills/invest/run.sh prepare_committee NDQ.AX     # 召唤 4 角色委员会
```

---

## 🧠 Architecture

```
invest/
├── agents/                    🤖 4 个角色 + macro strategist 的 prompts
│   ├── macro_strategist.py
│   ├── quant.py
│   ├── risk_officer.py
│   └── cio.py
├── core/
│   ├── committee.py           🎯 Coordinator-Worker 编排（cross-challenge / Round 2 / CIO 综合）
│   ├── memory_store.py        💾 frontmatter + atomic write + transaction()
│   ├── portfolio_manager.py   👤 with_portfolio_tx() — 单锁 RMW 闭包
│   └── consolidation_lock.py  🔒 Dreaming 跨进程独占锁
├── jobs/                      ⏰ APScheduler 自动发现的 YAML 定义
│   ├── daily_report.py / .yml
│   ├── dreaming.py / .yml     💤 OpenClaw 三阶段记忆整合
│   ├── payday_check.py / .yml
│   └── commsec_sync.py / .yml
├── scheduler/runner.py        🕐 APScheduler + SQLAlchemy 持久化
├── connectors/napcat_bot.py   📱 微信/QQ 命令接口（/deposit /gold_buy ...）
├── skill/                     🪄 Claude Code Skill (SKILL.md + run.sh + install.sh)
├── memory/                    💾 source-of-truth（不入 git）
│   ├── user.md / strategy.md / portfolio.md
│   ├── daily/<date>.md        — 日志
│   ├── .committee/<date>/*.md — 委员会备忘
│   ├── .dreams/events.jsonl   — 审计 + 失败事件流
│   └── insights/*.md          — Dreaming 凝固出的长期模式
└── utils/
    ├── exchange_fee.py        💱 多源行情（DB → scraper → yfinance → CSV 兜底）
    └── gold_price.py          🥇 浙商积存金克价换算
```

---

## ⚙️ 配置详解

### `target_assets` 多资产 schema

```yaml
target_assets:
  - symbol: NDQ.AX
    currency: AUD
    max_single_invest_cny: 10000
    channel: CommSec
    note: AUD 子弹已用尽，重点观察突破回调
  - symbol: GC=F
    currency: CNY
    max_single_invest_cny: 5000
    channel: 浙商银行积存金
    sell_fee_pct: 0.0038
    price_offset_pct: 0.0
```

每个资产独立 cap、独立 channel、独立点差。详见 [`docs/memory_layout.md`](docs/memory_layout.md)。

### 可调 env (默认值已合理)

| Env | 默认 | 作用 |
|---|---|---|
| `INVEST_LLM_MAX_ATTEMPTS` | 3 | LLM 最大尝试次数 |
| `INVEST_LLM_BASE_DELAY` | 2.0 | 重试初始延迟 (秒) |
| `INVEST_LLM_MAX_DELAY` | 20.0 | 重试单次上限 (秒) |
| `INVEST_PRICE_STALE_DAYS` | 3 | 价格陈旧告警阈值 |
| `INVEST_WHITELIST_QQ` | — | NapCat 命令白名单 QQ |
| `DIGEST_EMAIL_TO` | — | 兜底收件人 |

---

## 🪄 Claude Code Skill：把 invest 装进 Claude

> 🔥 **OpenClaw + Claude Code v2.1.88 Coordinator Mode 的标准实现样本之一。**

```bash
cd $INVEST_HOME && bash skill/install.sh
```

`install.sh` 在 `~/.claude/skills/invest/` 建立 symlink 指向仓库里的
`skill/SKILL.md` + `skill/run.sh`。改协议只需 commit + 其他设备 `git pull` 立即同步。

可用子命令：

| Command | 干啥 |
|---|---|
| `status` | 持仓 + 浮盈 + 实时价（JSON） |
| `strategy` | target_assets + Dreaming 长期 insight |
| `live_prices` | VIX / TNX / USDCNY / GC=F / NDQ.AX |
| `history -n 10` | 最近 N 笔交易 + N 个委员会决议 |
| `prepare_committee <SYMBOL>` | 拿到 brief + prompts，给 Claude 做 Coordinator-Worker fan-out |
| `save_committee <SYMBOL>` | 持久化 4 角色 transcript 到 `memory/.committee/<date>/` |
| `what_if --gold-pct -5` | 算 P&L 假设场景，无 LLM 调用 |

详见 [`skill/README.md`](skill/README.md) 和 [`skill/SKILL.md`](skill/SKILL.md)。

---

## 🗺️ Roadmap

✅ **已交付**

- [x] OpenClaw 风格 frontmatter Markdown memory store
- [x] APScheduler + YAML job discovery
- [x] 4 角色 Investment Committee + cross-challenge round
- [x] Dreaming 三阶段记忆整合（Light/REM/Deep Sleep）
- [x] 多资产支持（股票 / 黄金，单元独立 cap）
- [x] NapCat 微信/QQ 命令接口
- [x] Claude Code Skill 双 LLM 模式
- [x] 5 轮 audit 硬化：atomic write / LLM retry / TOCTOU / data quality 4 件套 / email raise
- [x] Docker + APScheduler 容器化部署

🔜 **路上**

- [ ] `tests/test_concurrency.py` —— 把 50 线程压测固化进 pytest，加 GitHub Actions
- [ ] Multi-tenant：`memory/<user_id>/...` schema，`MemoryStore.path_of` 加 user_id 维度
- [ ] Prometheus metrics 出口（job_runs / llm_call_duration / price_staleness_days）

---

## 🚨 投资免责声明

本系统为 LLM-driven 决策辅助工具。

- 不构成任何投资建议
- LLM 输出可能出错、过度自信、漏看重要信息
- 交易行为请你自己评估、自己执行、自己负责
- 系统当前默认**只建议入场/加仓/减仓，不会自动下单**

LLM 失误的损失没人能赔。**用之前先用 `what_if` 在小金额上跑两周。**

---

## 🤝 Contributing & 引用

PR / Issue 欢迎。如果你在论文 / 博客 / 项目里引用这个架构（4 角色委员会 + Dreaming + frontmatter memory），请反链回来：

```
https://github.com/longsizhuo/invest
```

借鉴 / 致敬：

- [**OpenClaw Dreaming Guide**](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e) — Dreaming 三阶段架构灵感来源
- [**Claude Code v2.1.88**](https://claude.com/claude-code) — Coordinator-Worker 协议样本

---

<div align="center">

**如果这个项目帮到了你，给个 ⭐️ 是最好的鼓励。**

*Built with 🧠 by humans + AIs · 让 Coordinator 帮你睡个好觉*

</div>
