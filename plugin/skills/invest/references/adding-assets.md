# Adding a new asset (read when the user wants to track AAPL / TSLA / 005827 etc.)

Onboarding only records the cash and holdings the user lists; there are no default assets. The
v2 schema supports any yfinance symbol. Three ways to add one, ordered by preference:

## Method 1: `track_asset` (+ record the position if the ledger doesn't have it yet)

```bash
# analyze it: adds the symbol to strategy.target_assets (what the committee / DCA cover)
~/.claude/skills/invest/scripts/run.sh track_asset --symbol AAPL --max-single-invest-cny 8000
```

Recording a position is separate — `buy` never touches `target_assets`; it only puts the position
into `status` / P&L. First run `run.sh status`: a symbol it already lists is in the ledger, so
**don't record it again** (that counts it twice). Otherwise:

```bash
# held BEFORE using openInvest, not yet listed by `status`: cash is not touched
~/.claude/skills/invest/scripts/run.sh buy --symbol AAPL --units 100 --price 150 -c USD --kind equity --existing-position
# a new purchase made now: paid from ledger cash (refuses if that cash is short)
~/.claude/skills/invest/scripts/run.sh buy --symbol AAPL --units 100 --price 150 -c USD --kind equity
```

MCP users: `track_asset`; `record_existing_position` for the first case (an MCP server older than
that tool answers `Unknown tool` — don't fall back to `buy`, it deducts cash); `buy` for the second.
Weighted average cost is computed automatically. Ask which case it is: recording an already-held
position as a plain `buy` shrinks the cash the user reported by its cost.

**"I just want to watch it, not hold it"**: `track_asset` alone (native entry point since issue #179).
It is an idempotent upsert: re-tracking doesn't error, it only updates the fields you pass.
`untrack_asset` removes a symbol, and `set_allocations` changes the stock/cash target allocation.
Only if you still need "a zero-unit row shown in the holdings table" for display purposes should
you use `POST /api/holdings` with `is_tracking_only: true` — or skip persistence entirely and
just analyze (Method 3).

## Method 2: REST API (long tail: tracking-only positions / remote hub)

```http
POST /api/holdings
Content-Type: application/json
Authorization: Bearer $INVEST_API_TOKEN   # only when the hub sets INVEST_API_TOKEN

{
  "symbol": "AAPL",
  "kind": "stock",
  "units": 0,
  "unit_label": "股",
  "avg_cost": 0,
  "cost_currency": "USD",
  "channel": "Robinhood",
  "is_tracking_only": true
}
```

`kind` enum: `stock` / `etf` / `metal` / `crypto` / `bond` / `fund` / `other`.

The Web API is deprecated (it only serves remote hub mode) — prefer CLI `buy` wherever it covers
the scenario; only curl for fields the CLI doesn't expose, such as `is_tracking_only`.

## Method 3: the user only wants analysis, no persistence

If the user says **"should I buy TSLA" (该不该买 TSLA)** but doesn't want TSLA added to the
portfolio yet, analyze it directly:

```bash
~/.claude/skills/invest/scripts/run.sh prepare_committee TSLA
```

The committee can analyze any yfinance symbol, whether or not it's in holdings. The output lands
in `memory/.committee/<date>/TSLA.md` for history, but TSLA does not enter the portfolio.

Use this when:
- The user is brainstorming and not committed to tracking
- The symbol is one-off (e.g. reacting to news)
- The user explicitly says "just give me your take, don't add it"

## yfinance symbol formats

The underlying data source is yfinance. Common formats:

| Market | Format | Examples |
|------|------|------|
| US stocks | bare ticker | `AAPL`, `TSLA` |
| US ETFs | bare ticker | `SPY`, `QQQ` |
| ASX (Australia) | `XXX.AX` | `NDQ.AX`, `BHP.AX` |
| HKEX (Hong Kong) | `XXXX.HK` | `0700.HK`, `9988.HK` |
| Shanghai (SSE) | `XXXXXX.SS` | `600519.SS`, `510300.SS` (ETF) |
| Shenzhen (SZSE) | `XXXXXX.SZ` | `000001.SZ` |
| LSE (London) | `XXX.L` | `BP.L`, `HSBA.L` |
| TSE (Tokyo) | `XXXX.T` | `7203.T` |
| Crypto | `XXX-USD` | `BTC-USD`, `ETH-USD` |
| FX rates | `XXXYYY=X` | `USDCNY=X`, `AUDCNY=X` |
| Commodity futures | `XX=F` | `GC=F` (gold), `CL=F` (crude oil) |

If the user says "AAPL", use it as-is. If they say "茅台" (Moutai), convert it to `600519.SS`
first before passing it to the API.

**Chinese off-exchange mutual funds (场外公募基金, bought via Alipay / bank apps)** are not on
yfinance and must NOT get a `.SS` / `.SZ` suffix. Use `FUND:<6-digit code>` with `--kind fund`:

```bash
# 用户在用 openInvest 之前就已持有、且 `run.sh status` 里还没有的基金：加 --existing-position 补录，不扣现金
~/.claude/skills/invest/scripts/run.sh buy --symbol FUND:123456 --units 5000 --price 2.0 -c CNY --kind fund --unit-label 份 --existing-position
```

(Skip it if `status` already lists the fund. Drop `--existing-position` only for a new purchase
paid from the recorded cash.)

They are valued at the latest confirmed unit NAV from Eastmoney (not the intraday estimate), so
`status` / P&L / the committee's portfolio summary all include them. Running the committee *on* a
fund itself is not supported yet (no price history wired in). If the user only knows the holding
amount and holding P&L (持有金额 / 持有收益), use `import_holdings` — it derives units and average
cost from the latest NAV.

## What yfinance does NOT support

Be honest with the user:

- ❌ Bank wealth-management products (e.g. CMB 朝朝盈)
- ❌ Yu'ebao (余额宝) / treasury reverse repos
- ❌ Private funds / trusts
- ❌ Unlisted REITs
- ❌ Crypto on minor exchanges (only mainstream pairs like `BTC-USD` are supported)

These would require adding a new data source — see
[docs/wiki/07-extending.md#2-加新数据源](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/07-extending.md#2-加新数据源).
You can't add one for the user on the spot (it requires code changes), but you can point them to
that doc.

## Confirming the addition

Afterwards, have the user run (or run it for them) `~/.claude/skills/invest/scripts/run.sh status`
and check whether the new symbol appears in `all_holdings`.
