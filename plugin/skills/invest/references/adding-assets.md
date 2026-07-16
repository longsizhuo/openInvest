# Adding a new asset (read when the user wants to track AAPL / TSLA / 005827 etc.)

The default onboarding configures only two assets (NDQ.AX + GC=F). The v2 schema supports any
yfinance symbol. Three ways to add one, ordered by preference:

## Method 1: CLI `buy` (preferred, when the user actually holds it)

```bash
~/.claude/skills/invest/scripts/run.sh buy --symbol AAPL --units 100 --price 150 -c USD --kind stock
```

MCP users call the `buy` tool directly with the same parameter names. Weighted average cost is
computed automatically, and the symbol is automatically added to tracking.

**"I just want to watch it, not hold it"** scenario (native entry point since issue #179):
```bash
~/.claude/skills/invest/scripts/run.sh track_asset --symbol AAPL --max-single-invest-cny 8000
```
MCP users call the `track_asset` tool directly (idempotent upsert: re-tracking doesn't error,
it only updates the fields you pass). It adds the symbol to the strategy's tracking list — which
determines the coverage of the committee/DCA; `untrack_asset` removes it, and `set_allocations`
changes the stock/cash target allocation.
Only if you still need "a zero-unit row shown in the holdings table" for display purposes should
you use `POST /api/holdings` with `is_tracking_only: true` — or skip persistence entirely and
just analyze (Method 3).

## Method 2: REST API (long tail: tracking-only positions / remote hub)

```http
POST /api/holdings
Content-Type: application/json

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
| Shanghai (SSE) | `XXXXXX.SS` | `600519.SS`, `005827.SS` (mutual fund) |
| Shenzhen (SZSE) | `XXXXXX.SZ` | `000001.SZ` |
| LSE (London) | `XXX.L` | `BP.L`, `HSBA.L` |
| TSE (Tokyo) | `XXXX.T` | `7203.T` |
| Crypto | `XXX-USD` | `BTC-USD`, `ETH-USD` |
| FX rates | `XXXYYY=X` | `USDCNY=X`, `AUDCNY=X` |
| Commodity futures | `XX=F` | `GC=F` (gold), `CL=F` (crude oil) |

If the user says "AAPL", use it as-is. If they say "茅台" (Moutai), convert it to `600519.SS`
first before passing it to the API.

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
