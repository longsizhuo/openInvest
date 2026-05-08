# Onboarding（doctor 返回 `needs_setup` 时读）

用户还没第一次配。用 `AskUserQuestion` 工具问 6 个问题，然后拼 JSON
piped 给 `run.sh init --from-stdin`。**永远不要**让用户"自己去编辑
user_profile.json"——那是 skill 失败模式。

## 6 个问题

用普通话问（如果用户用其他语言，跟随用户）：

| # | 问 | 备注 |
|---|------|------|
| Q1 | 怎么称呼你？ | display name；用户不愿给就 "Anonymous" |
| Q2 | 风险偏好？ | Conservative / Balanced / Aggressive 三选一 |
| Q3 | 月收入 / 月支出 / 换汇周转金 (CNY)？ | 三个数。换汇周转金 = 应急金，不会被投资 |
| Q4 | 当前持仓？ | 看下面 "Q4 详细"——大多数人用默认就行 |
| Q5 | DeepSeek API key？ | **可选**。Skill 模式（你正在用的）不需要；只有 cron 自动模式需要。告诉用户这点，免得他们以为必须去 platform.deepseek.com 注册 |
| Q6 | Gmail App Password？ | **可选**。不给则不发邮件日报。Gmail 必须先开 2FA 再去 [App Passwords](https://myaccount.google.com/apppasswords) 生成 16 位密码（不是登录密码！） |

### Q4 详细

5 个字段对应用户**默认**的两个追踪资产（NDQ.AX + GC=F 浙商积存金）。
**不要问 `target_assets`**——默认覆盖 90% 用户，他们之后可以通过 Web GUI
或 `POST /api/holdings` 加任意 yfinance symbol（看 references/adding-assets.md）。

| 字段 | 问法 | 用户没这资产时填 |
|------|------|------------------|
| `cash_cny` | CNY 现金多少？ | 0 |
| `aud_cash` | AUD 现金多少？(澳元，没有可填 0) | 0 |
| `ndq_shares` | NDQ.AX 股数？(BetaShares 纳指 100 ETF，澳交所) | 0 |
| `gold_grams` | 黄金克数？(浙商积存金) | 0 |
| `gold_avg_cost_cny_per_gram` | 黄金均价 CNY/g？(0 表示未持有) | 0 |

如果用户只有 CNY 现金没别的，全部填 0 也 OK。委员会能跑——只是没仓位
评估，等用户后续 GUI 加资产再说。

## 拼 payload

收完答案：

```bash
echo '{
  "profile": {
    "name": "<Q1>",
    "risk_tolerance": "<Q2>",
    "monthly_income_cny": <Q3a>,
    "monthly_expenses_cny": <Q3b>,
    "exchange_buffer_cny": <Q3c>,
    "last_run_date": "<今天 YYYY-MM-DD>",
    "current_assets": {
      "cash_cny": <Q4a>, "aud_cash": <Q4b>,
      "ndq_shares": <Q4c>, "gold_grams": <Q4d>,
      "gold_avg_cost_cny_per_gram": <Q4e>
    },
    "investment_strategy": {
      "target_allocation_stock": 0.7,
      "target_allocation_cash": 0.3,
      "max_single_invest_cny": 10000
    }
  },
  "env": {
    "DEEPSEEK_API_KEY": "<Q5 或空字符串>",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "EMAIL_SENDER": "<Q6a 或空字符串>",
    "EMAIL_PASSWORD": "<Q6b 或空字符串>"
  }
}' | ~/.claude/skills/invest/scripts/run.sh init --from-stdin
```

`init` 返回 `status: "ok"` 后，**马上**再跑一次 `run.sh doctor` 确认
`status: "ready"`，然后回去执行用户最初的请求。

## 为什么 wire 格式还在用 v1 字段

JSON payload 用扁平字段（`cash_cny`、`ndq_shares` 等）但 `portfolio.md` 存储是
v2（cash dict + holdings list）。`migrate_profile.py` 是转换层——它接受 v1 payload
然后写 v2 `portfolio.md`（带 `schema_version: 2`）。wire 格式保持扁平是为了
最小化 onboarding 问题数量；用户之后通过 GUI 或 `POST /api/holdings` 加新 symbol /
新币种。

## 重新 onboarding

`run.sh init --force` 会覆盖现有 `user_profile.json`。用户想从头开始时用这个。
（不动 `.env`——那个是合并写入的）。

## 常见坑

- **Gmail App Password 不是 16 位** → 用户给的多半是登录密码。指他们去
  https://myaccount.google.com/apppasswords。
- **DeepSeek key 不以 `sk-` 开头** → 多半是页面标题误粘了。让用户重新复制 key。
- **用户不记得 NDQ.AX 股数 / 黄金克数** → 说"填 0 也行，之后用 GUI 或 NapCat 命令补"。
  别因为记忆问题卡住 onboarding。
