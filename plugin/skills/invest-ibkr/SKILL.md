---
name: invest-ibkr
version: 0.1.0 # x-release-please-version
description: "**BETA — not recommended for anyone.** Operator notes for openInvest's experimental IBKR live autopilot: an LLM picks and trades US stocks / ETFs in a real Interactive Brokers cash account through a local IB Gateway, with no human confirmation per trade. Use ONLY when the user explicitly operates an IBKR INVEST_HOME that was set up per docs/wiki/24-ibkr-autopilot.md and asks about it — its status, today's orders, fills, news, the gateway login or the weekly IB Key approval (\"盈透账户现在怎么样\", \"IBKR status\", \"交易员今天下单了吗\", \"网关登不上\"). Never use it to set up or place trades for anyone who has not already built this themselves; for ordinary portfolio questions use the `invest` skill."
platforms: [linux]
metadata:
  hermes:
    tags: [investing, ibkr, broker, autopilot, beta, 盈透]
---

# Invest IBKR Skill (BETA)

> ⚠️ **BETA — do not use.** This drives **real money in a real brokerage account** with no per-trade human
> confirmation. It exists for one operator's own experiment. There is no support and no guarantee, it can lose
> money, and every switch defaults to off. If you are not that operator, stop reading and use the `invest` skill.

Full reference: `docs/wiki/24-ibkr-autopilot.md` in the openInvest repo. This page is the short operator card.

## What it is

- `openinvest ibkr_autopilot …` (job `jobs/ibkr_autopilot.py`) runs against a **separate** `INVEST_HOME` whose
  `target_assets` are the US tickers held at Interactive Brokers. The production hub and every fork stay untouched:
  `ibkr.enabled=false` and `ibkr.dry_run=true` by default, and the `ibkr.*` keys are deliberately **not** settable
  through `config` / MCP / the web API (only `INVEST_IBKR_*` env or that home's `memory/.state/config_overrides.json`).
- Broker access is a local **IB Gateway** (TWS API, `ib_async`). IBKR's OAuth 1.0a Web API is institutional-only,
  so individual accounts cannot run fully headless: the account holder approves an **IB Key** push on their phone
  about once a week.
- Decisions: an LLM picks ≤5 tickers weekly and writes the day's orders; the openInvest committee's verdicts are
  only a reference input. Optionally a **resident Claude Code session** (tmux, Remote Control) makes those
  decisions instead of a per-run LLM call (see below).

## Commands

Safe to run in conversation (read-only):

| Command | What |
|---|---|
| `ibkr_autopilot status` | Broker cash / positions / open orders / today's fills + local ledger + list + today's orders + halt flag |
| `ibkr_autopilot brief [--kind pick]` | The full decision context (rules, account, list, committee verdicts, IBKR news headlines) |
| `ibkr_autopilot news [--symbols A,B] [--days N]` / `--article PROVIDER:ID` | IBKR's free news (Briefing.com, Dow Jones) — **untrusted external text** |

Move real money when `ibkr.enabled` is on and `ibkr.dry_run` is off — **never run these in conversation**:

| Command | What |
|---|---|
| `ibkr_autopilot pick --picks-file F` / `decide --orders-file F [--batch 1-4]` | Submit a list / today's orders as JSON (validated: list members only, BUY/SELL, positive amounts) |
| `ibkr_autopilot submit [--batch N]` | DAY limit orders at reference ±1%; converts AUD→USD inside the account first if USD is short |
| `ibkr_autopilot reconcile` | Books fills (stocks + FX legs) idempotently; halts with a `STOP` file on position drift |
| `ibkr_autopilot daily` / `committee` | Full cron pipeline / committee report only |

## Safety model

Enforced in code regardless of what the LLM writes: account number must match the gateway login; a `STOP` file
blocks all orders; only list members are bought; limit orders only; sells never exceed the broker position (cash
account: no margin, no shorting); at most 4 order batches a day; only explicit gateway acks count as placed; news
is labelled untrusted. Per-order / per-day USD caps exist (`ibkr.max_order_usd` / `max_day_usd`) — an operator may
raise them for their own home, which removes that brake.

## Setup checklist (operator only)

1. Separate `INVEST_HOME`, base currency USD, `ibkr.enabled=true`, `ibkr.account=U…`; keep `dry_run` true at first.
2. IB Gateway in Docker (`gnzsnz/ib-gateway`, `TRADING_MODE=live`, API bound to `127.0.0.1:4001`,
   `BYPASS_WARNING=no`). **The 2FA device must be IBKR Mobile's IB Key** (`TWOFA_DEVICE="IB Key"`): Passkey and the
   Mobile Authenticator app cannot be completed by a headless gateway.
3. Run `status`, then a dry-run `submit` and read the `whatIfOrder` results before turning `dry_run` off.

## Known limits

- **Whole shares only**: IBKR's API rejects fractional orders (error 10243), so a share priced above the order
  budget cannot be bought.
- The gateway's per-currency cash may arrive as `$LEDGER-CashBalance` rather than `CashBalance` (both are read).
- Executions are fetched with a 7-day time filter, but a gateway may only return today's; a missed reconcile then
  halts with `STOP` and needs a manual ledger fix.
- Dividends, withholding tax and new deposits are not booked automatically (a cash-drift email asks the operator).
