# Onboarding（doctor 返回 `needs_setup` 时读）

用户还没第一次配。**永远不要**让用户"自己去编辑 user_profile.json"——那是
skill 失败模式。两条路径都喂 stdin：

- **Coordinator 路径（Claude Code）**：你（Claude）用 `AskUserQuestion`
  问下面 5 个问题，拼 JSON piped 给 `run.sh init --from-stdin`。
- **Direct 路径（任意 agent）**：照样可以走 `init --from-stdin`，把答案
  拼成 JSON 喂进去；问问题靠你自己的对话工具。

## 5 个问题（默认问，不要问"你追踪哪些 yfinance symbol"）

普通话提问（如果用户用其他语言，跟随用户）：

| # | 问 | 备注 |
|---|------|------|
| Q1 | 怎么称呼你？ | display name；用户不愿给就 `Anonymous` |
| Q2 | 风险偏好？ | `Conservative` / `Balanced` / `Aggressive` 三选一 |
| Q3 | 月收入 / 月支出 / 换汇周转金 (CNY)？ | 三个数。**都可填 0 跳过**（不影响委员会跑，只影响 Risk Officer 算 dry_powder）|
| Q4 | **当前持有什么？**（自由描述）| 见下面 "Q4 自然语言"——不要按字段问 |
| Q5 | DeepSeek API key & Gmail App Password？ | **可选**。Coordinator 路径（Claude Code 里说话）不需要任何 key 就能跑；只有想让服务器后台每天自动跑/发邮件才需要。详见下面 "Q5 详细" |

### Q4 自然语言（核心改动 2026-05）

**不要再硬编码 "NDQ.AX 股数 / 黄金克数 / aud_cash / cash_cny"**。让用户自由
描述。后端 `cmd_init` 看到 `holdings_description` 字段会自动调 DeepSeek 解析
成 v2 schema。

**问法**：
> 用一句话告诉我你现在持有什么（资产 + 现金都说）。例：
> "510300 沪深 300 ETF 3000 股 4.2 元，招行朝朝宝 8 万，工行积存金 50 克 750 均价"
> "AAPL 100 股 150 美元成本，BTC 0.3 个，CNY 现金 5 万"
> "什么都没有，就 1 万块 CNY"

**几条边界规则告诉用户**（不强制，但帮 LLM 解析得准）：
- A 股直接说代码（`510300`），不需要后缀
- 港股 / 美股说 ticker（`0700.HK` 或干脆"腾讯"）
- 加密直接说币种（`BTC` / `ETH`）
- 余额宝 / 朝朝宝 / 银行理财 / 货币基金 → 解析器会归到 cash，不进 holdings
- 没说均价 / 渠道也行，缺啥后端补默认

**回退路径**：
- 如果用户**没有提供 DeepSeek key**（Q5 留空）：解析跑不了，cmd_init 会回退
  到 v1 字段，只把 `cash_cny`、`aud_cash` 写进 portfolio。这种用户之后必须
  用 CLI `run.sh buy <SYM> ...`（或同名 MCP 工具）加追踪资产。**告诉用户这点**。
