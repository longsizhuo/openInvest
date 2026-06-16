# 30 分钟上手 openInvest

> 给陌生 fork 用户的最快通路：从 `git clone` 到第一份 AI memo + GUI 看板。
>
> 完整版背景在 [README.md](../README.md)。这里只走 happy path。

---

## ⏱️ 时间分配

| 阶段 | 用时 | 你要做的 |
|------|------|----------|
| 1. 装依赖 | 5 min | `uv sync` |
| 2. 配 `.env` 凭证 | 5 min | DeepSeek key + 邮箱（可跳过 IMAP） |
| 3. 改 `memory/` 的 portfolio + strategy | 10 min | 把"演示数据"换成自己的 |
| 4. 跑第一次委员会 | 5 min | `python -m jobs.daily_report` |
| 5. 装 Web GUI | 5 min | `python -m scripts.sync_gui_dist` + uvicorn |

---

## 0. 前置

需要的工具：

```bash
# Python 3.13+
python --version    # 应 ≥ 3.13

# uv（推荐，快 100 倍 pip）
curl -LsSf https://astral.sh/uv/install.sh | sh
```

可选：

- DeepSeek API key（必填，否则 LLM 跑不起来）—— [开通入口](https://platform.deepseek.com/)
- Gmail 应用密码（CommSec 邮件导入用，可跳过）
- Cloudflare Access 账号（生产部署 GUI 时用，本地开发不用）

---

## 🐳 想用 Docker？（替代 step 1 + 5）

不想装 uv / 手起 uvicorn，可以直接用容器（镜像自带 GUI，无需单独 `sync_gui_dist`）：

```bash
git clone https://github.com/longsizhuo/openInvest.git && cd openInvest
cp .env.example .env && $EDITOR .env                               # 填 DEEPSEEK_API_KEY
docker compose run --rm invest-web python -m scripts.skill init     # onboarding（建 memory/，走 invest-web：agent 的 sh -c entrypoint 会吞参数）
docker compose up -d --build                                       # 起 web(:8765) + scheduler
```

浏览器开 <http://localhost:8765>。预构建镜像（`docker compose pull`）/ 端口暴露 / 生产
Caddy 等细节见 [08-deployment.md 第 0 节](wiki/08-deployment.md#0-容器一键自托管docker-compose--ghcr)。

下面的 step 1–5 是**手装 uv 的等价路径**，与 Docker 二选一即可。

---

## 1. clone + 装依赖（5 min）

```bash
git clone https://github.com/longsizhuo/openInvest.git
cd openInvest
uv sync           # 装 prod + dev 依赖
```

验证：

```bash
uv run pytest tests/ -q
# 期望: 148 passed
```

如果 `pytest` 全过 → 进 step 2。如果挂了多条，最常见是 `pyarrow` / `yfinance` 拉数据失败 → 看 [troubleshooting](#troubleshooting) 第 1 节。

---

## 2. 配 `.env`（5 min）

```bash
cp .env.example .env
$EDITOR .env
```

最少要填这些：

```bash
# DeepSeek（必填，否则委员会跑不动）
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com

# Web GUI 端口（保持默认）
INVEST_WEB_HOST=127.0.0.1
INVEST_WEB_PORT=8765
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

`memory/` 是你的私有数据目录（git ignore）。**首次 clone 时是空的**，按下面的模板写。

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
uv run python -m jobs.daily_report
```

正常会看到：

```
[INFO] Macro snapshot: SCORE=+1 SIGNAL=neutral STRENGTH=4
[INFO] Running committee for NDQ.AX (max_debate_rounds=1)
[INFO] Round 1: quant + risk parallel...
[INFO] Round 2: cross-challenge...
[INFO] CIO synthesizing...
[INFO] Saved to memory/daily/2026-05-06/NDQ.AX.md
[INFO] Email sent (if SMTP configured)
```

**预算**：单次 ~5 LLM 调用 × $0.001 ≈ ¥0.05（DeepSeek 价格）。

跑完后看：

```bash
cat memory/daily/$(date +%F)/NDQ.AX.md
# 一份完整 markdown brief，含 4 角色 transcript + CIO verdict
```

跑挂了？看 [troubleshooting](#troubleshooting) 第 2 节。

---

## 5. 装 Web GUI（5 min）

GUI 通过 GitHub Releases 分发预构建产物（不在主仓库 git history）：

```bash
# 拉最新构建到 static/
uv run python -m scripts.sync_gui_dist

# 起 web server
uv run uvicorn connectors.web_api:app --host 127.0.0.1 --port 8765
```

浏览器开 <http://localhost:8765>，应该看到：

- **主面板**：持仓 + 浮盈 + 总资产折 CNY
- **历史**：所有 deposit/withdraw/buy/sell 流水
- **策略**：目标配置 vs 实际偏差
- **委员会**：点 [Run] 触发，SSE 直播 6 个 stage
- **System**：LLM 用量、命中率、数据源健康

---

## 之后能做的事

### 自动化（可选）

```bash
# 系统级 cron 每天 03:00 跑委员会
crontab -e
# 加一行：
0 3 * * * cd /your/path/openInvest && uv run python -m jobs.daily_report
```

或者用 invest 自带的 jobs runner：

```bash
uv run python -m core.scheduler   # 读 jobs/*.yml 跑所有 enabled job
```

### CommSec 自动同步成交（澳股用户）

`.env` 里填了 EMAIL_SENDER/PASSWORD 后：

```bash
# 预览（不写入）
uv run python -m scripts.import_commsec --lookback 30

# 真正写入
uv run python -m scripts.import_commsec --lookback 30 --apply
```

或者 GUI 上点 [Import CommSec] 按钮。

> ⚠️ 默认禁用了 cron 自动模式（`jobs/commsec_sync.yml: enabled: false`），因为 IMAP 临时失败会静默丢成交。建议手动触发，先看清楚拉到了什么再写。

### 生产部署（用 CF Access 保护 GUI）

参考 README 的 ["生产部署"](../README.md) 章节。简单说：

1. systemd 起 invest-web.service（uvicorn daemon 绑 127.0.0.1:8765）
2. Caddy 反代 invest.your-domain.com → 127.0.0.1:8765
3. Cloudflare Access 在边缘验证你的邮箱

### NapCat QQ 命令（移动端）

`/balance` `/deposit 1000` `/gold_buy 5g 720` 等 11 个命令在 QQ 私聊里就能跑。装 NapCat → `core/napcat_runner.py` 启动即可。命令清单见 `connectors/napcat_bot.py:_handle`。

---

## Troubleshooting

### 1. `uv sync` 后 pytest 挂多条

最常见原因是 `yfinance` 第一次拉数据被限速。先单跑一个简单的：

```bash
uv run pytest tests/test_schemas.py -v
```

如果这个过了 → yfinance 类的测试用 `mock` 不需要网络也应该过。如果挂在 `test_quotes.py` / `test_gold_price.py` 是网络问题，重跑几次。

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

### 4. GUI 显示 "缺少 EMAIL_SENDER" 但已经填了

`.env` 是后改的，需要重启 uvicorn。Ctrl+C 后重跑。

### 5. CommSec preview 拿到 0 条但邮箱里明明有

CommSec 邮件最近 180 天才扫。如果是更早的成交：

```bash
uv run python -m scripts.import_commsec --lookback 365 --apply
```

最大 365 天。

### 6. 想从演示数据回到干净状态

```bash
rm -rf memory/.committee/* memory/.runs/* memory/daily/*
# 不要删 portfolio.md / strategy.md / user.md
```

---

## 验收清单

走完上面 5 步，下面这些都应该能正常：

- [ ] `uv run pytest tests/` 全绿
- [ ] `python -m jobs.daily_report` 能跑出 `memory/daily/<date>/<SYMBOL>.md`
- [ ] `curl http://localhost:8765/api/portfolio` 返回真实数字
- [ ] 浏览器开 :8765 能看到主面板 + 持仓数字一致
- [ ] GUI 点 [Run committee] 能看到 SSE 直播 6 个 stage
- [ ] `memory/llm_usage.jsonl` 有新条目（token 计费透明化生效）

任何一条没过就回 troubleshooting 找对应 case，找不到就开 issue。
