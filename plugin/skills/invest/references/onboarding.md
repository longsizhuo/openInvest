# Onboarding (read when doctor returns `needs_setup`)

The user hasn't done first-time setup yet. **Never** tell the user to "go edit
user_profile.json yourself" — that is a skill failure mode. Both paths feed stdin:

(Exception: if the only problem `doctor` reports is the `portfolio_schema` check with
`needs_migration`, the user *is* set up but their portfolio.md is in the old v1 format, so
`status` shows cash 0. Run the exact command in that check's `hint` — do not rerun `init`.)

- **Coordinator path (Claude Code)**: you (Claude) use `AskUserQuestion` to ask
  the 5 questions below, assemble the JSON, and pipe it to `run.sh init --from-stdin`.
- **Direct path (any agent)**: the same `init --from-stdin` works — assemble the
  answers into JSON and feed it in; asking the questions relies on your own
  conversation tooling.

## The 5 questions (ask these by default; do NOT ask "which yfinance symbols do you track")

Ask in Mandarin (if the user uses another language, follow the user):

| # | Question | Notes |
|---|------|------|
| Q1 | What should I call you? | display name; use `Anonymous` if the user declines |
| Q2 | Risk appetite? | pick one of `Conservative` / `Balanced` / `Aggressive` |
| Q3 | Monthly income / monthly expenses / FX working buffer (CNY)? | Three numbers. **All can be 0 to skip** (doesn't affect committee runs; only affects the Risk Officer's dry_powder calculation) |
| Q4 | **What do you currently hold?** (free-form description) | See "Q4 natural language" below — do NOT ask field by field |
| Q5 | DeepSeek API key & Gmail App Password? | **Optional**. The Coordinator path (chatting inside Claude Code) runs without any keys; they're only needed if you want the server to run automatically every day in the background / send emails. See "Q5 details" below |

### Q4 natural language (core change, 2026-05)

**Stop hard-coding "NDQ.AX share count / gold grams / aud_cash / cash_cny"**. Let the user
describe freely. When the backend `cmd_init` sees a `holdings_description` field, it
automatically calls DeepSeek to parse it into the v2 schema.

**How to ask**:
> Tell me in one sentence what you currently hold (both assets and cash). Examples:
> "510300 沪深 300 ETF 3000 股 4.2 元，招行朝朝宝 8 万，工行积存金 50 克 750 均价" (3000 units of the 510300 CSI 300 ETF at 4.2 yuan, 80k in CMB Zhaozhaobao, 50 grams of ICBC gold accumulation at 750 avg cost)
> "AAPL 100 股 150 美元成本，BTC 0.3 个，CNY 现金 5 万" (100 AAPL shares at $150 cost, 0.3 BTC, 50k CNY cash)
> "什么都没有，就 1 万块 CNY" (nothing at all, just 10k CNY)

**A few boundary rules to tell the user** (not mandatory, but they help the LLM parse accurately):
- For A-shares, just say the code (`510300`) — no suffix needed
- For HK / US stocks, say the ticker (`0700.HK`, or simply "腾讯 / Tencent")
- For crypto, just say the coin (`BTC` / `ETH`)
- Yu'ebao (余额宝) / Zhaozhaobao (朝朝宝) / bank wealth-management products / money-market funds → the parser folds these into cash; they don't enter holdings
- Omitting the avg cost / channel is fine; the backend fills defaults for whatever's missing

**Fallback paths**:
- If the user **did not provide a DeepSeek key** (Q5 left blank): parsing can't run, and cmd_init
  falls back to `current_assets`, writing only `cash_cny` and `aud_cash` into the portfolio. On a
  fresh install such users add their positions later, one per call, with
  `run.sh buy --symbol S --units N --price P [-c CCY] [--kind K] --existing-position`
  (MCP: the `record_existing_position` tool). That records a position held **before** using the
  system without touching cash. A plain `buy` is a new purchase paid from ledger cash, so it would
  shrink the cash the user reported. **Tell the user this.** Only add symbols that `run.sh status`
  does not list yet — re-adding a listed one counts it twice.
  An MCP server older than this tool answers `Unknown tool: record_existing_position`: do **not**
  fall back to `buy` (it deducts cash) — use the CLI form or upgrade the server.
- If the user **truly holds nothing**: they can enter `"什么都没有，CNY 现金 0"` (nothing at all,
  CNY cash 0) — as long as the pipeline goes through, that's fine.

## Assembling the payload

Once you have the answers:

```bash
echo '{
  "profile": {
    "name": "<Q1>",
    "risk_tolerance": "<Q2>",
    "monthly_income_cny": <Q3a>,
    "monthly_expenses_cny": <Q3b>,
    "exchange_buffer_cny": <Q3c>,
    "last_run_date": "<today YYYY-MM-DD>",
    "holdings_description": "<Q4 user's answer, pasted here verbatim>",
    "current_assets": {"cash_cny": <CNY cash from Q4>, "aud_cash": <AUD cash from Q4>},
    "investment_strategy": {
      "target_allocation_stock": 0.7,
      "target_allocation_cash": 0.3,
      "max_single_invest_cny": 10000
    }
  },
  "env": {
    "DEEPSEEK_API_KEY": "<Q5a or empty string>",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "EMAIL_SENDER": "<Q5b or empty string>",
    "EMAIL_PASSWORD": "<Q5c or empty string>"
  }
}' | ~/.claude/skills/invest/scripts/run.sh init --from-stdin
```

Always copy the cash the user mentioned in Q4 into `current_assets.cash_cny` / `aud_cash` (0 only
if they really have none): without a key, or when the parse fails, that is the only cash recorded.
When `holdings_description` parses, it **overwrites** portfolio.md (v2 schema with the full holdings list).

In the JSON that `init` returns, check `holdings_parse_note`:
- `"parsed via LLM; portfolio.md overwritten with v2 schema"` → success
- `"existing portfolio left unchanged"` (checked first — it wins over the other values) → the
  portfolio already had holdings or trades, so this run wrote **nothing**: neither the
  `current_assets` cash nor any holding. Run `run.sh status` first; see the table below
- `"LLM parse failed (...); fell back to v1 fields"` → DeepSeek errored; only the `current_assets`
  cash was recorded. Tell the user; rerun `init --force` later, or add the holdings with
  `buy --existing-position` (see above)
- `"DEEPSEEK_API_KEY 缺失"` (key missing) → no key given in Q5; only the `current_assets` cash was
  recorded. Either have the user provide one and rerun `init --force`, or add the holdings with
  `buy --existing-position` (see above)
- `"v2 write failed"` → the parsed holdings were **not** saved (portfolio.md unchanged); see below

`cash_recorded` in the same JSON is the cash this run wrote. If it is `{}`, do not tell the user
their cash was recorded.

After `status: "ok"`, **immediately** run `run.sh doctor` again to confirm `status: "ready"`,
then go back and carry out the user's original request.

## Q5 details: how to get the DeepSeek key & Gmail App Password

**Both are optional.** If chatting inside Claude Code is enough for the user, **skipping both is
perfectly fine** — tell the user: "Just say 'show my portfolio' or 'should I add to X', and
Claude will run the analysis for you — no tokens burned, no account registration needed."

### LLM key (used by the Direct path; many people call it the "DeepSeek key" but it isn't limited to DeepSeek)

When it's needed: **unattended cron / scheduled runs** (regardless of which agent is behind
them). Interactive scenarios (a user present, asking "should I buy X") don't need it — agents
with subtask-delegation ability such as Claude Code / Hermes use the Coordinator protocol, zero
keys. The criterion is "is a human present", not "which agent is being used" — see "Choosing a
path" in SKILL.md.

