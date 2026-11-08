---
name: invest
version: 0.34.4 # x-release-please-version
description: openInvest multi-asset AI investment committee — **daily use**. Read portfolio / live prices / strategy / decision history / adjust positions / run a 4-role LLM committee for an investment verdict. Supports any yfinance symbol (A-share `.SS`/`.SZ` / HK / US / ETF / crypto / commodities), China off-exchange funds (`FUND:<code>`) and any currency. **Two paths** — (1) Coordinator, Claude Code spawns 4 subagents, saves DeepSeek tokens; (2) Direct, any agent (Codex / Hermes / OpenClaw / Cursor / Cline / plain script) runs `run.sh run_committee <SYM>` for a one-shot verdict. **Trigger scenarios** — "show portfolio / 看看我的持仓", "how is my P&L / 我现在涨了多少", "should I buy/sell X / 该不该买卖X", "analyze X / 分析一下X", "run committee on X / 跑委员会", "track AAPL / 跟踪苹果", "add/trim a position, log a trade / 加仓减仓记一笔". **First-time install uses a separate skill `invest-setup`** (switch to it when `doctor` returns `needs_setup`). Backend — longsizhuo/openInvest.
platforms: [linux, macos]
metadata:
  hermes:
    tags: [investing, portfolio, committee, stocks, gold, etf, crypto, 投资, 持仓, 委员会, 行情]
---

# Invest Skill

> `<skill-dir>` is this skill's own directory — the folder that holds this SKILL.md (Claude Code
> shows it as the skill's base directory; Hermes / Codex / OpenClaw install it inside the plugin).
> `run.sh` below always means `<skill-dir>/scripts/run.sh`.

**First-time fork users**: run the `invest-setup` skill first to initialize. The main flow lives in this skill (the AI agent uses the CLI/MCP to view the portfolio / run the committee / replay decision history).
The Web GUI has been retired (2026-07) — every capability is exposed via CLI subcommands / MCP tools.
The backend is distributed from PyPI (pulled on demand via uvx) at the version pinned in `run.sh`, which moves with each plugin release: updating the plugin updates the backend. `run.sh update` re-downloads the pinned version if the uv cache breaks.

openInvest multi-asset AI investment committee. **This skill is not Claude-exclusive** — any agent that can run shell commands can use it; see "Choosing a path" below.

Reply in the language the user is currently speaking unless they explicitly ask to switch languages.

## Choosing a path

Choose a path by available capabilities and the configured reasoning mode. Both
paths must use the backend's prepared prompts, input context, model identity and
sealed submission contract. A scheduled host may use Coordinator only when it
can actually delegate isolated roles and complete that contract; otherwise use
Direct with a configured provider. Missing capability, credentials or a failed
worker is an incomplete analysis, never permission to fabricate HOLD.

| Path | Requirements | Invocation |
|------|--------------|------------|
| Coordinator | Real isolated workers; complete prepared context; recorded provider/model | Follow `references/committee-protocol.md` (Hermes: `references/committee-protocol-hermes.md`) |
| Direct | Configured backend model API | `run.sh run_committee <SYMBOL>` |

The caller chooses the model provider; preserve that identity and its actual usage
receipts. Model agreement is not independent evidence or a calibrated probability.
For historical studies use the user's explicitly selected API provider and the
study's sealed inputs; never substitute this host agent for paid model responses.

## Decision tree (identical up front, whichever path you take)

