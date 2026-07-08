---
type: reference
title: 30 分钟上手 openInvest
tags: [quickstart, onboarding, setup, deployment]
intent: 新用户从 pip/uvx 安装到跑通委员会的最快通路
documents:
  endpoints: []
  config_keys: []
  symbols: []
---

# 30 分钟上手 openInvest

> 给新用户的最快通路：从 `uvx openinvest` 到第一份 AI memo。
>
> 完整版背景在 [README.md](../README.md)。这里只走 happy path。
>
> ⚠️ 2026-07-05 起 Web GUI 已退役（前端仓库封存待重做，重做走独立前端连 MCP）。
> 没有 :8765 网页面板；日常交互走 CLI / MCP / agent skill。

---

## ⏱️ 时间分配

| 阶段 | 用时 | 你要做的 |
|------|------|----------|
| 1. 安装 | 2 min | `uv tool` / `uvx`，无需 clone |
| 2. 配 `INVEST_HOME` + `.env` 凭证 | 5 min | DeepSeek key + 邮箱（可跳过 IMAP） |
| 3. 改 `memory/` 的 portfolio + strategy | 10 min | 把"演示数据"换成自己的 |
| 4. 跑第一次委员会 | 5 min | `uvx openinvest run_committee <SYM>` |
| 5. 注册 MCP（给 agent 用） | 2 min | `claude mcp add openinvest ...` |

---

## 0. 前置

需要的工具：

```bash
# uv（自带 uvx，推荐）
curl -LsSf https://astral.sh/uv/install.sh | sh
```

可选：