Any OpenAI-compatible endpoint works (set `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL` in `.env`;
the `DEEPSEEK_*` trio remains supported for compatibility):
- **Want zero cost**: any provider currently offering free quotas — Qwen / Zhipu / MiMo etc. —
  can be plugged in (quota terms change; confirm the current policy on each platform)
- **Don't want to comparison-shop**: register at [platform.deepseek.com](https://platform.deepseek.com) →
  create a key on the API keys page and copy the string starting with `sk-`. At daily-report
  volume (a few assets/day) it costs about ¥0.01-0.03 per run, under ¥2 a month

### Gmail App Password (for sending the daily verdict email)

When it's needed: the user wants an email summary sent to them after the cron daily_report
finishes. **Skip it if no email is wanted.**

Where to get it (**give the user this full link**):
1. The Gmail account must have 2FA enabled first ([myaccount.google.com/security](https://myaccount.google.com/security))
2. Then go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) to generate the 16-character password
3. **It is NOT the login password** — it's a 16-character random string with spaces, e.g. `abcd efgh ijkl mnop`

### Guidance after the user skips Q5 (**critical**)

Once `init` is done, tell the user:
> You can now simply say "show my portfolio" or "should I add to X", and I'll run the 4-role AI
> committee analysis for you.

**Do NOT** use jargon like "Coordinator mode / Direct mode" — beginners won't understand it.

## Feeding structured v2 directly (advanced users / scripted scenarios)

If the caller can already produce the v2 schema (e.g. another agent parsed a broker statement),
skip `holdings_description` and pass `holdings_v2` directly:

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

`holdings_v2` takes precedence over `holdings_description` (no LLM call — saves tokens).

## Re-onboarding

`run.sh init --force` overwrites the existing `user_profile.json` and updates the **name and
risk tolerance** in `memory/user.md` (other user.md fields are kept). `profile_note` says what
happened: `"user.md updated: ..."`, or `"user.md unchanged: ..."` when the value was rejected (risk
tolerance must be `Conservative` / `Balanced` / `Aggressive`) — tell the user either way. It does
**not** change strategy.md: allocations go through `set_allocations`, caps and tracked assets through
`track_asset` / `untrack_asset`. (It does not touch `.env` either — that file is merge-written.)
Never run `python -m openinvest.migrate_profile --force` on an existing install: it replaces the
portfolio with the old `current_assets` cash and empties the tracked assets. It rewrites portfolio.md only
while that is still the cash-only result of a previous init (no holdings, no trades recorded),
and backs up the old file to `portfolio.md.bak.<timestamp>` first. Once holdings or any
buy/sell/deposit exist, the portfolio is kept exactly as it is — the new `current_assets` cash is
not written either — and `holdings_parse_note` starts with `"existing portfolio left unchanged"`.
So `--force` is not the way to correct a holding or the cash — see "Common pitfalls".

## Mandatory phrasing after a degraded parse

The `holdings_parse_note` value returned by `cmd_init` determines what the agent MUST say. **You
may not skip it, and you may not bury it in `next_step` and wait for the user to ask.**

| `holdings_parse_note` value (contains these keywords) | What the agent must say to the user (verbatim script — do not alter the key points) |
|---|---|
| `"existing portfolio left unchanged"` or `"v2 write failed"` (**check first** — when present, ignore the rows below) | "I didn't change your portfolio this time — it already has data, so I left it as it was (neither the cash nor the holdings were updated)." Then run `run.sh status`. Positions already listed there must **not** be added again (another buy would count them twice). Only for positions missing from `status`: "I can add the ones that are missing — that doesn't touch your cash." (then `buy --existing-position` per missing position). Do **not** read `parsed_holdings_for_user_review` back as if it were recorded |
| `"DEEPSEEK_API_KEY 缺失"` (key missing) | "For now I've recorded your holdings in basic mode — only the cash was captured; the specific stocks you mentioned weren't recognized. If you want automatic recognition (the kind that maps 510300 → CSI 300 ETF), you need a free DeepSeek API key — 30 seconds to register at platform.deepseek.com. Want to set that up now?" If `cash_recorded` is `{}`, replace "only the cash was captured" with "nothing was captured yet" and ask how much cash they have. Without a key, on a fresh install: add the positions with `buy --existing-position`, but only those `run.sh status` doesn't already list |
| `"LLM parse failed"` | "Something went wrong while parsing your holdings (a temporary DeepSeek outage or a network timeout), so only the cash portion was recorded. You can wait a bit and rerun `run.sh init --force`, or I can add the positions you already hold one by one now — that doesn't touch your cash." (fresh install: run `run.sh status` first, then `buy --existing-position` per position it doesn't list) |
| `"parsed via LLM"` with `user_review_required: true` | Read out each holding in `parsed_holdings_for_user_review` for the user to confirm, e.g.: "My understanding is you hold: 3000 units of A at 4.2 yuan, and 50 grams of gold B at 750 avg cost. Is that right?" |
| `"no holdings_description provided"` | Nothing extra needed (the user didn't describe any holdings in the first place) |

### What NOT to do after a degraded parse

- Do not mention it briefly in `next_step` and then push ahead with other steps — the user never
  sees the `next_step` field
- Do not assume the user knows what a v1 / v2 fallback is; say "basic mode" instead
- Do not run `run.sh status` and tell the user "your holdings look correct" before they've
  confirmed their holdings (the status command would print an empty portfolio and make the user
  think something broke)

## Common pitfalls

- **Gmail App Password isn't 16 characters** → the user most likely gave their login password.
  Point them to https://myaccount.google.com/apppasswords.
- **DeepSeek key doesn't start with `sk-`** → they probably pasted the page title by mistake.
  Ask the user to re-copy the key.
- **The LLM parsed the wrong symbol / units / cost** (e.g. mapping "宁德时代" (CATL) to
  `300750.SZ` when the user actually bought the HK-listed `3750.HK`) → check with `run.sh status`,
  then `run.sh delete_holding --symbol <wrong> --force` and re-record it with
  `buy ... --existing-position`. Neither step moves cash. Don't use `sell` / plain `buy` for this:
  they move cash. Don't rerun `init --force` either: it is refused once holdings exist.
- **A Coordinator-path user gave no DeepSeek key** → completely fine; the Coordinator never
  calls DeepSeek. But tell the user: "Since you skipped the key, you can't use the Direct path
  (cron / non-Claude agents); if you only use this inside Claude Code, you're all set."