```
1. Run `run.sh doctor` (no shell access? call the MCP `doctor` tool — same output)   ← mandatory first step
   ├─ status: "ready"        → go to step 2 (keep using this skill). A missing LLM key / mail
   │                          credentials / .env shows up only as `optional_*` checks and never
   │                          flips the status: Coordinator needs no key (only Direct does — see
   │                          `direct_ready`), so do NOT re-onboard for those
   └─ status: "needs_setup"  → memory not initialized (or portfolio.md still the v1 format) →
                              **switch to the `invest-setup` skill** (not handled here)
   (An older MCP server answers `Unknown tool` for `doctor`: call `status` instead — a normal
    reply means onboarding is done.)

2. Pick the subcommand by user intent:

   "show portfolio / how much do I have / 看持仓"     → run.sh status
   "assess the situation / risk / concentration
    / 分析战况"                                       → run.sh status (see Constraints before
                                                        calling anything "overweight")
   "how am I doing / annualised return / drawdown
    / 我的收益率 / 跑赢大盘没"                        → run.sh performance (TWR / XIRR / max
                                                        drawdown / vs benchmark; also inside
                                                        status.performance — quote TWR as the
                                                        return, XIRR only as money-weighted)
   "what is my strategy / 我的策略是什么"             → run.sh strategy
   "recent trades / transaction log / 最近交易"       → run.sh history
   "where is the market / VIX now / 现在大盘"         → run.sh live_prices
   "if X drops 5% how much do I lose / 如果X跌5%"     → run.sh what_if --symbol X --pct -5
                                                        (X is a yfinance symbol in the user's
                                                        portfolio; --gold-pct / --ndq-pct kept
                                                        for legacy usage)
   "do I need to rebalance / what to buy or sell to
    get back to my target allocation / 要不要再平衡"  → run.sh rebalance_plan [--mode hybrid|cash_only|
                                                        sell_to_rebalance] [--band-pct 5] — a list
                                                        only (BUY/SELL, whole lots, reason); the user
                                                        places the orders; quote amounts from the JSON
   "I got a dividend / interest / paid a fee or tax /
    X split / 收到分红 利息 交了费 拆股 退回本金"      → run.sh record_dividend (--kind interest) /
                                                        record_fee (--kind tax) / record_split /
                                                        record_return_of_capital (MCP tools of the same
                                                        name) — bookkeeping of what already happened,
                                                        pass the REAL date (--trade-date); always send
                                                        an idempotency key (a repeated split multiplies
                                                        the position twice); never use deposit/withdraw
                                                        for these (that would book them as new money
                                                        instead of return)
   "I bought / subscribed an off-exchange fund (FUND:xxxxxx)
    today / 申购了场外基金 / 基金份额确认了吗"          → run.sh place_fund_order (MCP tool of the same
                                                        name) when the order was just placed: cash goes
                                                        out now, the order is "in transit" and total
                                                        assets do not dip. NAV publication only creates a
                                                        tracking projection. confirm_fund_order requires
                                                        the reported fill NAV and fee (explicit 0 for a
                                                        known zero fee). cancel_fund_order if
                                                        the order never went through). Status shows
                                                        pending_fund_orders. Don't use buy for these
   "should I buy/sell X / analyze X / 该不该买卖X"    → committee protocol ↓
   "track AAPL / I want to watch TSLA / 跟踪苹果"     → see references/adding-assets.md
   "alert me if gold falls below 900 / tell me when
    X drops 3% in a day / 跌破N提醒我 / 日跌超3%报警"  → run.sh track_asset --symbol X
                                                        --alert-price-below 900 | --alert-day-drop-pct 3
                                                        (MCP track_asset alert_* args) — then tell
                                                        the user alerts are only SENT when the server
                                                        has risk-alert delivery enabled and the channel
                                                        returns an acceptance receipt (see Constraints)
   "I already held 3000 X before using this /
    补录我原来就有的持仓"                              → run.sh status first — never re-add a
                                                        symbol it lists (double count); else
                                                        run.sh buy ... --existing-position
                                                        (MCP tool: record_existing_position;
                                                        `Unknown tool` = old server, do NOT
                                                        fall back to buy) — records it WITHOUT
                                                        deducting cash; a plain `buy` is a new
                                                        purchase paid from ledger cash

3. Run the committee per your path:
   - Coordinator → read references/committee-protocol.md (spawn 4 subagents)
   - Direct      → just `run.sh run_committee <SYMBOL>` for a JSON verdict

4. After getting the verdict / cio_memo:
   - **`cio_memo` is a Markdown string** (with `# title ## verdict` structure etc.).
     Render it **as Markdown for the user** — do not print raw JSON and make the
     user parse it themselves
   - Execution step: check the next_step field and guide the user as it says.
     **Never** write memory/ directly (see Constraints)
