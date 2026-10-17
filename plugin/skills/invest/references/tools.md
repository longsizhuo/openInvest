# Tool Catalog (CLI subcommands + Web API endpoints)

> This file is the **tool documentation** — SKILL.md only covers workflow (issue #133 Decision 6:
> Tool Usage is delegated to MCP schema auto-discovery; the Skill shrinks to the orchestration protocol).
>
> - **MCP users** (Claude Code plugin / codex mcp): the tools come with auto-discovered schemas,
>   so you usually don't need this file; only consult this table for the long-tail endpoints MCP
>   doesn't cover (trades/config/events/...)
> - **CLI/REST agents** (Gemini / Cursor / scripts): this file is the complete reference

## Subcommand overview

| Command | Path | Use for | Returns |
|------|------|------|------|
| `doctor` | Universal | Mandatory first step | JSON, `status: "ready"` or `"needs_setup"` |
| `init [--from-stdin] [--force]` | Universal | **Not used in this skill** — first-time installation goes through the `invest-setup` skill | — |
| `status` | Universal | View portfolio | cash + holdings + live prices + P&L + `performance` (same block as below) |
| `performance` | Universal | Real-account performance: "how am I doing / 我的收益率 / 跑赢大盘没" | TWR 30d / YTD / since inception, XIRR (money-weighted, needs ≥30 days), max drawdown with peak→trough dates, annualised volatility, return vs the configured benchmark (`portfolio.benchmark_symbol`, default 510300.SS) and `data_quality` (forward-filled days, cost-fallback symbols, `unreconciled` = opening position inferred from `portfolio.md` − ledger). `{"status": "insufficient_history"}` until the ledger spans 2 trading days. Base currency, zero LLM |
| `strategy` | Universal | View strategy | target_assets + Dreaming insights |
| `history [-n N]` | Universal | View the transaction log | last N trades + committee verdicts |
| `live_prices` | Universal | Background market data | VIX / TNX / USDCNY / AUDCNY / NDQ / GC=F |
| `discipline` | Universal | "what did the committee block / how is my discipline" (委员会拦了什么/纪律如何) | inaction rate (HOLD share of **live** verdicts) + count of blocked impulsive actions + counterfactual money saved/lost (read-only, zero LLM, aligned with ADR-023). Equivalent to `GET /api/discipline` |
| `decisions [--days N] [--symbol S] [--verdict V] [--limit N]` | Universal | "how many recommendations did I follow / which ones weren't executed" (我听了几次建议/哪些没执行) | verdict↔intervention↔execution↔outcome join + adoption rate (read-only, zero LLM). Lists the newest 20 by default (`--limit 0` = all); `count` / `summary` cover every match. Same data as `GET /api/decisions` (issue #133 Decision 9) |
| `explain_decision DECISION_ID` | Universal | "why was today's verdict HOLD / why did it tell me to sell" (为什么今天是 HOLD/为什么让我卖) — replay one past verdict. `DECISION_ID` is `<date>/<symbol>` exactly as `decisions` prints it, e.g. `2026-07-03/GC=F` | verdict / confidence / alloc_cny + `confidence_lookup` (quote this next to the verdict, not the self-reported confidence) + the full 4-role debate `transcript_markdown` with the CIO memo (render it) + `path_snapshot` (may be null; always null in REST remote mode, whose hub endpoint doesn't serve it). Read-only, zero LLM. Same output as MCP `explain_decision` |
| `ingest_event` | Write | agent feeds news into the event ledger (normalization + severity grading, idempotent; requires backend LLM key). Holding-relevant severity ≥ mid non-neutral items auto-trigger a rate-limited committee re-run (`committee_task_id` in the result); disabled in advisory mode | `--title --url [--snippet --source --ts]` |
| `record_execution DECISION_ID [--rejected] [--reason "..."]` | Universal, write | Write back when the user says "I didn't buy / I bought it" (我没买/我买了) | Idempotent append to executions.jsonl. **When the user rejects a recommendation, proactively ask why before recording** (you are the collection end of the Reason Loop). Equivalent to `POST /api/decisions/execution` |
| `what_if [--symbol X --pct N \| --gold-pct N \| --ndq-pct N]` | Universal | "how much do I lose if X drops Y%" (X 跌 Y% 我亏多少) | Arithmetic scenario, no LLM |
| `rebalance_plan [--mode hybrid\|cash_only\|sell_to_rebalance] [--band-pct N]` | Universal | "do I need to rebalance / what should I buy or sell to get back to target" (要不要再平衡/该买卖多少) — compares holdings with `strategy.md` targets (`target_assets[].target_pct`; assets without one split `target_allocation_stock` equally, marked `target_source: equal_split`). Equivalent to MCP `rebalance_plan` | List only, places no order: `rows` (per asset: current/target/drift pct, `action` BUY/SELL/NONE, `amount` in base currency, `units` rounded down to whole lots — A-share 100, convertible bond 10, off-exchange fund by amount, HK/others 1 with `lot_note`, `reason`), `totals` (cash_after / turnover / n_trades), `unmanaged`, `warnings`. Within ±band (default 5pp) → NONE. Modes: `cash_only` spends only cash above the target cash ratio and never sells; `hybrid` (default) uses that cash first and sells overweights only to cover the gap; `sell_to_rebalance` sells every overweight back to target. Read-only, zero LLM |
| `correlate --symbols A,B[,C...] [--period 6mo] [--with-llm]` | "btw" side-query | The user **asks in passing** "do A and B move alike?" (A 跟 B 像不像) (no writes to memory/.committee; pure query, returns results) | pairwise correlation matrix + sector + macro linkage |
| `prepare_committee SYM` | Coordinator | Get the brief for the 4 subagents | brief JSON + 6 prompt sections |
| `save_committee SYM` | Coordinator | Persist the transcript to disk | 4-section output via stdin → markdown |
| `run_committee SYM [--force]` | Direct | One-shot full committee | verdict JSON + CIO memo |
| `deposit -c CCY -a N` | Universal, write | Deposit cash (any currency) | JSON new balance |
| `withdraw -c CCY -a N` | Universal, write | Withdraw cash; errors on insufficient balance | JSON new balance |
| `buy --symbol S --units N --price P [-c CCY] [--kind etf/equity/...] [--existing-position]` | Universal, write | Add to / open a position (weighted average cost), paid from ledger cash (refuses if cash is short). `--existing-position` (MCP: separate tool `record_existing_position`; an older MCP server answers `Unknown tool` — don't fall back to `buy`): record a position the user held **before** onboarding that `status` doesn't list yet (re-adding a listed symbol counts it twice) — cash unchanged, history `funding_source: external_funding`, `source: skill_cli:existing_position`; refused in REST remote mode. Does not add the symbol to `target_assets` (that's `track_asset`) | JSON action + estimated cost + `funding_source` |
| `sell --symbol S --units N --price P` | Universal, write | Reduce a position (returns cash per the holding's cost_currency) | JSON remaining units |
| `delete_holding --symbol S [--force]` | Universal, write | Delete a holding row (units must be 0, or use --force) | JSON deleted |
| `import [--file F \| --text T] [--commit]` | Universal, read/write | Free-text/CSV holdings description → LLM-parsed structured holdings (broker-position paste, bulk entry). Preview-only by default; `--commit` performs a non-destructive write (only adds new symbols; cash only fills currencies currently at 0; re-importing is idempotent). Equivalent to POST /api/holdings/import. `parsed.warnings` (present only when non-empty) names each off-exchange fund whose amount could not be converted to units (stays 0 units) — tell the user | JSON `{parsed, committed, summary?}` |
| `config [--set KEY VALUE] [--clear KEY]` | Universal, read/write | Read/modify the whitelist of API-configurable parameters (concentration_lens / **cash_opportunity_cost_rule** (opportunity-cost rule, default OFF, ADR-024) / risk_profile / gold_defense_dca / dreaming.llm_verify / **dca.auto_dca_enabled / dca.auto_dca_amount_cny** — auto-DCA switch and amount, ADR-018 / **event.watch_schedule** — event_watch scan-window crontab (interpreted in Asia/Shanghai; default Beijing 8:00 to 2:30 next day; the scheduler picks changes up automatically within ≤10 minutes, no restart needed) / **event.sentinel_enabled / event.sentinel_atr_mult / event.sentinel_cooldown_min / event.sentinel_schedule** — price-anomaly sentinel (vertical-move detection; sends an alert email first, then triggers the committee, ADR-025): master switch / trigger multiple (× daily ATR, default 0.8) / cooldown minutes for the same symbol + same direction (default 120) / scan-window crontab (default every 5 minutes). There are also several event.*/staleness.* keys — see GET /api/config for the full list). No args = read everything. Equivalent to GET/PUT /api/config (ADR-017) | JSON of all effective values |
| `track_asset --symbol S [--display-name N] [--channel C] [--max-single-invest-cny N] [--price-offset-pct P] [--sell-fee-pct P]` | Universal, write | Add or update a tracked asset in `target_assets` (committee coverage; places no order, touches no holdings). A new asset without `--max-single-invest-cny` inherits the strategy's single-entry cap; on an update, pass only the fields you change | JSON |
| `untrack_asset --symbol S` | Universal, write | Stop tracking an asset (removes it from `target_assets`; holdings are not touched) | JSON |
| `set_allocations --stock X --cash Y` | Universal, write | Set the target stock / cash ratios (0–1 each) | JSON |
| `news_sources [--add NAME URL] [--remove NAME_OR_URL]` | Universal, read/write | No args: list news sources (built-in + your extra RSS feeds). `--add` adds a feed after a live probe; `--remove` deletes one of your extra feeds | JSON source list |
| `event_check [--live] [--recall SYMBOL]` | Universal | Run one news-sentinel scan. Dry-run by default (no email, no committee); `--live` sends alerts and triggers the committee; `--recall SYMBOL` only tests event recall for that symbol | JSON summary |
| `daily_report` | cron | Full daily report over all tracked assets, with email. Burns LLM tokens — **do not run it proactively in conversation** (see SKILL.md constraints) | report |

**The subcommand names are a closed set — any command not in the table above does not exist.**
When you catch yourself wanting to call `get_committee_context` / `analyze_asset` / `pull_brief`
or names like that, stop and check against the table — you are most likely hallucinating a
nonexistent command name and should pick `prepare_committee` or `run_committee` instead.

All output is JSON. **Always quote numbers from the JSON**, never from the `memory/*.md`
markdown (the markdown body is rendered from the frontmatter and may lag slightly behind).

### Importing holdings from broker-app **screenshots** (you do the OCR; zero backend dependency)

When the user sends a **screenshot** of broker holdings: **read the image yourself** (you have
vision), convert each row into text as `symbol/units/cost/currency/channel`, then run
`import --text "..."` (or POST /api/holdings/import). The backend `import`'s LLM parses **text
only** — do not feed it the image (DeepSeek and most chat models don't accept images and will
error). In short: screenshot → you transcribe to text → import. This is more accurate than
backend OCR and doesn't depend on the backend model. Run `--text` first (without `--commit`)
so the user can verify the preview, then `--commit` for the non-destructive write.

## Web API write endpoints (agents can call these too)

**Product philosophy**: the agent (you) has access to ALL of openInvest's functionality.
**Prefer CLI subcommands / MCP tools**; only curl the endpoints below (default :8765) for
long-tail operations not covered by CLI/MCP. The Web API is marked deprecated (the GUI is
retired; the remaining endpoints serve remote hub mode, and no new endpoints will be added).
For remote scenarios, prefer the hub's remote MCP (`openinvest-mcp --http`, all tools direct);
REST forwarding only backfills the long tail MCP doesn't cover.
If the hub sets `INVEST_API_TOKEN`, every `/api/*` call (except `/api/health`) must carry
`-H "Authorization: Bearer $INVEST_API_TOKEN"` — loopback included; otherwise it returns 401.

Call these when the user says "record a trade" (记一笔交易) / "I plan to buy X" (我打算买 X) /
"mark as executed" (标记成交) / "add a new asset" (加新资产):

| Endpoint | Use for | Example body |
|------|------|-----------|
| `POST /api/trades/record` | **Record an intended trade** (no real payment connection; internal ledger only) | `{symbol, direction: "BUY"\|"SELL", units, price?, intended_date?, note?}` |
| `GET /api/trades?limit=N` | View the last N intended / executed trades | — |
| `PATCH /api/trades/{id}/status` | **Mark as executed** (status: "executed") → auto-syncs portfolio.md (updates holdings + deducts cash) | `{status: "executed"}` |
| `POST /api/holdings` | Add a yfinance-tracked asset (places no order; only records holding data) | `{symbol, kind, units, avg_cost, cost_currency, channel?}` |
| `POST /api/holdings/import` | Free-text/CSV holdings description → LLM parse (broker-position paste, bulk entry). `commit:false` previews only, no write; `commit:true` non-destructive write (only adds new symbols; cash only fills currencies currently at 0). Requires backend LLM key | `{content, commit?}` |
| `PUT /api/holdings/{symbol}` | Modify holding fields | `{units?, avg_cost?, channel?}` |
| `POST /api/deposit` / `/api/withdraw` | Adjust cash | `{currency: "CNY"\|"AUD"\|..., amount}` |
| `POST /api/gold/buy` / `/sell` | Gold buy/sell (sell_fee computed automatically) | `{grams, price_per_gram}` |
| `POST /api/strategy/asset` | Add a target_assets entry. **Native equivalents already exist**: MCP `track_asset` / CLI `track_asset` (plus `untrack_asset`, `set_allocations`) — prefer the native ones; stop curling this | `{symbol, channel?, max_single_invest_cny}` |
| `GET /api/events/recent?hours=24&min_severity=low&limit=50` | List news perceived by the event layer in the last N hours (ADR-006). For debugging / "what is the system currently aware of" (系统现在感知到什么) | — |
| `GET /api/discipline` | Committee discipline ledger: inaction rate (HOLD share of live verdicts) + count of blocked impulsive actions + counterfactual P&L (aligned with ADR-023; lets the agent show "what it blocked") | — |
| `GET /api/verdict_review/summary` | Verdict hit rates split into `live` / `backtest` / `contaminated` buckets (ADR-022). Only `live` is performance; never sum buckets; a `null` rate means n<30; weekend duplicates (weekend-closed assets, Friday's close reused) are excluded and counted in `weekend_dup_excluded` | — |
| `GET /api/decisions?days=90` | Unified decision view: verdict↔intervention↔execution↔outcome join + adoption rate (issue #133 Decision 9) | — |
| `POST /api/decisions/execution` | Write back the user's execution/rejection of a verdict + the reason (idempotent, ADR-016) | `{decision_id: "2026-07-03/GC=F", executed: false, reason?: "..."}` |
| `POST /api/events/check` | Manually run event_watch once (fetch news + normalize + store + trigger the committee on hits). Synchronous, 30-90s | — |
| `GET /api/config` | View the current effective values of the API-configurable whitelist parameters (+ whether overridden + metadata) | — |
| `PUT /api/config` | Set one whitelist override (persisted to disk, shared across processes, takes precedence over env; ADR-017) | `{key, value}`, e.g. `{"key":"verdict.concentration_lens_enabled","value":false}` |
| `DELETE /api/config/{key}` | Delete an override, reverting to the default | — |

**Typical flow**: the user says "I plan to..." (我打算...) / "just bought X" (刚买了 X) / "my
position in Y grew" (我的持仓多了 Y) → use `POST /api/trades/record` (with intended_date to
distinguish planned vs executed) → once actually executed, use `PATCH .../status executed`;
the backend **automatically updates portfolio.md** (weighted average cost + cash deduction) —
you don't need to call anything else.

**Full OpenAPI**: `http://127.0.0.1:8765/openapi.json` lists every endpoint + Pydantic schema.
