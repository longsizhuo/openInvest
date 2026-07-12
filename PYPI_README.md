# openInvest

<!-- mcp-name: io.github.longsizhuo/openinvest -->

**An investment runtime built for AI agents.** Your agent knows you — openInvest knows investing.

openInvest runs a 4-role LLM investment committee (Macro Strategist, Quant Analyst, Risk Officer, CIO) that debates any asset and returns a BUY / ACCUMULATE / HOLD / TRIM / SELL verdict with a written memo — plus portfolio tracking, a decision ledger, and full decision-accounting (which advice you followed, what happened after).

It is **not a chatbot**. Conversation, memory, and personalization stay in your agent (Claude Code, Codex, Hermes, OpenClaw, Cursor, or any script); openInvest provides the investing capabilities behind it.

## Install & run

```bash
# no install needed — uvx fetches and caches it
INVEST_HOME=~/openInvest uvx openinvest status

# or install it
pip install openinvest
```

`INVEST_HOME` is your data directory (portfolio, ledgers, `.env`). All state lives there — the package itself is stateless.

### First-time setup

```bash
uvx openinvest init          # interactive onboarding (or `--from-stdin` for agents)
uvx openinvest doctor        # health check
```

### As an MCP server (recommended for agents)

18 tools — portfolio read/write, live prices, decision ledger, and the committee — over stdio (or `openinvest-mcp --http` for a remote streamable-HTTP server, BETA):

```bash
claude mcp add openinvest -e INVEST_HOME=~/openInvest -- uvx openinvest-mcp
```

Read tools carry `readOnlyHint`; money-moving tools (`buy`, `sell`, `deposit`, `withdraw`) carry `destructiveHint`, so MCP clients gate them behind confirmation.

### As an agent plugin (Claude Code / Codex / Hermes)

Skills (committee orchestration protocol) + MCP server, bundled:

```
# Claude Code
/plugin marketplace add longsizhuo/openInvest
/plugin install invest@openinvest

# Codex
codex plugin marketplace add longsizhuo/openInvest

# Hermes Agent
hermes plugins install longsizhuo/openInvest --enable
```

## What it does

- **Investment committee** — 4 LLM roles debate any `yfinance` symbol (equities, ETFs, crypto, commodities, any currency). Two execution paths: *Coordinator* (your agent plays the roles — no API key) or *Direct* (backend LLM, needs `DEEPSEEK_API_KEY`).
- **Portfolio management** — multi-currency cash + holdings with an append-only, fcntl-locked ledger. Never places real orders.
- **Decision accounting** — every verdict is joined at read time with rule interventions, your executions (or refusals, with reasons), and post-hoc PnL. Ask it: *"How often did I actually follow the committee?"*
- **Discipline ledger** — counterfactual PnL of what the safety rules blocked.

## CLI

```bash
uvx openinvest status                    # portfolio + live P&L
uvx openinvest run_committee GC=F        # one-shot committee verdict
uvx openinvest decisions --days 90       # decision ledger + adoption rate
uvx openinvest record_execution 2026-07-03/GC=F --rejected --reason "too pricey"
uvx openinvest buy --symbol AAPL --units 10 --price 210 --currency USD
```

## Disclaimer

LLM-driven decision support — **not investment advice**. LLMs make mistakes and get overconfident. openInvest never auto-trades; its ledger is a local SQLite record with no connection to any brokerage or payment system. Past performance does not predict future returns.

## Links

- [Repository & full docs (中文)](https://github.com/longsizhuo/openInvest)
- [Wiki](https://github.com/longsizhuo/openInvest/tree/main/docs/wiki)
- [RFC: Agent-Native Investment Runtime](https://github.com/longsizhuo/openInvest/issues/133)
- License: MIT
