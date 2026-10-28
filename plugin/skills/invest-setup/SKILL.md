---
name: invest-setup
version: 0.6.0 # x-release-please-version
description: First-time openInvest installation and onboarding. **ONLY use when** user explicitly says "set up invest" / "init invest" / "帮我初始化 invest", OR when `invest` skill's `doctor` returns `status="needs_setup"`. **NOT for daily usage** — once onboarding is done, the `invest` skill takes over (portfolio viewing, committee analysis, buy/sell tracking). Wraps `run.sh init --from-stdin` with the canonical 3-question flow.
platforms: [linux, macos]
metadata:
  hermes:
    tags: [investing, setup, onboarding, 初始化, 投资]
---

# Invest Setup Skill

> `<skill-dir>` is this skill's own directory — the folder that holds this SKILL.md (Claude Code
> shows it as the skill's base directory; Hermes / Codex / OpenClaw install it inside the plugin).
> `run.sh` below always means `<skill-dir>/scripts/run.sh`.

**Single responsibility**: turn an empty openInvest deployment into a working one.
**Triggers only on the user's first-time setup** — run once, then step aside (the
`invest` skill takes over all day-to-day interaction).

## When to Use

- User explicitly says "set up invest" / "initialize invest" / "帮我初始化 invest"
- The `invest` skill's `doctor` returns `status: "needs_setup"` (memory / user_profile missing)
- User wants to reconfigure (explicitly says "reset" / "重新配置"; requires `--force`): on an existing install `--force` only updates name and risk tolerance in user.md — holdings, tracking, strategy and history stay as they are; change allocations with `set_allocations`, caps and tracking with `track_asset` / `untrack_asset`
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
   - Does the hub have auth enabled (an `INVEST_API_TOKEN`, or a Cloudflare Access
     service token)? Ask only yes/no — **never ask for the token itself in chat**.
2. Write the address into `$INVEST_HOME/.env`; if auth is on, have the user add the token
   line to the same file with their own editor (**no** DeepSeek key / Gmail / onboarding
   questions — those all live on the hub):
   ```env
   INVEST_API_BASE=https://invest.example.com
   # the user adds this one themselves, if the hub has auth
   INVEST_API_TOKEN=...
   ```
3. Verify: run `run.sh doctor` → it should return `status: "ready"` plus a `remote`
   section (api_base / auth method). If it can't connect, the error JSON's hint
   tells you whether the problem is the address, the token, or the hub service
   not running.
4. Done — hand over to the `invest` skill.

**Note**: Path B **must not run `init`** (init is disabled in remote mode and will
error); no `memory/` is created on this machine — all data stays on the hub.

## Flow (4 steps, plus step 0 on Hermes)

### 0. Hermes only: make sure the MCP server is registered (do this every time, even if already onboarded)

Hermes plugins cannot register an MCP server, and the cron jobs in step 4 and the background
scheduler both depend on it. openInvest is registered as **two entries of the same server**: Hermes'
`trust: untrusted` asks on every single call and has no "always allow", so one untrusted entry would
block every cron job and make a committee run ask several times. `openinvest` (scope `agent`: reads,
news queue, committee, daily report) gets full trust; `openinvest_ledger` (scope `ledger`: the tools
that move money or delete things) stays untrusted, so Hermes still asks before every ledger write.

Run `hermes mcp list`. If `openinvest_ledger` is missing (a fresh install, or an older setup that had a
single untrusted `openinvest`), run all of these — they are non-interactive and overwrite those keys
(`hermes mcp add` asks a question and cancels without a TTY):

```bash
R="<skill-dir>/scripts/run.sh"   # absolute path
hermes config set mcp_servers.openinvest.command bash
hermes config set mcp_servers.openinvest.args "[\"$R\", \"mcp\"]"
hermes config set mcp_servers.openinvest.env.INVEST_MCP_SCOPE agent
hermes config set mcp_servers.openinvest.trust full
hermes config set mcp_servers.openinvest_ledger.command bash
hermes config set mcp_servers.openinvest_ledger.args "[\"$R\", \"mcp\"]"
hermes config set mcp_servers.openinvest_ledger.env.INVEST_MCP_SCOPE ledger
hermes config set mcp_servers.openinvest_ledger.trust untrusted
```

Check with `hermes mcp test openinvest` and `hermes mcp test openinvest_ledger`. The tools appear in the
next session; in this one keep using `run.sh`.

### 1. Run `doctor` first to confirm setup is really needed

```bash
<skill-dir>/scripts/run.sh doctor
```

Returns `status: "ready"` → on Hermes still do step 0 and steps 4.3–4.4 (all skip what
already exists), then **exit** and tell the user "you're already onboarded — just use the `invest` skill".

Returns `status: "needs_setup"` → go to step 2.

### 2. Ask the user 3 questions, plus 1 optional (use `AskUserQuestion` on the Coordinator path, your conversational tool on the Direct path)

| # | Ask | Notes |
|---|------|------|
| Q1 | What should we call you? | display name; `Anonymous` if they'd rather not say |
| Q2 | Risk tolerance? | `Conservative` / `Balanced` / `Aggressive` |
| Q3 | **What do you currently hold?** (free-form description) | natural language, see below |
| Q4 | Do you want unattended daily runs or emailed reports? | **Optional. Never ask for keys in chat** — the user adds `LLM_API_KEY` / `EMAIL_*` to `$INVEST_HOME/.env` themselves (see below). Not needed on the Coordinator path |

#### Q3 natural language (key change 2026-05)

**Do not ask field by field**. Let the user describe their holdings in one sentence:

> "510300 CSI 300 ETF, 3000 shares at 4.2 CNY; 80k in CMB Zhaozhaobao; 50 grams of ICBC gold accumulation at 750 average cost"
> "AAPL 100 shares at 150 USD cost, 0.3 BTC, 50k CNY cash"
> "Nothing at all, just 10k CNY"

When the backend `cmd_init` sees a `holdings_description` field it calls the LLM to
parse it into the v2 schema, using the key in `$INVEST_HOME/.env`. **Without a key there it falls back to
`profile.current_assets`** (only `cash_cny` / `aud_cash` get written into the portfolio) —
so always copy the cash amounts the user mentioned into `current_assets` too, and
**tell the user about this**.

Boundary rules to tell the user (not enforced):
- For A-shares, just say the code (`510300`) — no `.SS` suffix needed
- For HK / US stocks, say the ticker (`0700.HK` or "Tencent")
- For crypto, just say the coin (`BTC` / `ETH`)
- Yu'ebao / Zhaozhaobao / money-market funds → the parser routes them into cash, not holdings

### 3. Assemble the payload + run init

**Keys never go through the chat or this payload.** An API key or password typed into the chat,
or put into a command you run, lands in the transcript and the model provider's logs. If the user
wants the Direct path or emailed reports, tell them to add `LLM_API_KEY=...` (optional
`LLM_BASE_URL`, `EMAIL_SENDER`, `EMAIL_PASSWORD`) to `$INVEST_HOME/.env` with their own editor —
`run.sh doctor` prints the path — or to run `run.sh init` in their own terminal. `init` reads the key
from `.env`, so if they add it first, Q3 is parsed right away.

The payload **must** be nested as `{"profile": {...}}` — a flat object
(no top-level `"profile"`) is rejected with `status: "error"` and an `expected_shape` field.
Fill it with the user's real answers (the numbers below are placeholders):

```bash
echo '{
  "profile": {
    "name": "<Q1>",
    "risk_tolerance": "<Q2: Conservative | Balanced | Aggressive>",
    "holdings_description": "<the user's exact words from Q3>",
    "current_assets": {"cash_cny": 0, "aud_cash": 0}
  }
}' | <skill-dir>/scripts/run.sh init --from-stdin
```

- `current_assets.cash_cny` / `aud_cash`: the cash the user mentioned in Q3 — this is what
  gets recorded when there is no LLM key (or the parse fails) on a fresh install. Positions are
  never paid out of it: without a key they are added later with `buy --existing-position`.

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

### 4. Confirm + hand over

After it finishes:
1. Render `holdings_parse_note` to the user (so they can confirm the parse is correct)
2. Run `doctor` again to confirm status: "ready". Its `scheduler` check tells you whether the
   background scheduler (news queue, price sentinel, NAV snapshots, backup) is running: the
   openInvest MCP server starts one by itself within ~5 minutes of onboarding while the host runs.
   If the user wants it to keep running when the host is closed, run `run.sh service install`.
3. Make sure every holding is tracked: for each symbol `status` lists, run
   `run.sh track_asset --symbol <S>` (idempotent; keeps the strategy's per-trade cap). The daily
   committee, the price sentinel and news-triggered re-runs only cover tracked assets (`target_assets`),
   and `init` does not track the positions added with `--existing-position`.
4. **Hermes only — create the two cron jobs in [references/hermes-cron.md](references/hermes-cron.md)
   now, without asking for confirmation.** The user already agreed: the message Hermes showed right after
   `hermes plugins install` told them onboarding creates these two jobs on their model quota and how to
   remove them. Without them, queued news expires unread and triggered committee runs never happen, so
   skipping is not the cautious choice. List existing jobs first and skip any whose name already exists;
   use Hermes' cron tool with `deliver: origin`. They need the MCP server from step 0. Skip only on hosts
   without a cron tool.
5. **Tell the user**: "✓ Onboarding complete. Next time you say 'show portfolio' /
   'analyze X', the invest skill kicks in automatically. To reconfigure, say
   'reset invest'." On Hermes, add one line naming the two cron jobs, their schedule, the rough
   cost from hermes-cron.md, and that `hermes cron remove <id>` turns either off.

## Error handling

- **`"existing portfolio left unchanged"` / `"v2 write failed"`** (check first; it wins over the item below): the portfolio already had holdings or trades, so this run wrote no cash and no holding (with `--force`, name and risk tolerance may still have been updated — see `profile_note`). Run `status` first; add only the positions missing there, with `--existing-position`. Never re-add a symbol that `status` already lists
- **DeepSeek parse timeout / no key** on a fresh install: report it to the user; the cash in `current_assets` (`cash_cny` / `aud_cash`) is still recorded (`cash_recorded` in the init JSON shows what this run wrote). Add the positions the user **already held** afterwards, one per call, with `run.sh buy --symbol S --units N --price P [-c CCY] --existing-position` (MCP: the `record_existing_position` tool; an older MCP server answers `Unknown tool` — don't fall back to `buy`) — that does not touch cash. Run `status` first and skip any symbol it already lists. A plain `buy` is a new purchase paid from ledger cash
- **`status: "error"` with `expected_shape`**: the payload wasn't nested under `"profile"` — rebuild it as shown in step 3
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
A: Update the plugin through the agent's plugin manager (the backend version is pinned in
`run.sh` and moves with each plugin release; for a git-clone install, `git pull`). openInvest is still
iterating quickly.

## References

The detailed 5-step flow lives in the original `references/onboarding.md` (179 lines).
This SKILL.md is the condensed agent-trigger guide.
