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

## Flow (4 steps)

### 1. Run `doctor` first to confirm setup is really needed

```bash
<skill-dir>/scripts/run.sh doctor
```

Returns `status: "ready"` → **exit without changing MCP configuration, tracking, or schedules**
and switch to the `invest` skill. An explicit reset or maintenance request is separate permission;
do only the changes the user requested, not the rest of onboarding.

Returns `status: "needs_setup"` → ask whether the user wants first-time setup, then continue.

#### Hermes only: offer MCP registration for a new deployment

Hermes plugins cannot register an MCP server. The optional cron jobs and the background
scheduler depend on it. Explain before registering that starting the MCP server also starts a
local scheduler (news collection, price checks, NAV snapshots and backups) while the host runs.
Register **two entries of the same server**, both with `trust: untrusted` by default:
`openinvest` (scope `agent`: reads, news queue, committee, daily report) and `openinvest_ledger`
(scope `ledger`: portfolio writes and destructive operations). Untrusted keeps approval prompts.
These scopes are not a substitute for the host's authorization policy.

Run `hermes mcp list`. If entries are missing, show the proposed configuration and obtain approval
before adding them. Preserve existing entries and trust settings; do not run this whole block to
overwrite an older setup. The following is the **fresh-install example after approval**:

```bash
R="<skill-dir>/scripts/run.sh"   # absolute path
hermes config set mcp_servers.openinvest.command bash
hermes config set mcp_servers.openinvest.args "[\"$R\", \"mcp\"]"
hermes config set mcp_servers.openinvest.env.INVEST_MCP_SCOPE agent
hermes config set mcp_servers.openinvest.trust untrusted
hermes config set mcp_servers.openinvest_ledger.command bash
hermes config set mcp_servers.openinvest_ledger.args "[\"$R\", \"mcp\"]"
hermes config set mcp_servers.openinvest_ledger.env.INVEST_MCP_SCOPE ledger
hermes config set mcp_servers.openinvest_ledger.trust untrusted
```

Check with `hermes mcp test openinvest` and `hermes mcp test openinvest_ledger`. The tools appear in the
next session; in this one keep using `run.sh`.

Unattended jobs may be blocked by approval prompts. Do not solve that by silently switching
trust. If the user wants unattended jobs, explain exactly which `agent`-scope calls will run
without prompts (including model calls, costs and persistent research writes), then obtain
separate explicit approval before changing `openinvest.trust` to `full`. Keep the ledger entry
untrusted. A plugin installation notice, stored API key, or acceptance of onboarding is not approval
to remove prompts or create schedules. If approval is declined, keep interactive use available.

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

**Before external parsing**, explain that the holdings description (symbols, amounts, costs,
cash balances and named institutions) will be sent to the configured LLM provider. Identify the
actual provider/endpoint without displaying credentials and ask for approval for that data and
purpose. A saved API key is not consent. If the destination cannot be established, do not send it.
Use structured `profile.holdings_v2` instead and omit `holdings_description` when external parsing
is not approved; see [the structured example](references/onboarding-detailed.md#feeding-structured-v2-directly-advanced-users--scripted-scenarios).
This avoids the additional backend LLM parsing call, not the host agent's own model processing.

When the backend `cmd_init` sees an approved `holdings_description` field it calls the LLM to
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
from `.env`; adding a key does not replace the external-parsing approval above.

The payload **must** be nested as `{"profile": {...}}` — a flat object
(no top-level `"profile"`) is rejected with `status: "error"` and an `expected_shape` field.
Create a private JSON file using a file-writing tool and a JSON serializer, not shell interpolation
of user text. Fill it with the user's answers (the numbers below are placeholders). Include
`holdings_description` only after the external-parsing approval above; otherwise use `holdings_v2`.

```json
{
  "profile": {
    "name": "<Q1>",
    "risk_tolerance": "<Q2: Conservative | Balanced | Aggressive>",
    "holdings_description": "<the user's exact words from Q3>",
    "current_assets": {"cash_cny": 0, "aud_cash": 0}
  }
}
```

Pass that file as stdin, then remove the temporary file after use:

```bash
<skill-dir>/scripts/run.sh init --from-stdin < "/absolute/path/to/private-onboarding.json"
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
4. **Hermes only — offer, do not automatically create, the two cron jobs in
   [references/hermes-cron.md](references/hermes-cron.md).** If Q4 was declined or skipped, create no
   jobs. Before creation, confirm each chosen job, schedule and timezone, model/data destination,
   likely quota costs and delivery recipient. Preserve the user's choices instead of replacing them
   with the example schedules. Default to a private destination; `deliver: origin` is permitted only
   when that origin is the user's verified private conversation, not a group or public channel.
   Obtain the separate trust-change approval above if full trust is needed. List existing jobs and
   leave matching jobs unchanged; do not overwrite schedules or duplicate jobs.
5. **Tell the user**: "✓ Onboarding complete. Next time you say 'show portfolio' /
   'analyze X', the invest skill kicks in automatically. To reconfigure, say
   'reset invest'." On Hermes, report which jobs were actually created (or that none were), their
   approved schedules/destinations, the cost estimate and `hermes cron remove <id>` to turn them off.

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