- 如果用户**真的什么都没有**：可以填 `"什么都没有，CNY 现金 0"`，pipeline 跑通就行。

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
    "holdings_description": "<Q4 用户原话，原样塞这里>",
    "current_assets": {"cash_cny": 0, "aud_cash": 0, "ndq_shares": 0},
    "investment_strategy": {
      "target_allocation_stock": 0.7,
      "target_allocation_cash": 0.3,
      "max_single_invest_cny": 10000
    }
  },
  "env": {
    "DEEPSEEK_API_KEY": "<Q5a 或空字符串>",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "EMAIL_SENDER": "<Q5b 或空字符串>",
    "EMAIL_PASSWORD": "<Q5c 或空字符串>"
  }
}' | ~/.claude/skills/invest/scripts/run.sh init --from-stdin
```

`current_assets` 里那三个 v1 字段全填 0 也 OK——`holdings_description` 走通
之后会**覆盖**写 portfolio.md（v2 schema 含完整 holdings list）。

`init` 返回 JSON 里看 `holdings_parse_note`：
- `"parsed via DeepSeek; portfolio.md overwritten with v2 schema"` → 成功
- `"LLM parse failed (...); fell back to v1 fields"` → DeepSeek 出错，跑了 v1
  兜底；告诉用户 + 让他之后用 CLI `buy` 重补
- `"DEEPSEEK_API_KEY 缺失"` → Q5 没填 key，回退 v1。要么让用户填，要么让他
  之后用 CLI `buy` 加资产

`status: "ok"` 后**马上**再跑一次 `run.sh doctor` 确认 `status: "ready"`，
然后回去执行用户最初的请求。

## Q5 详细：DeepSeek key & Gmail App Password 怎么搞

**两个都是可选**。如果用户在 Claude Code 里聊天就够，**两个都跳过没问题**——
告诉用户："你直接说 '看看我的持仓' / '该不该加仓 X'，Claude 会帮你跑分析，
不烧任何 token 也不用注册账号。"

### LLM key（Direct 路径用，很多人叫它"DeepSeek key"但不是只能填 DeepSeek）

什么时候需要：**cron / 定时任务无人值守跑**（不管背后是哪个 agent）。
交互场景（用户在场问"该不该买 X"）不需要——Claude Code / Hermes 等有子任务
委派能力的 agent 走 Coordinator 协议，零 key。判定标准是"有没有人在场"，
不是"用的什么 agent"，详见 SKILL.md"选路径"。

任意 OpenAI 兼容端点都行（`.env` 填 `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`，
`DEEPSEEK_*` 三件套仍兼容保留）：
- **想零成本**：千问 / 智谱 / MiMo 等目前有免费额度的供应商都能接
  （额度条款会变，去对应平台确认当前政策）
- **不想比价**：[platform.deepseek.com](https://platform.deepseek.com) 注册 →
  API keys 页面创建，复制 `sk-` 开头的字符串。日报量级（几个资产/天）成本
  约 ¥0.01-0.03 一次，一个月不到 ¥2

### Gmail App Password（用于发每日决议邮件）

什么时候需要：用户想让 cron 跑完每日 daily_report 后给自己发邮件总结。
**不发邮件就跳过**。

去哪开（**告诉用户这个完整链接**）：
1. Gmail 账号必须先开 2FA（[myaccount.google.com/security](https://myaccount.google.com/security)）
2. 然后去 [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) 生成 16 位密码
3. **不是登录密码** —— 是 16 位带空格的随机串，例如 `abcd efgh ijkl mnop`

### 用户跳过 Q5 后的引导（**关键**）

`init` 完了，告诉用户：
> 现在你可以直接对我说"看看我的持仓"或"该不该加仓 X"，我会帮你跑 4 角色 AI
> 委员会分析。

**不要**说 "Coordinator 模式 / Direct 模式" 这种术语 —— 小白听不懂。

## 直接结构化喂 v2（高级用户 / 脚本场景）

如果调用方已经能算 v2 schema（比如另一个 agent 解析了 broker statement），
跳过 `holdings_description`，直接传 `holdings_v2`：

```json
{
  "profile": {
    "...": "...",
    "holdings_v2": {
      "cash": {"CNY": 50000, "AUD": 800},
      "holdings": [
        {"symbol": "510300.SS", "kind": "etf", "units": 3000,
         "unit_label": "股", "avg_cost": 4.20, "cost_currency": "CNY",
         "channel": "未指定", "display_name": "沪深 300 ETF"}
      ]
    }
  }
}
```

`holdings_v2` 优先级高于 `holdings_description`（不调 LLM，省 token）。

## 重新 onboarding

`run.sh init --force` 会覆盖现有 `user_profile.json`。用户想从头开始时用这个。
（不动 `.env`——那个是合并写入的）。

## 降级后必说话术

`cmd_init` 返回的 `holdings_parse_note` 值决定 agent 必须说的话。**不允许跳过，不允许只在
`next_step` 里藏着、等用户追问才说**。

| `holdings_parse_note` 值（含以下关键词） | agent 必须对用户说的话（中文原文，不得改动要点） |
|---|---|
| `"DEEPSEEK_API_KEY 缺失"` | "你的持仓我暂时按基础模式记录了——只录了现金，没识别你说的具体股票。想让我自动识别 (510300 → 沪深300ETF 那种)，需要一个免费 DeepSeek API key，30 秒去 platform.deepseek.com 注册。要不要现在搞定？" |
| `"LLM parse failed"` | "解析你说的持仓时出了点问题（DeepSeek 临时故障或网络超时），现在只录了现金部分。你可以等一会儿重跑 `run.sh init --force`，或者让我用 `run.sh buy` 帮你手动加股票。" |
| `"parsed via DeepSeek"` 且 `user_review_required: true` | 读出 `parsed_holdings_for_user_review` 里每条持仓让用户确认，例："我理解你持有：A 3000 股 4.2 元、B 50 克黄金 750 均价。对吗？" |
| `"no holdings_description provided"` | 无需额外说（用户本来就没描述持仓） |

### 降级后禁止做的事

- 不要在 `next_step` 里简单带过，然后继续推进其他步骤——用户看不到 `next_step` 字段
- 不要假设用户知道什么是 v1 / v2 fallback；改用"基础模式"这种说法
- 不要在用户确认持仓前就跑 `run.sh status` 告诉用户"持仓正确"（status 命令会
  输出空持仓，会让用户以为出错）

## 常见坑

- **Gmail App Password 不是 16 位** → 用户给的多半是登录密码。指他们去
  https://myaccount.google.com/apppasswords。
- **DeepSeek key 不以 `sk-` 开头** → 多半是页面标题误粘了。让用户重新复制 key。
- **LLM 解析的 symbol 不对**（如把"宁德时代"映射成 `300750.SZ` 但用户其实买的
  港股 `3750.HK`）→ 让用户跑 `run.sh status` 检查，不对就用 CLI `sell` / `buy` 修正。
- **Coordinator 路径用户没给 DeepSeek key** → 完全 OK，Coordinator 不调
  DeepSeek。但要告诉用户："你跳过 key 之后没法用 Direct 路径（Cron / 非
  Claude agent），如果只在 Claude Code 里用就够了。"