```

## Coordinator path details (Claude Code only)

Read `references/committee-protocol.md` and follow the backend's prepared
`prompts` verbatim. This entrypoint does not carry a second copy of role rules.
Always call `prepare_committee` to obtain the current context and pending causes.
Only the backend's validated current cache may be reused. A same-day Markdown
transcript, a previous HOLD or a report timestamp is never sufficient proof of
freshness or request completion. Record provider/model and submit against the
exact context identifier. If input validation fails, report that state explicitly.

If `valuation_brief` contains an `EQUIBLES_FUNDAMENTALS` block (A-share / HK stock with
`EQUIBLES_API_KEY` set), keep its `Data: Equibles - https://equibles.com` line, and name
Equibles as the source whenever you repeat those figures to the user.

## Direct path details (any agent)

```bash
# One command does it all
<skill-dir>/scripts/run.sh run_committee NDQ.AX
```

JSON output:
```json
{
  "status": "ok",
  "asset": {...},
  "verdict": {"verdict": "ACCUMULATE", "confidence": 0.72, ...},
  "confidence_lookup": "样本不足（n=12）",
  "cio_memo": "<full CIO memo markdown>",
  "transcript_path": "memory/.committee/2026-05-09/NDQ.AX.md",
  "next_step": "..."
}
```

Options:
- `--force`: rerun even if already run today (default reads the cache to save tokens)
- `--max-rounds N`: cap on cross-challenge rounds (default 1)

### Daily report (cron on the host-agent side)

```bash
<skill-dir>/scripts/run.sh daily_report   # = uvx openinvest daily_report
```

Runs the full daily-report pipeline (multi-asset committee + Gemini second opinion +
translator + discipline ledger); stdout emits markdown identical to the email body,
**does not send email** — delivery belongs to the host agent (a Hermes cron can forward
`--no-agent --script` output as-is; report formatting is guaranteed uniformly by the
backend). On a circuit-breaker trip / unconfigured target_assets, stdout is a structured
JSON error. Rerunning reruns the entire committee (burns tokens).

**Prerequisite**: with an LLM key in `.env` (backend mode) this runs the committee itself. Without
one (agent mode, the default for plugin installs) it only assembles the report from verdicts you
already submitted today — submit every target asset first (see "Keyless backend" below); the
Hermes cron template `openinvest-daily` in the invest-setup skill's `references/hermes-cron.md` does exactly that.
(**Remote-mode exception**: the hub runs it, not needed locally — see next section.)

## Remote mode (hub-and-spoke, multiple devices sharing one dataset)

> **Recommended new path (2026-07, BETA — not yet field-tested by the author in a real multi-device setup)**: when the hub runs `openinvest-mcp --http` (remote MCP),
> spokes register the HTTP MCP directly — all tools fully available, no CLI forwarding:
> `claude mcp add --transport http openinvest https://<hub>/mcp --header "Authorization: Bearer $INVEST_API_TOKEN"`
> The CLI→REST forwarding below is still supported (maintenance mode); the Coordinator protocol (prepare/save) and
> event_check still go through it (`doctor` is an MCP tool too). For deployment see backend wiki 08 §9.

With `INVEST_API_BASE` set in `.env`, this machine is a **client**: every subcommand is
auto-forwarded to the remote hub (another machine running the invest web_api), and
**this machine has no `memory/` and should not have one**.
All data (portfolio/strategy/verdicts/prompts) lives on the hub; change it once and every device sees it.

Minimal client `.env` (no DeepSeek key / Gmail / memory needed):

```bash
INVEST_API_BASE=https://your-hub.example.com   # or http://10.0.0.x:8765
INVEST_API_TOKEN=<the hub's token of the same name>   # only needed if the hub has auth enabled
# If going through Cloudflare Tunnel + Access, use this pair instead:
# CF_ACCESS_CLIENT_ID=...  CF_ACCESS_CLIENT_SECRET=...
```

Behavior differences (all other commands are forwarded transparently, **output has the
same shape as local**, decision tree applies as usual):

