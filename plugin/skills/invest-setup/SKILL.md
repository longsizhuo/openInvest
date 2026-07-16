---
name: invest-setup
version: 0.3.1 # x-release-please-version
description: First-time openInvest installation and onboarding. **ONLY use when** user explicitly says "set up invest" / "init invest" / "帮我初始化 invest", OR when `invest` skill's `doctor` returns `status="needs_setup"`. **NOT for daily usage** — once onboarding is done, the `invest` skill takes over (portfolio viewing, committee analysis, buy/sell tracking). Wraps `run.sh init --from-stdin` with the canonical 5-question flow.
platforms: [linux, macos]
metadata:
  hermes:
    tags: [investing, setup, onboarding, 初始化, 投资]
---

# Invest Setup Skill

**Single responsibility**: turn an empty openInvest deployment into a working one.
**Triggers only on the user's first-time setup** — run once, then step aside (the
`invest` skill takes over all day-to-day interaction).

## When to Use

- User explicitly says "set up invest" / "initialize invest" / "帮我初始化 invest"
- The `invest` skill's `doctor` returns `status: "needs_setup"` (memory / user_profile missing)
- User wants a full reconfiguration (explicitly says "reset" / "重新配置"; requires `--force`)
- v1 → v2 schema migration (the user's portfolio.md is in the old format)

## When NOT to Use

- User is already onboarded (`doctor` returns `status: "ready"`) → **switch to the `invest` skill**
- User wants to view holdings / P&L / run the committee → use the `invest` skill
- User wants to track a new asset but is already onboarded → the `invest` skill's `POST /api/holdings` endpoint
- User wants to commit / push code → that's a git operation, unrelated to setup

If you (the agent) entered this skill by mistake, **exit immediately** and tell the
user to use the `invest` skill instead.

## Two onboarding paths

| Path | Scenario | Flow |
|------|------|------|
| **A. Fresh deployment** (default) | User's first time with openInvest; data lives on this machine | "Flow (4 steps)" below |
| **B. Connect to an existing hub** | User already runs openInvest on another machine (a server) and wants this machine to share the same data (multi-device) | "Path B" below, 2 minutes |

Phrasings that trigger Path B: "connect to my hub / 连接我的 hub" / "it's already
installed on my server / 我服务器上已经装好了" / "share one portfolio across
machines / 多台电脑共用持仓" / "connect to my existing deployment".

## Path B: connect to an existing hub (no init)

1. Ask two questions:
   - Hub address? (e.g. `https://invest.example.com` or `http://10.0.0.6:8765`)
   - Does the hub have auth enabled? A token (`INVEST_API_TOKEN`) or a Cloudflare
     Access service token (`CF_ACCESS_CLIENT_ID/SECRET`)? Skip if not enabled.
2. Write the answers into `$INVEST_HOME/.env` (only these two or three lines are
   needed; **no** DeepSeek key / Gmail / 5-question flow — those all live on the hub):
   ```env
   INVEST_API_BASE=https://invest.example.com
   INVEST_API_TOKEN=...        # optional
   ```
3. Verify: run `run.sh doctor` → it should return `status: "ready"` plus a `remote`
   section (api_base / auth method). If it can't connect, the error JSON's hint
   tells you whether the problem is the address, the token, or the hub service
   not running.
4. Done — hand over to the `invest` skill.

**Note**: Path B **must not run `init`** (init is disabled in remote mode and will
error); no `memory/` is created on this machine — all data stays on the hub.

## Flow (4 steps)

### 1. Run `doctor` first to confirm setup is really needed

```bash
~/.claude/skills/invest-setup/scripts/run.sh doctor
```

Returns `status: "ready"` → **exit immediately** and tell the user "you're already
onboarded — just use the `invest` skill".

Returns `status: "needs_setup"` → go to step 2.

### 2. Ask the user 5 questions (use `AskUserQuestion` on the Coordinator path, your conversational tool on the Direct path)

| # | Ask | Notes |
|---|------|------|
| Q1 | What should we call you? | display name; `Anonymous` if they'd rather not say |
| Q2 | Risk tolerance? | `Conservative` / `Balanced` / `Aggressive` |
| Q3 | Monthly income / monthly expenses / FX working buffer (CNY)? | three numbers; all can be 0 to skip |
| Q4 | **What do you currently hold?** (free-form description) | natural language, see below |
| Q5 | DeepSeek API key & Gmail App Password? | **Optional**. Not needed on the Coordinator path |

#### Q4 natural language (key change 2026-05)

**Do not ask field by field**. Let the user describe their holdings in one sentence:

> "510300 CSI 300 ETF, 3000 shares at 4.2 CNY; 80k in CMB Zhaozhaobao; 50 grams of ICBC gold accumulation at 750 average cost"
> "AAPL 100 shares at 150 USD cost, 0.3 BTC, 50k CNY cash"
> "Nothing at all, just 10k CNY"

When the backend `cmd_init` sees a `holdings_description` field it calls DeepSeek to
parse it into the v2 schema. **Without a DeepSeek key it falls back to v1 fields**
(only cash_cny / aud_cash get written into the portfolio) —
**tell the user about this**.

Boundary rules to tell the user (not enforced):
- For A-shares, just say the code (`510300`) — no `.SS` suffix needed
- For HK / US stocks, say the ticker (`0700.HK` or "Tencent")
- For crypto, just say the coin (`BTC` / `ETH`)
- Yu'ebao / Zhaozhaobao / money-market funds → the parser routes them into cash, not holdings

### 3. wealth_context (optional but recommended)

If the user reveals "this account is pocket money" / "I have an emergency reserve" /
"family backup" → ask one more question:

> Do you have an emergency fund / family backup outside this portfolio? Roughly how
> much? (Family funds **cannot** be used for investing — they only serve to prevent
> the "low cash = high risk" misjudgment.)

Record it into wealth_context:
```yaml
wealth_context:
  emergency_buffer_cny: 200000  # or whatever number the user gives
  family_backup_available: true
  account_purpose: "pocket-money account"  # the user's own words
  lifestyle_notes: "..."
```

See [docs/wiki/12-verification.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/12-verification.md)
claim 7 (WealthContextOfficer) for details.

### 4. Assemble the payload + run init

```bash
echo '{
  "display_name": "...",
  "risk_tolerance": "Balanced",
  "monthly_income_cny": 30000,
  "monthly_expense_cny": 15000,
  "exchange_buffer_cny": 10000,
  "holdings_description": "<the user's exact words from Q4>",
  "wealth_context": { ... },   # optional
  "deepseek_api_key": "...",   # optional
  "gmail_app_password": "..."  # optional
}' | ~/.claude/skills/invest-setup/scripts/run.sh init --from-stdin
```

Returns JSON:
```json
{
  "status": "ok",
  "holdings_parse_note": "...",  // natural-language parse result, **show it to the user**
  "memory_root": "/path/...",
  "next_step": "run status via the invest skill to view holdings"
}
```

### 5. Confirm + hand over

After it finishes:
1. Render `holdings_parse_note` to the user (so they can confirm the parse is correct)
2. Run `doctor` again to confirm status: "ready"
3. **Tell the user**: "✓ Onboarding complete. Next time you say 'show portfolio' /
   'analyze X', the invest skill kicks in automatically. To reconfigure, say
   'reset invest'."

## Error handling

- **DeepSeek parse timeout**: report the error to the user and have them re-enter using v1 fields (aud / cny / ndq_units / gold_grams)
- **schema validation fail**: usually a wrong field type — check the error field in the `init` response
- **user_profile.json already exists**: refuse to overwrite; have the user add `--force` to confirm explicitly

## FAQ

### Q: I swapped DeepSeek for Qwen / Zhipu and it doesn't work
A: When editing `.env`, **the model name must change too**:

```env
LLM_API_KEY=...
LLM_BASE_URL=...
LLM_MODEL=qwen-max         # ← don't forget this
```

Changing only the API key + base_url while the model stays `deepseek-chat` → the
upstream returns 400 "model not found". Every provider names its models differently —
check the provider's own site.

### Q: The committee decision replay is blank after a run
A: Check whether the `memory/.committee/<today>/<asset>.md` file was generated. If
not, something failed during the call — run `run.sh doctor` and see which item's
hint is red.

### Q: The code seems older than the demo site
A: Run `run.sh update` (pulls the latest release from PyPI). openInvest is still
iterating quickly.

## References

The detailed 5-step flow lives in the original `references/onboarding.md` (179 lines).
This SKILL.md is the condensed agent-trigger guide.
