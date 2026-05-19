---
name: invest-setup
version: 0.1.0 # x-release-please-version
description: First-time openInvest installation and onboarding. **ONLY use when** user explicitly says "set up invest" / "init invest" / "帮我初始化 invest", OR when `invest` skill's `doctor` returns `status: "needs_setup"`. **NOT for daily usage** — once onboarding is done, the `invest` skill takes over (portfolio viewing, committee analysis, buy/sell tracking). Wraps `run.sh init --from-stdin` with the canonical 5-question flow.
---

# Invest Setup Skill

**单一职责**：把一个空的 openInvest 部署变成可用状态。**只在用户首次配置时
触发**，跑完一次就退场（`invest` skill 接管所有日常交互）。

## When to Use

- 用户明说 "set up invest" / "initialize invest" / "帮我初始化 invest"
- 跑 `invest` skill 的 `doctor` 返回 `status: "needs_setup"`（memory / user_profile 缺失）
- 用户想完全重配（明确说 "reset" / "重新配置"，需要 `--force`）
- v1 → v2 schema 迁移（用户的 portfolio.md 是老格式）

## When NOT to Use

- 用户已经 onboard 完成（`doctor` 返回 `status: "ready"`）→ **切到 `invest` skill**
- 用户想看持仓 / P&L / 跑委员会 → 走 `invest` skill
- 用户想加新追踪资产但已 onboard → `invest` skill 的 `POST /api/holdings` 端点
- 用户想 commit / push 代码 → 这是 git 操作，跟 setup 无关

如果你（agent）误进了这个 skill，**立刻退出**，告诉用户应该用 `invest` skill。

## 流程（4 步）

### 1. 先跑 `doctor` 确认真的需要 setup

```bash
~/.claude/skills/invest-setup/scripts/run.sh doctor
```

返回 `status: "ready"` → **立即退出**，告诉用户 "已经 onboard 过了，用 `invest`
skill 即可"。

返回 `status: "needs_setup"` → 进 step 2。

### 2. 问用户 5 个问题（Coordinator 路径用 `AskUserQuestion`，Direct 路径用对话工具）

| # | 问 | 备注 |
|---|------|------|
| Q1 | 怎么称呼你？ | display name，不愿给就 `Anonymous` |
| Q2 | 风险偏好？ | `Conservative` / `Balanced` / `Aggressive` |
| Q3 | 月收入 / 月支出 / 换汇周转金 (CNY)？ | 三个数；都可填 0 跳过 |
| Q4 | **当前持有什么？**（自由描述）| 自然语言，见下面 |
| Q5 | DeepSeek API key & Gmail App Password？ | **可选**。Coordinator 路径不需要 |

#### Q4 自然语言（关键改动 2026-05）

**不要按字段问**。让用户一句话描述持仓：

> "510300 沪深 300 ETF 3000 股 4.2 元，招行朝朝宝 8 万，工行积存金 50 克 750 均价"
> "AAPL 100 股 150 美元成本，BTC 0.3 个，CNY 现金 5 万"
> "什么都没有，就 1 万块 CNY"

后端 `cmd_init` 看到 `holdings_description` 字段会调 DeepSeek 解析成 v2 schema。
**没 DeepSeek key 时回退到 v1 字段**（只写 cash_cny / aud_cash 进 portfolio），
**告诉用户这一点**。

边界规则告诉用户（不强制）：
- A 股直接说代码（`510300`），不需要 `.SS` 后缀
- 港股 / 美股说 ticker（`0700.HK` 或 "腾讯"）
- 加密直接说币种（`BTC` / `ETH`）
- 余额宝 / 朝朝宝 / 货币基金 → 解析器归到 cash，不进 holdings

### 3. wealth_context（可选 但推荐问）

如果用户透露 "这账户是零花钱" / "我有备用金" / "家族 backup" → 多问一句：

> 你 portfolio 外有应急金 / 家族 backup 吗？大概多少？（家族资金**不能**用于
> 投资，只用于消除"低现金=高风险"的误判）

记到 wealth_context：
```yaml
wealth_context:
  emergency_buffer_cny: 200000  # 或用户给的数
  family_backup_available: true
  account_purpose: "零花钱账户"  # 用户原话
  lifestyle_notes: "..."
```

详见 [docs/wiki/12-verification.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/12-verification.md)
主张 7（WealthContextOfficer）。

### 4. 拼 payload + 跑 init

```bash
echo '{
  "display_name": "...",
  "risk_tolerance": "Balanced",
  "monthly_income_cny": 30000,
  "monthly_expense_cny": 15000,
  "exchange_buffer_cny": 10000,
  "holdings_description": "<Q4 用户原话>",
  "wealth_context": { ... },   # 可选
  "deepseek_api_key": "...",   # 可选
  "gmail_app_password": "..."  # 可选
}' | ~/.claude/skills/invest-setup/scripts/run.sh init --from-stdin
```

返回 JSON：
```json
{
  "status": "ok",
  "holdings_parse_note": "...",  // 自然语言解析结果，**给用户看一眼**
  "memory_root": "/path/...",
  "next_step": "用 invest skill 跑 status 看持仓"
}
```

### 5. 确认 + 移交

跑完后：
1. 把 `holdings_parse_note` 渲染给用户看（让他确认解析对了）
2. 跑 `doctor` 再确认 status: "ready"
3. **告诉用户**："✓ Onboarding 完成。下次你说'看持仓' / '分析 X' 时会自动用
   invest skill。如果需要重新配置说 'reset invest'"。

## 错误处理

- **DeepSeek 解析超时**：报错给用户，让用户用 v1 字段重填（aud / cny / ndq_units / gold_grams）
- **schema validation fail**：通常是字段类型不对，看 `init` 返回的 error 字段
- **user_profile.json 已存在**：拒绝覆盖，让用户加 `--force` 显式确认

## 常见问题

### Q: 我用千问 / 智谱替代 DeepSeek，跑不通
A: 改 `.env` 时 **model name 也要改**：

```env
LLM_API_KEY=...
LLM_BASE_URL=...
LLM_MODEL=qwen-max         # ← 别忘了
```

只改 API key + base_url 但 model 还是 `deepseek-chat` → 上游返回 400
"model not found"。每家 provider 的 model 名都不一样，去对应官网查。

### Q: NapCat 同步成功了但 GUI 不显示新持仓
A: 当前 GUI 是 beta，可能不实时刷新。先用 `run.sh status` 验证后端数据在
（CLI 永远是 source of truth），然后**刷新浏览器**或者切到 `invest` skill
通过 AI 看持仓。

### Q: 跑完委员会决策回放空白
A: 看 `memory/.committee/<today>/<asset>.md` 文件是否生成。如果生成了但
GUI 看不到，是 GUI 已知 bug；如果没生成，调用过程出错了——跑 `run.sh doctor`
看哪一项 hint 红了。

### Q: 代码好像比示例网站旧
A: `cd ~/openInvest && git pull` 拉最新代码。openInvest 还在快速迭代，
每天可能有 GUI / oracle 修复，不一定发 release tag。

## References

详细 5 步流程在原版 `references/onboarding.md`（179 行）。本 SKILL.md 是精简
agent-触发指引。