| Command | Remote-mode behavior |
|------|--------------|
| `doctor` | Returns **hub-perspective** checks + an extra `remote` section (api_base / auth mode / connectivity) |
| `init` | **Disabled** (data lives on the hub; connecting to the hub only needs the two .env lines above). Error includes a hint |
| `live_prices` / `correlate` | Still run **locally** (pure yfinance, touches no data) |
| `run_committee` | Runs on the **hub** (DeepSeek key is on the hub); the CLI polls automatically until done; same-day cache uses the hub's date semantics |
| `prepare/save_committee` | Via hub RPC — the Coordinator protocol (spawn 4 subagents) is **completely unchanged** |
| `buy/sell/deposit/...` write ops | Land in the hub ledger (history `source: skill_remote`) |
| `buy --existing-position` | **Refused** (the REST endpoint can't carry it, and forwarding would deduct hub cash) — run it on the hub, or use the remote MCP tool `record_existing_position` |
| `record_dividend/record_fee/record_split/record_return_of_capital`, `place_fund_order/confirm_fund_order/cancel_fund_order`, `backfill_raw_close` | **Refused** (no REST endpoint, by design) — run them on the hub, or use the remote MCP tools of the same name |
| `event_check` | Forwards the hub's manual scan; `--live` / `--recall` disabled (hub cron already covers them) |

**Discipline**: in remote mode this machine has no `memory/`, so "reading/writing memory
files directly" does not even exist — everything goes through `run.sh` or the hub API.
In the Web API table below, replace `:8765` with `$INVEST_API_BASE` in remote mode, and
add `Authorization: Bearer $INVEST_API_TOKEN` when curling.

## Tool lookup (MCP first; long tail in references/tools.md)

**MCP users** (auto-registered once the plugin is installed; same for Claude Code / Codex): doctor / status / strategy /
history / live_prices / what_if / rebalance_plan / discipline / decisions / explain_decision /
record_execution / ingest_event / buy / record_existing_position / record_balance_observation / sell / deposit / withdraw /
record_dividend / record_fee / record_split / record_return_of_capital /
place_fund_order / confirm_fund_order / cancel_fund_order /
set_allocations / track_asset / untrack_asset / news_sources / add_news_source / remove_news_source /
run_committee / prepare_committee / submit_committee / collect_news / event_history / pending_news / pending_committees / daily_report / quality_status — schemas auto-discovered; call them directly, no table lookup needed.
Use current explicit user instructions and this request's versioned backend facts for financial constraints. Historical memory is not current cash, positions or a new trading restriction; a long-term preference is not a blanket ban on risk reduction. A verifier must judge only the issued bound packet, without importing unverified memory or conclusions from the proposing agent.
**Agent-run committee over MCP (no backend LLM key):** `prepare_committee` returns the data blocks, the 6 role
prompts and `instructions`; play Macro / Quant / Risk (cheap subagents are fine), write the CIO memo yourself,
Pass your actual `provider` and `model` to `prepare_committee`, then call
`submit_committee(symbol, transcript, provider, model, context_id)` with its exact returned `context_id` within 6 hours (`provider` = your brand, `model` = the model that
plays the roles, e.g. `claude-haiku-5-5`; omitted is recorded as `unknown` and the scorecard buckets by `provider:model`). The backend applies its own saved facts for the
defense / regime rules and rejects a CIO section with no parseable verdict (fix it and resubmit).
All six role sections are required. A changed portfolio, configuration, or evidence registry invalidates the prepared context; prepare again.
An identical retry returns the original decision without consuming another recommendation quota.
`DATA_UNTRUSTED` is an input failure, not a market HOLD. Never silence `QUALITY_ALERT`, missing inputs,
or an expired/triggered review plan merely because every allocation is HOLD. `quality_status` checks these independently.
**Keyless backend (agent mode):** `collect_news` captures fixed providers with an explicit initial source window or resumes durable coverage; it makes no model calls. `event_history` reads qualified current events and is not a coverage cursor or a veto on corrected reports. `pending_news` lists
fetched news — `ingest_event` the relevant items with `one_line_claim` / `stance` / `severity` / `affected_symbols` and their exact `source_snapshot_id`.
Dismiss only individually checked snapshot versions with `dismiss_source_ids`; do not dismiss a URL or topic as permanently handled. Unprocessed expired items remain audit/recovery work, never evidence of a quiet market. `pending_committees` lists triggered re-runs — prepare, run, submit each.
The plugin hooks inject a one-paragraph `[openInvest]` digest (`run.sh brief --text`) at session start; when it says
items are waiting and the conversation is about investing, process them before or after answering unless a cron job already does it. Expiry is an overdue/recovery state; it never erases unfinished news or committee causes.
Daily report in agent mode: submit a verdict for every target asset first, then `daily_report` returns the
report Markdown to deliver; anything in `missing_verdicts` is a failure to report, never something to skip.
Do the submissions and the `daily_report` call in the same run (it reads today's local date — splitting them
across midnight makes every asset show as missing).
`track_asset` takes an optional `target_pct` (0–1 share of total assets, read by `rebalance_plan`) and
`set_allocations` a `target_pcts` map — use them instead of editing strategy.md; the weights must sum
to ≤ `target_allocation_stock` (a write that exceeds it is rejected with the current sum).
Per-asset alert lines: `track_asset` takes `alert_price_above` / `alert_price_below` / `alert_day_drop_pct` /
`alert_day_rise_pct` / `alert_drawdown_from_cost_pct`; `set_allocations` takes an `alerts` map
(`{symbol: {rule: value}}`, a null value clears that rule, a null symbol clears all its rules). `status`
shows each line and its distance on `all_holdings[].alerts` (e.g. "距 price_below 还有 3.2%").

  `source_snapshot` reads one exact captured original input and its acquisition proof.
  A queued snapshot or a `backend` label alone does not authenticate a report. The backend checks its own API response, server acquisition receipt and item observation, or a controlled collector receipt. Unbound/legacy inputs remain audit-only until acquisition or content-bound review; inspect the actual ingest status and keep verification work pending. An authenticated report is not proof of truth, prediction accuracy, learning or publication permission. Do not rewrite provenance or repeatedly repack an excluded input to obtain acceptance.
  `committee_review` retrieves the exact saved transcript and bound review packet after a task boundary; inspect resume status before resubmitting.
  For unattended jobs, `INVEST_MCP_SCOPE=automation` exposes only analysis, source ingestion and reports. Money/position changes, user constraints, execution feedback and embedded scheduler startup are unavailable in this scope. Interactive owner tools retain their usual capabilities.

**CLI/REST agents**, or long-tail operations MCP does not cover (trades intent flow /
config whitelist / events / holdings import / gold-specific endpoints) → read
`references/tools.md` (full subcommand table + endpoint table). Full OpenAPI:
`http://127.0.0.1:8765/openapi.json`.

**Subcommand names are a closed set** — commands outside the table do not exist. When
tempted to call names like `get_committee_context` / `analyze_asset`, stop and check the
table — you are most likely hallucinating; you probably want `prepare_committee`
or `run_committee`. All output is JSON; **always quote numbers from the JSON**, never from memory/*.md.

### Importing holdings from a broker-app **screenshot** (you do the OCR; backend has zero dependencies)

When the user sends a broker holdings **screenshot**: **read the image yourself** (you
have vision), convert each row into text as
`symbol/quantity/cost/currency/channel`, then use `import --text "..."` (or POST
/api/holdings/import) — the backend LLM only parses text, it never receives images.
Preview first without `--commit`; after the user verifies, run with `--commit` for a
non-destructive write.

## Feeding news (your search beats any crawler)

You have far stronger search capability than the backend crawler (including
Chinese-language sources). **When you come across finance news relevant to the user's
holdings while browsing/searching, proactively call `ingest_event` to feed it into the
event ledger** (MCP tool, or CLI `ingest_event
--title --url [--snippet --source]`) — the backend handles normalization / severity
grading / dedup / RAG recall, and resending the same item never double-books.
A new item that grades severity ≥ mid, non-neutral, and hits a holding/target automatically
re-runs the committee for that symbol (rate-limited and shared with the crawler: 12h per-symbol
cooldown that a strictly higher-severity item can bypass, ≤4 per rolling 24h; the result's
`committee_task_id` says whether one fired) — so feed material news only. Ingestion is disabled in advisory mode.
A-share / regional-market news especially: that is the crawler's blind spot and you are
the only source. If the host has a quotes/news skill installed (e.g. Longbridge), its
news is worth feeding too — the ledger cares about the information, not where it came from.

## Screeners / options chains / earnings calls / SEC filings: not built here — use a hosted MCP

openInvest does **not** build or proxy these, and will not add a dependency for them. If the user asks
for a stock screener, an options chain, an earnings-call transcript, or SEC filing text, **you (the host
agent) register a hosted MCP next to openInvest and call it directly**. Three options (checked 2026-10):

| MCP | Connect | Covers | Cost |
|-----|---------|--------|------|
| **Alpha Vantage** (official, hosted) | remote endpoint `https://mcp.alphavantage.co/mcp` (linked from the official docs as "our official MCP server"); free key at `alphavantage.co/support/#api-key` — how the key is attached (URL query vs header) is on the official MCP page, not repeated here | API catalog includes options data, earnings-call transcripts, fundamentals; the exact tool list is whatever the server's `tools/list` returns | free key: 25 requests/day |
| **Financial Datasets** (hosted) | `https://mcp.financialdatasets.ai` (answers 401 without credentials); key from `financialdatasets.ai`. The open-source repo `financial-datasets/mcp-server` runs locally over stdio with `FINANCIAL_DATASETS_API_KEY` | financial statements (income / balance sheet / cash flow), prices, company news, crypto; the site also lists insider trades (Form 4) and 10-K search | paid API |
| **OpenBB MCP server** (self-hosted, PyPI `openbb-mcp-server`) | `pip install openbb-mcp-server openbb`, then `openbb-mcp` — serves `http://127.0.0.1:8001/mcp` (streamable-http; `--transport sse` also available). Keep it on localhost. `--allowed-categories equity,news` limits what is exposed; `--tool-discovery` hides the full catalog behind search tools | routes of the installed OpenBB extensions, grouped by category: equity, economy, news, fixedincome, derivatives, etf, currency, commodity, index, regulators, crypto | free (data providers behind it may need their own keys) |

Register with your client's own MCP command (Claude Code: `claude mcp add --transport http <name> <url>`;
Codex / Hermes / Cursor: their MCP config). **Never paste an API key into the conversation, a note, or the ledger.**

**Boundary — background only, never committee input.** Whatever these return is context for answering the
user *outside* the committee. Do not pass it to `ingest_event`, into a committee prompt / brief, or into
`record_execution` reasons: the committee's inputs are deterministic and replayable (as-of-safe), and a
live screener / options-chain / transcript / filing pull is neither. The verdict still comes only from
`run_committee`. Say plainly to the user which numbers came from the committee and which from the hosted MCP.

## Decision-loop workflow (Decision Review + Reflection)

openInvest does the bookkeeping; **you do the collection** — that is the host agent's
core duty (issue #133 Decision 2).

**User asks "why HOLD today / why did it tell me to sell"** (Decision Review):
1. `explain_decision <decision_id>` for the full 4-role debate transcript + CIO memo + path snapshot
   (+ `confidence_lookup`, the number to quote next to the verdict — see Constraints)
2. Combine with `status` (current portfolio) if needed
3. Answer using evidence from the transcript — do not invent reasons yourself

**User reacts to a recommendation with "I didn't buy / I bought / I disagree"** (Reflection):
1. First ask one question about the reason (valuation too high? insufficient funds?
   disagrees with the committee? forgot?) — do not skip this; the reason is the one
   piece of information the system cannot obtain on its own
2. `record_execution <decision_id> [--rejected] --reason "..."` to write it back (idempotent; the user may change their answer anytime)
3. If the user actually traded → guide them to log the trade (a same-symbol,
   same-direction fill within 7 days is also auto-matched as a fallback)

**User asks "how often did I follow the advice / is the committee reliable"**:
`decisions --days 90` → adoption rate + the full verdict↔intervention↔execution↔outcome chain per decision
(lists the newest 20 — narrow with `--symbol` / `--verdict`, `--limit 0` for all; `count` and `summary` always cover every match);
pair with `discipline` for the counterfactual P&L of rule-blocked actions (its inaction rate counts
**live** verdicts only). For hit rates use `GET /api/verdict_review/summary`: it returns separate
`live` / `backtest` / `contaminated` buckets — quote only `live` as performance, never add the
buckets together, and say "sample too small" when a rate is `null` (n<30). Weekend decisions on
weekend-closed assets reuse Friday's close, so they are left out of every bucket and counted in
`weekend_dup_excluded` — that is why `live.n` is below the number of live decisions. When the user
rejects the same class of recommendation several times in a row, proactively point out
"the divergence pattern between you and the committee" — that is not a bad thing; it is
a signal worth recording.

## Constraints (guard these, do not break them)

- **Don't call the portfolio "overweight → TRIM" from `status` alone** (the 2026-05-12 mistake): the
  portfolio is often only part of the user's money, and the committee's concentration lens is off by
  default (ADR-020). Raise concentration only if the user asks about it. The cap on any add-on buy is
  **always** portfolio cash — this never changes
- **`max_single_invest_cny` ("单次上限") is a per-order cap on the suggested amount — not a daily or
  total allowance, and orders can be split.** When the memo says "金额已按单次上限 … 钳制", the CIO's
  amount was cut to that cap per order. Never tell the user "today's allowance is used up" because of it
- **Dividends / interest / fees / taxes / splits are ledger activities, not trades or deposits**: `record_dividend`
  (cash + gross − tax withheld; `--reinvest` = DRIP), `record_fee` (`--kind tax` for tax), `record_split`
  (`--ratio` = new/old: 10-for-1 → 10, 1-for-10 → 0.1; pass the real ex-date — if later trades were already booked in post-split units it is refused, and `--ledger-only` writes the ledger row without touching units),
  `record_return_of_capital` (lowers average cost).
  They count as account return/cost, not as money in/out. Account performance values history at the
  **raw** (not dividend-adjusted) price once `close_raw` is backfilled — run `backfill_raw_close` once after
  upgrading, and read `performance.price_basis` (`raw` | `adjusted`) before quoting a return: for an
  `adjusted` symbol a recorded dividend is deliberately left out of the NAV (the adjusted price already
  contains it; `data_quality.income_in_adjusted_price` says so); `data_quality.price_basis_gaps` lists symbols
  that fell back to `adjusted` because some days lack `close_raw` (run `backfill_raw_close`). `status.income_30d` totals the last 30 days.
- **Off-exchange China mutual funds (`FUND:<6 digits>`) are a two-step purchase**: `place_fund_order` deducts the cash now and
  records the order as in transit (`status.pending_fund_orders` + a one-line `pending_fund_orders_line`; `total_assets_cny` already
  counts it at face value, so total assets don't dip between order day and confirmation). Orders placed before 15:00 Beijing time on a
  trading day are priced at that day's NAV (`nav_date`), later ones at the next trading day's; shares arrive when that NAV is
  published — the `fund_confirm` job confirms automatically every weekday evening (QDII / FOF NAVs can take a day or two longer),
  or call `confirm_fund_order` (it is safe to repeat: an already-confirmed order returns `already_confirmed`; unpublished NAV returns
  `pending_nav` and changes nothing). Pass `--nav` / `--fee` there only to supply the real NAV or subscription fee (the job assumes fee 0).
  `cancel_fund_order` is only for orders still in transit. The expected confirm date is an **estimate** (Monday–Friday calendar, no
  China holiday table; `lag_source=default` means the fund type couldn't be fetched) — say "expected", not "will". `buy` still works
  for these funds but books them as instantly filled and warns; use it only for back-filling a purchase that is already confirmed.
- **Gold (`GC=F`) is booked per gram in CNY only**: `buy` / `record_existing_position` with
  `units` = grams, `price` = CNY/gram, `currency=CNY`, `kind=metal`, `unit_label=克`. A USD/ounce booking
  is rejected (the error shows the correct form) — convert the quote first (USD/oz ÷ 31.1035 × USDCNY),
  never retry with another currency to get it through.
- **Per-asset alert lines (`alerts`) are only delivered when the server runs with `INVEST_EVENT_ALERT=1`
  (default off, set by the owner on 2026-09-10 because event alerts were too noisy).** Without it a hit is
  logged to the event store and nothing is sent — so after setting a line, say that plainly instead of
  promising a notification, and never turn the switch on for the user (it is a server env var, not a tool
  argument). `price_*` lines are in `status.current_price`'s unit (held: its cost currency, gold booking =
  CNY per gram; tracked-only: the quote source's own price, e.g. `GC=F` in USD/oz); `*_pct` lines are
  percentage points (3 = 3%), **not** the 0–1 fractions `target_pct` uses; `drawdown_from_cost_pct` only
  works for assets the user holds. Price lines re-alert at most once per 24h; `alert_day_*` lines report a given trading day's move once (on weekends/holidays they describe the last session, and the alert text names that date); no line ever starts a committee run.
- **Do not run `daily_report` proactively in conversation** — unless the user explicitly says "run the deep analysis" / "run full report"
  or you are setting up the daily cron. That path burns DeepSeek tokens. In conversation, single-asset `run_committee` is enough.
- **Never fabricate live prices.** Always go through `run.sh status` or `live_prices`. yfinance may return
  stale data; watch the `is_stale` flag.
- **Write symbols in full.** Listed assets are yfinance tickers: US `AAPL`, A-share `603083.SS` (Shanghai) /
  `000001.SZ` (Shenzhen), HK `0700.HK`, exchange ETF `510300.SS`, crypto `BTC-USD`, futures `GC=F`. China
  off-exchange (OTC) mutual funds are `FUND:<6-digit code>`, e.g. `FUND:110011`. A bare 6-digit code has no
  data: when the user gives one, add the suffix or the `FUND:` prefix yourself; if `run_committee` answers
  "无 X 行情数据（六位代码请写全…）", retry with the candidates it lists instead of giving up.
- **Never write `memory/` directly.** All state changes go through CLI subcommands / MCP tools (atomic write +
  fcntl lock + audit trail). Direct editing causes schema drift + concurrent-write corruption.
- Always obtain the backend's current context. Backend cache qualification checks source,
  model, policy and pending requests; an existing same-day Markdown file is insufficient.
- Do not assign a fixed confidence because workers disagree. Keep the model's actual output
  for audit; self-reported confidence is not a measured win probability.
- Quote `confidence_lookup` only when the backend returns a currently qualified table.
  Preserve its source and sample limitations. Missing or insufficient evidence means
  “样本不足”; do not substitute bundled fleet defaults or a fixed confidence. A HOLD
  sideways-outcome share measures market movement, not trading accuracy. Constrained,
  untrusted and incomplete analysis must be named separately from a market HOLD.
- **Do not leak the user's email or other personally identifying information** — never hard-code it in output.

## Where to look when something breaks

Read the `doctor` JSON output carefully. Every check has a `hint` field telling you how to fix it;
checks marked `optional_*` are not faults, they only say which path/feature (Direct, e-mail) is unavailable; a `warning` check (e.g. `gold_booking`: a GC=F row not booked in CNY per gram) means a data fix is due, not that onboarding is unfinished — follow its hint with the user, do not switch to `invest-setup`.
If doctor is all green but a subcommand still fails, read `references/troubleshooting.md`.

Common Direct-path errors:
- `error: DEEPSEEK_API_KEY 未设` (backend's verbatim "key not set" message) → no key in `.env`; run `run.sh init` to configure it
- `error: asset X not in strategy.target_assets` → add X to the strategy first,
  see `references/adding-assets.md`

## References index

| File | When to read |
|------|--------|
| `references/tools.md` | Full subcommand table + Web API endpoint table (long-tail operations MCP does not cover) |
| `references/committee-protocol.md` | Running the committee on the Coordinator path (Claude Code only) |
| `references/two-paths.md` | Understanding Coordinator vs Direct / DeepSeek cron triggering |
| `references/adding-assets.md` | User wants to track a new symbol |
| `references/troubleshooting.md` | doctor all green but still failing |
| `references/onboarding.md` | **First-time install goes to the `invest-setup` skill**; this file is kept as a detailed reference |

For deeper architectural context see the project wiki:
[github.com/longsizhuo/openInvest/tree/main/docs/wiki](https://github.com/longsizhuo/openInvest/tree/main/docs/wiki)