- DeepSeek API key（Direct 路径必填，否则 LLM 跑不起来）—— [开通入口](https://platform.deepseek.com/)
- Gmail 应用密码（CommSec 邮件导入用，可跳过）

> git clone 只用于**开发后端本身**，不是用户安装方式。想改代码再 clone。

---

## 1. 安装（2 min）

后端从 PyPI 分发，两种等价方式：

```bash
# 方式 A：uvx 免安装直跑（推荐，首跑自动拉包）
uvx openinvest doctor

# 方式 B：pip 常驻安装
pip install openinvest
openinvest doctor
```

装完拿到三个命令：

| 命令 | 用途 |
|------|------|
| `openinvest` | CLI（status / run_committee / buy / sell / ...） |
| `openinvest-mcp` | MCP stdio server（14 工具，给 agent 用） |
| `openinvest-web` | API server（仅 remote hub 部署用，见 [08-deployment.md](wiki/08-deployment.md)） |

**更新**：`skills/invest/scripts/run.sh update` 或 `uvx --refresh openinvest doctor`。

---

## 2. 配数据目录 + `.env`（5 min）

代码和数据分离：包在 uv cache / site-packages，**你的数据全在 `INVEST_HOME`**
（默认 `~/openInvest`，只放 `memory/`、`db/`、`.env`）。

```bash
export INVEST_HOME=~/openInvest    # 写进 shell rc；用默认值可省略
mkdir -p ~/openInvest
uvx openinvest init                # onboarding：交互建 memory/ 三件套
$EDITOR ~/openInvest/.env
```

`.env` 最少要填：

```bash
# DeepSeek（Direct 路径必填，否则委员会跑不动）
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

可选（全部能跳过）：

```bash
# CommSec 成交回报邮件导入（澳股用户）
EMAIL_SENDER=you@gmail.com
EMAIL_PASSWORD=app-password-16-chars

# 委员会跑完后发邮件 brief
SMTP_HOST=smtp.gmail.com
SMTP_USER=you@gmail.com
SMTP_PASS=app-password
SMTP_TO=you@gmail.com
```

> ⚠️ Gmail 必须用 [应用密码](https://myaccount.google.com/apppasswords)，不是登录密码。

---

## 3. 配自己的持仓 + 策略（10 min）

`$INVEST_HOME/memory/` 是你的私有数据目录。`init` 没走完 / 想手写，按下面的模板。

### 3.1 `memory/portfolio.md`

持仓 + 现金。Frontmatter v2 格式：

```markdown
---
cash:
  CNY: 30000.00
  AUD: 5000.00
holdings:
  - symbol: NDQ.AX
    kind: stock
    units: 50
    unit_label: 股
    avg_cost: 38.50
    cost_currency: AUD
    channel: CommSec
    display_name: BetaShares Nasdaq 100 ETF (AUD)
  - symbol: GC=F
    kind: metal
    units: 30.5
    unit_label: 克
    avg_cost: 750.00
    cost_currency: CNY
    channel: 浙商积存金
    display_name: 伦敦金 (浙商积存金)
    yfinance_proxy: GC=F
    proxy_kind: gold_cny_per_gram
    sell_fee_pct: 0.0038
---

## 你的备注

随便写。frontmatter 之外的内容是给自己看的笔记，不入决策。
```

**没有持仓也行**——`holdings` 留空 list `[]`，cash 填入自己的可投金额，先让委员会给「应该买什么」建议。

### 3.2 `memory/strategy.md`

目标配置 + 单次买入上限。

```markdown
---
target_assets:
  - symbol: NDQ.AX
    target_pct: 0.50
    max_single_buy_aud: 1000
    display_name: BetaShares Nasdaq 100 ETF
  - symbol: GC=F
    target_pct: 0.20
    max_single_buy_cny: 5000
    price_offset_pct: 0.025
    display_name: 黄金 (浙商积存金)

target_allocation_stock: 0.7
target_allocation_cash: 0.3
---

## 风险偏好

balanced（保守 / balanced / aggressive 三选一，影响 risk_officer 的尾部损失阈值）
```

### 3.3 `memory/user.md`

写一段自我介绍（年龄、收入、风险承受、目标），CIO 拿来调风格：

```markdown
---
risk_level: balanced
monthly_income_cny: 25000
monthly_savings_cny: 8000
horizon_years: 10
---

## 自我介绍

XX 岁，研发，无房贷，目标 5 年内首付。可承受 -20% 回撤但不能 -40%。
```

> 三个文件的完整字段说明见 [memory_layout.md](./memory_layout.md)。

---

## 4. 跑第一次委员会（5 min）

```bash
uvx openinvest run_committee NDQ.AX
```

正常会看到 4 角色辩论 + CIO verdict，transcript 落盘：

```bash
cat ~/openInvest/memory/daily/$(date +%F)/NDQ.AX.md
# 一份完整 markdown brief，含 4 角色 transcript + CIO verdict
```

**预算**：单次 ~5 LLM 调用 × $0.001 ≈ ¥0.05（DeepSeek 价格）。

日常查看：

```bash
uvx openinvest status        # 持仓 + 浮盈
uvx openinvest live_prices   # 实时行情
uvx openinvest decisions     # 历史决议
```

跑挂了？看 [troubleshooting](#troubleshooting) 第 2 节。

---

## 5. 注册 MCP（给 agent 用，2 min）

Claude Code / 任何 MCP 宿主一行注册：

```bash
claude mcp add openinvest -e INVEST_HOME=~/openInvest -- uvx openinvest-mcp
```

之后在 Claude Code 里直接说"看看我的持仓"、"跑委员会分析 AAPL"即可，
14 个 MCP 工具覆盖读写全链路。

---

## 之后能做的事

### 自动化（可选）

日报 cron 走 `openinvest.jobs.daily_report` 模块（pip 安装形态）：

```bash
pip install openinvest
crontab -e
# 加一行（每天 03:00 跑委员会日报）：
0 3 * * * INVEST_HOME=$HOME/openInvest python -m openinvest.jobs.daily_report
```

### CommSec 自动同步成交（澳股用户）

`.env` 里填了 EMAIL_SENDER/PASSWORD 后：

```bash
INVEST_HOME=~/openInvest python -m openinvest.jobs.commsec_sync
```

> ⚠️ 默认禁用了 cron 自动模式，因为 IMAP 临时失败会静默丢成交。建议手动触发，先看清楚拉到了什么再写。

### Remote hub 模式（可选，多数用户不需要）

`openinvest-web` 起 FastAPI（Web API 已 **deprecated**，只服务 `INVEST_API_BASE`
转发与内部触发，不再新增端点）。容器 / systemd / Caddy 细节见
[08-deployment.md](wiki/08-deployment.md)。

---

## Troubleshooting

### 1. `uvx openinvest` 首跑失败

首次运行需网络从 PyPI 拉包。检查网络后重试；公司代理环境设好 `HTTPS_PROXY`。

### 2. 跑 committee 报 401 / DeepSeek 错

```
openai.AuthenticationError: Error code: 401
```

→ `.env` 的 `DEEPSEEK_API_KEY` 没填或失效。去 <https://platform.deepseek.com/api_keys> 重发一个。

```
RateLimitError: 429 Too Many Requests
```

→ DeepSeek 当前限速，等 30 秒重跑。或者降低并行：`max_debate_rounds=1`。

### 3. portfolio.md 写错被拒

启动时报 `Pydantic ValidationError`：

```
holdings.0.units → field required
```

→ 严格按上面的 v2 模板。所有持仓必须有：`symbol / kind / units / unit_label / avg_cost / cost_currency / channel / display_name`。

### 4. 改了 `.env` 不生效

CLI 每次进程启动重读 `.env`，一般即时生效；常驻的 `openinvest-web` / MCP server 要重启进程。

### 5. CommSec preview 拿到 0 条但邮箱里明明有

CommSec 邮件扫描窗口有限（最近 180 天）。更早的成交用 `uvx openinvest buy` /
`record_execution` 手动补账。

### 6. 想从演示数据回到干净状态

```bash
rm -rf ~/openInvest/memory/.committee/* ~/openInvest/memory/.runs/* ~/openInvest/memory/daily/*
# 不要删 portfolio.md / strategy.md / user.md
```

---

## 验收清单

走完上面 5 步，下面这些都应该能正常：

- [ ] `uvx openinvest doctor` 返回 ok
- [ ] `uvx openinvest status` 显示自己的持仓数字
- [ ] `uvx openinvest run_committee <SYM>` 能跑出 `~/openInvest/memory/daily/<date>/<SYMBOL>.md`
- [ ] `claude mcp list` 里能看到 openinvest，agent 能读持仓
- [ ] `~/openInvest/memory/llm_usage.jsonl` 有新条目（token 计费透明化生效）

任何一条没过就回 troubleshooting 找对应 case，找不到就开 issue。
