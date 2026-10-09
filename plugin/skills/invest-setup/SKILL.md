---
name: invest-setup
version: 0.4.0 # x-release-please-version
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
- v1 → v2 schema migration: `doctor`'s `portfolio_schema` check says `needs_migration` (an
  install made by an older version; `status` shows cash 0). Run the exact command in that
  check's `hint` — it backs up portfolio.md and converts it. Do **not** rerun `init` for this

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
parse it into the v2 schema. **Without a DeepSeek key it falls back to
`profile.current_assets`** (only `cash_cny` / `aud_cash` get written into the portfolio) —
so always copy the cash amounts the user mentioned into `current_assets` too, and
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

The payload **must** be nested as `{"profile": {...}, "env": {...}}` — a flat object
(no top-level `"profile"`) is rejected with `status: "error"` and an `expected_shape` field.
Fill it with the user's real answers (the numbers below are placeholders):

```bash
echo '{
  "profile": {
    "name": "<Q1>",
    "risk_tolerance": "<Q2: Conservative | Balanced | Aggressive>",
    "monthly_income_cny": 0,
    "monthly_expenses_cny": 0,
    "exchange_buffer_cny": 0,
    "holdings_description": "<the user's exact words from Q4>",
    "current_assets": {"cash_cny": 0, "aud_cash": 0},
    "wealth_context": {}
  },
  "env": {
    "DEEPSEEK_API_KEY": "<Q5 key, or empty string>",
    "EMAIL_SENDER": "<Gmail address, or empty string>",
    "EMAIL_PASSWORD": "<Q5 Gmail App Password, or empty string>"
  }
}' | ~/.claude/skills/invest-setup/scripts/run.sh init --from-stdin
```

- `current_assets.cash_cny` / `aud_cash`: the cash the user mentioned in Q4 — this is what
  gets recorded when there is no LLM key (or the parse fails) on a fresh install. Positions are
  never paid out of it: without a key they are added later with `buy --existing-position`.
- `wealth_context`: optional, from step 3; omit it if the user didn't mention any.
- `env`: every key is optional; `LLM_API_KEY` / `LLM_BASE_URL` work in place of `DEEPSEEK_*`.

Returns JSON:
```json
{
  "status": "ok",
  "completion": "completed_full | completed_partial",
  "holdings_parse_note": "...",  // natural-language parse result, **show it to the user**
  "parsed_holdings_for_user_review": { ... },  // present when the LLM parsed holdings
  "next_step": "..."
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

- **`"existing portfolio left unchanged"` / `"v2 write failed"`** (check first; it wins over the item below): the portfolio already had holdings or trades, so this run wrote **nothing** — neither the `current_assets` cash nor any holding. Run `status` first; add only the positions missing there, with `--existing-position`. Never re-add a symbol that `status` already lists
- **DeepSeek parse timeout / no key** on a fresh install: report it to the user; the cash in `current_assets` (`cash_cny` / `aud_cash`) is still recorded (`cash_recorded` in the init JSON shows what this run wrote). Add the positions the user **already held** afterwards, one per call, with `run.sh buy --symbol S --units N --price P [-c CCY] --existing-position` (MCP: the `record_existing_position` tool; an older MCP server answers `Unknown tool` — don't fall back to `buy`) — that does not touch cash. Run `status` first and skip any symbol it already lists. A plain `buy` is a new purchase paid from ledger cash
- **`status: "error"` with `expected_shape`**: the payload wasn't nested under `"profile"` — rebuild it as shown in step 4
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
