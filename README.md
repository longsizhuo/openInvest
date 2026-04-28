<div align="center">

# openInvest

### 4 个 AI 专家 · 1 份晨间 memo · 0 失眠夜

**不付 Wealthfront 0.25% 管理费。让 4 个独立 LLM 互相 challenge，告诉你今天该不该加仓。代码、决策、实盘 PnL 全开源。**

[![Python](https://img.shields.io/badge/Python-3.13+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-5C2D91)](https://deepseek.com)
[![Claude Code](https://img.shields.io/badge/Skill-Claude%20Code-D97757?logo=anthropic&logoColor=white)](https://claude.com/claude-code)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](#)
[![Stars](https://img.shields.io/github/stars/longsizhuo/openInvest?style=social)](https://github.com/longsizhuo/openInvest)

[⚡ 看一份示例 memo](examples/sample_memo.md) · [🪄 30 秒安装](#-30-秒上手claude-code-skill主推) · [📊 vs 8 基准实盘](#实盘-pnl-趋势live--vs-8-个基准) · [🛡️ 硬化日志](#硬化日志)

</div>

---

## 我们 vs 现有方案

| | **openInvest** | Wealthfront / Betterment | 主动型公募 | 大盘 ETF (SPY / 沪深 300) |
|---|---|---|---|---|
| 决策可解释 | ✅ 4 agent transcript 全公开 | ❌ 黑箱 | ⚠️ 季报 | — |
| 管理费 | **$0** (自托管) | 0.25% AUM | 1.5%+ | 0.05-0.5% |
| 全球资产 | ✅ 美 / 澳 / 中 | 🇺🇸 only | 🇨🇳 only | 单一市场 |
| 自托管 / 数据私有 | ✅ memory/ 留在你机器 | ❌ | ❌ | — |
| 跨会话长期记忆 | ✅ Dreaming 90 天 | ❌ | — | — |
| **实盘对比** | [📊 见下方榜单](#实盘-pnl-趋势live--vs-8-个基准) — 公开比 60 天累计 | 仅披露年化均值 | 月度净值 | 实时指数 |

> ⚠️ **不是 alpha 卖点**：60 天 sample size 太小，"跑赢"的统计意义弱。当作"AI 投资决策助理"用合理，当"基金经理替代品"会自欺。详见 [硬化日志](#硬化日志) 末尾的金融审计 TL;DR。

---

## 实盘 PnL 趋势（live）· vs 8 个基准

<div align="center">
  <img src="https://raw.githubusercontent.com/longsizhuo/openInvest/pnl-data/docs/pnl_chart.svg" alt="PnL chart with benchmark bars" width="100%"/>
  <sub>每 2h（工作日交易时段）自动 force-push 到 <a href="https://github.com/longsizhuo/openInvest/tree/pnl-data">pnl-data 分支</a> · 上半折线 = 实盘 30 天趋势，下半柱状图 = vs 同类策略产品的累计涨幅排行（类似 LLM benchmark）</sub>
</div>

**对比的 8 条基准**（按"产品逻辑相关度"分层）：

| 层级 | 类别 | 哪些 | 数据源 |
|---|---|---|---|
| **L1 同类策略产品**（核心对比） | 🤖 AI 投顾 | Wealthfront 6.2% / Betterment 6.1% / 蚂蚁帮你投 5.16% | [NerdWallet 2025 robo 比较](https://tokenist.com/investing/betterment-vs-wealthfront/) + [蚂蚁财富智能投顾](https://zhuanlan.zhihu.com/p/128638957)（一次性搜索） |
| **L1 同类策略产品** | 🏦 公募基金 | 易方达蓝筹 005827 / 兴全合宜 163417 / 招商白酒 161725 | 天天基金 API (`fund.eastmoney.com/pingzhongdata/<code>.js`) |
| **L2 机会成本基线** | 💰 储蓄/理财 | 余额宝 1.3% / 1 年定存 1.5% | 写死年化（按日复利） |
| **L3 不持有的市场** | 📊 大盘指数 | 沪深 300 | yfinance `000300.SS` |

**故意不比的基准**（避免自相关）：

- ❌ **纳指 100 / 标普 500** —— 用户持有 NDQ.AX 跟踪纳指 100，标普 500 与纳指相关性 0.85+。比这两个等于"和自己的影子比"，无意义
- ❌ **黄金 spot (GC=F)** —— 用户已持仓黄金，同样自相关

**为什么是柱状图**：基准的"今日累计涨幅"和时间轴弱相关，柱子高度一目了然。这是 LLM benchmark（GPT-4 vs Claude vs Gemini）那种产品对比榜单，不是某人的私人账本。

**刷新基准数据**：

```bash
python -m scripts.refresh_benchmarks               # 全量刷
python -m scripts.refresh_benchmarks --key 沪深300  # 单刷一条
```

建议作为 weekly cron job（基金净值每周更新即可）。AI 投顾类（Wealthfront/Betterment/帮你投）的年化是单次搜索快照，需人工定期复查 [`core/benchmarks.py`](core/benchmarks.py) 的 `_meta.retrieved` 字段。

**陈旧度提醒**（建议挂月度 cron，超过 90 天未更新就告警）：

```bash
python -m scripts.check_benchmark_freshness               # 默认 90 天
python -m scripts.check_benchmark_freshness --days 60     # 自定义阈值
```

退出码 0 = 全部新鲜；1 = 有陈旧，可被 cron 转告警邮件。

**清理历史噪声**（凌晨手动调试 / 同日多次采样导致折线图水平噪声）：

```bash
python -m scripts.clean_pnl_history --dry-run             # 预览
python -m scripts.clean_pnl_history                       # 实际执行（自动备份原文件）
python -m jobs.pnl_snapshot --render-only                 # 重渲染 SVG（不追加新 entry）
```

清理规则：① 北京时间 9-23 点之外的 entry 视为凌晨噪声删除；② 同日多条只保留最后一条合法采样。

---

## 它在做什么

每天早上 03:00，cron 触发一次投资委员会。

4 个 LLM 各开各的 session，信息隔离：

- **Macro Strategist** 看宏观（VIX / TNX / USDCNY）
- **Quant Analyst** 看技术面（RSI / 多周期分位 / 趋势），不知道你的持仓
- **Risk Officer** 看风控（集中度 / 浮盈缓冲 / 尾部损失），不知道技术信号
- **Round 2**：Quant 和 Risk 互看对方报告，调整观点
- **CIO** 综合所有人的发言，输出 BUY / ACCUMULATE / HOLD / TRIM / SELL + 置信度

输出是一份带署名的 Markdown memo，发到你邮箱。你决定要不要执行。

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

## 三个设计选择

### 1. Coordinator-Worker，不是大 prompt 塞人格

很多 multi-agent demo 是这样写的："你现在是 4 个分析师，请用 4 段话分别给出意见"。这种东西没有信息隔离，没有顺序依赖，也没有真正的 cross-challenge，本质上还是单次调用。

openInvest 是 4 个独立 LLM session，按 DAG 跑：

```
Macro ──┐
        ├─→ Quant + Risk (并行, 信息隔离)
        ├─→ Round 2: 互看对方报告再发言一次
        └─→ CIO 综合
```

Worker 之间能看见什么、看不见什么，全部在 `core/committee.py` 里显式控制。Quant 永远不知道用户持仓多少，Risk Officer 永远不知道 RSI 是多少，避免 LLM 互相污染观点。

### 2. Markdown 就是数据库

抛弃 `user_profile.json` 单文件 + 全量加载。改用 frontmatter + Markdown 双向通道：

```markdown
---
cash_cny: 18290.51
gold_grams: 123.92
gold_avg_cost_cny_per_gram: 1008.79
ndq_shares: 128
---
# 当前持仓
- CNY 现金: ¥18,290.51
- 黄金: 123.92 克，均价 ¥1008.79/g
- NDQ.AX: 128 股
```

- Frontmatter 给代码读写，atomic
- Body 给 LLM 直接读，不需要二次格式化
- 同一份 `portfolio.md`，Python 和 LLM 看到的永远一致
- `fcntl.flock` + `tmp → fsync → os.replace` 双保险，进程被 kill 也不会写一半

### 3. OpenClaw 风格的 Dreaming Memory

LLM 没有跨会话记忆。你 6 个月前因为过度集中持仓被 Risk Officer 警告过的事情，今天的 Risk Officer 完全不知道。

借鉴 [OpenClaw](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e) 的思路，每天凌晨跑三阶段记忆整合：

| 阶段 | 干什么 | 输出 |
|------|--------|------|
| Light Sleep | 摄入近 90 天交易 + 多 symbol 行情 | 信号清单 |
| REM Sleep | 找跨时间重复模式 | 候选 insight |
| Deep Sleep | 阈值门 (`score≥0.8` & `count≥3`) | 写入 `insights/` |

凝固出来的 insight 第二天会注入 CIO 的上下文。它真的会记住你。

---

## 硬化日志

大多数 agent demo 写完就丢，跑两天就崩。下面是 5 轮 audit 之后修掉的实际死法，每一条都能在代码里找到：

| 死法 | 修法 | 出处 |
|------|------|------|
| 进程被 kill 时 `portfolio.md` 写到一半，状态损坏 | Atomic write: `tmp + fsync + os.replace` 三步走 | `core/memory_store.py:_atomic_write_text` |
| NapCat 存款 + scheduler 扣款并发，有一笔凭空消失 (TOCTOU) | 单锁 RMW + `transaction()` context manager | `core/memory_store.py:transaction` |
| DeepSeek 偶发 429/5xx，CIO 在空字符串上编 memo | 指数退避 + jitter retry，区分 transient vs auth | `core/committee.py:_ask` |
| yfinance 拉不到价 → 估值返回 0 → Risk Officer 建议清仓 | `Optional[float]` + 跳过该资产，scheduler 标 `degraded` | `jobs/daily_report.py:_get_last_close` |
| BetaShares scraper 403 反爬 → NDQ 价拿不到 | Fallback yfinance，scrape 失败保 close 价 | `utils/exchange_fee.py` |
| 数据陈旧 5 天但 LLM 不知道，编今天的策略 | Staleness 阈值检测 + 注入 LLM 上下文 "⚠️ 数据陈旧 N 天" | `INVEST_PRICE_STALE_DAYS` env |
| 邮件 SMTP 失败静默 return，user 不知道日报丢了 | `EmailDeliveryError` raise，scheduler `job_runs` 表自动记录 | `services/notifier.py` |
| LLM 失败事件无审计，事后查不到 | 全部落 `.dreams/events.jsonl` | `core/memory_store.py:dream_event` |

并发压测：

```
50 线程并发 cash_cny += 1   →  最终 delta = 50.0   (0 lost updates)
20 轮 scheduler 扣款 + napcat 存款 race  →  delta 精确 = -37880  (0 lost updates)
```

---

## 🪄 30 秒上手：Claude Code Skill（主推）

**最简单的方式：把 invest 装成 Claude Code 的 skill，让 Claude 帮你 onboard。**

不用注册账号、不用编辑 JSON、不用研究 env。打开 Claude Code，跑这一行：

```bash
git clone https://github.com/longsizhuo/invest.git ~/projects-review/invest
bash ~/projects-review/invest/skill/install.sh
```

然后回 Claude Code 对话里说：

> **「帮我初始化 invest」**

Claude 会：

1. 自动检测 `memory/` 和 `.env` 缺失（`run.sh doctor`）
2. **用 5 个问题问你的情况**：姓名 / 风险偏好 / 月收入 / 当前持仓 / API key（可选）
3. 一键写入 `user_profile.json` + `.env` 并跑 migrate（`run.sh init --from-stdin`）
4. 直接给你跑 `run.sh status` 验证

之后任何时候说 **"看看我的持仓"** / **"分析一下黄金"** / **"该不该加仓 NDQ"**，
Claude 会自己调 `prepare_committee` → 派 4 个 worker（Macro/Quant/Risk/CIO）并行
分析 → 给你一份完整 CIO memo。

> 💡 **DeepSeek API key 是可选的**。Skill 模式下委员会 LLM 是 Claude 自己，不需要
> DeepSeek。只有想跑后台 cron 自动日报才需要注册 DeepSeek。

---

## 🚀 其他部署方式

### Option B · Docker（一键容器化，适合服务器跑 cron）

```bash
git clone https://github.com/longsizhuo/invest.git && cd invest
cp .env.example .env       # 填 DEEPSEEK_API_KEY / EMAIL_*

# 第一次：交互式 onboarding（写 user_profile.json + memory/）
docker compose run --rm invest-agent python -m scripts.skill init

# 起服务，自动跑 cron（daily_report / dreaming / payday_check ...）
docker compose up -d
docker compose logs -f invest-agent
```

`docker-compose.yml` 已挂载 `./memory ./db ./cache_data`，容器重建状态不丢。
启动前会自动检查 `memory/user.md` 是否存在，没初始化会友好提示并指引你跑 onboarding。

### Option C · 手动 Python（开发者 / 想魔改 prompts）

```bash
git clone https://github.com/longsizhuo/invest.git && cd invest
uv sync --frozen --python 3.13

# 跑交互式 onboarding（5 个问题）
.venv/bin/python -m scripts.skill init

python -m jobs.daily_report      # 跑一次完整委员会 (~6 min)
python -m scheduler.runner       # 全套 cron 持续跑
```

---

## 架构

```
invest/
├── agents/                    4 个角色 + macro strategist 的 prompts
│   ├── macro_strategist.py
│   ├── quant.py
│   ├── risk_officer.py
│   └── cio.py
├── core/
│   ├── committee.py           Coordinator-Worker 编排
│   ├── memory_store.py        frontmatter + atomic write + transaction()
│   ├── portfolio_manager.py   with_portfolio_tx() 单锁 RMW 闭包
│   └── consolidation_lock.py  Dreaming 跨进程独占锁
├── jobs/                      APScheduler 自动发现的 YAML 定义
│   ├── daily_report.py / .yml
│   ├── dreaming.py / .yml     OpenClaw 三阶段记忆整合
│   ├── payday_check.py / .yml
│   └── commsec_sync.py / .yml
├── scheduler/runner.py        APScheduler + SQLAlchemy 持久化
├── connectors/napcat_bot.py   微信/QQ 命令接口（/deposit /gold_buy ...）
├── skill/                     Claude Code Skill
├── memory/                    source-of-truth（不入 git）
│   ├── user.md / strategy.md / portfolio.md
│   ├── daily/<date>.md        日志
│   ├── .committee/<date>/*.md 委员会备忘
│   ├── .dreams/events.jsonl   审计 + 失败事件流
│   └── insights/*.md          Dreaming 凝固出的长期模式
└── utils/
    ├── exchange_fee.py        多源行情（DB → scraper → yfinance → CSV 兜底）
    └── gold_price.py          浙商积存金克价换算
```

---

## 配置

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

### 可调 env

| Env | 默认 | 作用 |
|-----|------|------|
| `INVEST_LLM_MAX_ATTEMPTS` | 3 | LLM 最大尝试次数 |
| `INVEST_LLM_BASE_DELAY` | 2.0 | 重试初始延迟 (秒) |
| `INVEST_LLM_MAX_DELAY` | 20.0 | 重试单次上限 (秒) |
| `INVEST_PRICE_STALE_DAYS` | 3 | 价格陈旧告警阈值 |
| `INVEST_WHITELIST_QQ` | — | NapCat 命令白名单 QQ |
| `DIGEST_EMAIL_TO` | — | 兜底收件人 |

---

## Claude Code Skill 子命令

同一套 `agents/` 和 `core/committee.py`，跑哪个 LLM 看你心情：

- **DeepSeek (cron 模式)**：每天 daily_report 自动跑，省 token
- **Claude (skill 模式)**：在 Claude Code 对话里随时召唤委员会，4 个 agent 真 async 并行

`install.sh` 在 `~/.claude/skills/invest/` 建立 symlink 指向仓库里的
`skill/SKILL.md` + `skill/run.sh`。改协议只需 commit + 其他设备 `git pull`。

| Command | 干啥 |
|---------|------|
| `doctor` | 健康自检：memory / .env / API key 状态（onboarding 入口） |
| `init [--from-stdin]` | 完成 onboarding：写 user_profile.json + .env + 跑 migrate |
| `status` | 持仓 + 浮盈 + 实时价（JSON） |
| `strategy` | target_assets + Dreaming 长期 insight |
| `live_prices` | VIX / TNX / USDCNY / GC=F / NDQ.AX |
| `history -n 10` | 最近 N 笔交易 + N 个委员会决议 |
| `prepare_committee <SYMBOL>` | 拿到 brief + prompts，给 Claude 做 Coordinator-Worker fan-out |
| `save_committee <SYMBOL>` | 持久化 4 角色 transcript 到 `memory/.committee/<date>/` |
| `what_if --gold-pct -5` | 算 P&L 假设场景，无 LLM 调用 |

详见 [`skill/README.md`](skill/README.md) 和 [`skill/SKILL.md`](skill/SKILL.md)。

---

## Roadmap

已交付：

- [x] OpenClaw 风格 frontmatter Markdown memory store
- [x] APScheduler + YAML job discovery
- [x] 4 角色 Investment Committee + cross-challenge round
- [x] Dreaming 三阶段记忆整合（Light/REM/Deep Sleep）
- [x] 多资产支持（股票 / 黄金，单元独立 cap）
- [x] NapCat 微信/QQ 命令接口
- [x] Claude Code Skill 双 LLM 模式
- [x] 5 轮 audit 硬化（atomic write / LLM retry / TOCTOU / data quality / email raise）
- [x] Docker + APScheduler 容器化部署

路上：

- [ ] `tests/test_concurrency.py` 把 50 线程压测固化进 pytest，加 GitHub Actions
- [ ] Multi-tenant：`memory/<user_id>/...` schema
- [ ] Prometheus metrics 出口（job_runs / llm_call_duration / price_staleness_days）
- [ ] **PnL 图升级为"vs 基准"对比**：现在 SVG 只画自己的实盘相对趋势。真正能凸显
  系统价值的是"vs 余额宝 / 沪深 300 / 知名 AI 投顾产品"的超额收益。改造方案：在
  `jobs/pnl_snapshot` 里加几条基准 series（年化 3% 直线 / yfinance 拉沪深 300 /
  公开 AI agent 产品的历史回报），同图叠加。隐私不变（仍是 % 趋势），但能告诉
  访客"我们比 XX 多赚 N% / 跑赢基金经理"

---

## 免责

LLM-driven 决策辅助工具。不构成投资建议。LLM 会出错、会过度自信、会漏看东西。

系统默认只建议入场/加仓/减仓，不会自动下单。

用之前先用 `what_if` 在小金额上跑两周。

---

## 致谢

- [OpenClaw Dreaming Guide](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e) — 三阶段记忆整合架构灵感来源
- [Claude Code](https://claude.com/claude-code) — Skill 模式 Coordinator-Worker fan-out 实现

PR 和 Issue 欢迎。觉得有用的话给个 ⭐️。
