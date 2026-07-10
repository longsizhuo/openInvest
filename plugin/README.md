# invest — openInvest Claude Code plugin

Self-hosted multi-asset **AI investment committee** for Claude Code. View your
portfolio and live prices, track any `yfinance` symbol (A-share / HK / US /
ETF / crypto / commodity, any currency), and run a 4-role LLM committee that
debates and returns a BUY / ACCUMULATE / HOLD / TRIM / SELL verdict with a
written memo.

> 中文完整文档见仓库根 [README.md](https://github.com/longsizhuo/openInvest#readme)
> 与 [docs/wiki](https://github.com/longsizhuo/openInvest/tree/main/docs/wiki)。

## Install

**Claude Code:**
```
/plugin marketplace add longsizhuo/openInvest
/plugin install invest@openinvest
```

**Codex** (same plugin, via the Codex marketplace):
```
codex plugin marketplace add longsizhuo/openInvest
codex plugin add invest@openinvest
```
Codex reads the same `SKILL.md` (agentskills.io). For the MCP tools, register
the server once after the backend bootstraps:
```
codex mcp add openinvest --env INVEST_HOME=$HOME/openInvest -- uvx openinvest-mcp
```

Then in chat say **"set up invest" / 帮我初始化 invest** — a 5-question
onboarding (name, risk tolerance, income, current holdings, optional API key)
writes your config. After that just ask things like *"show my portfolio"*,
*"how's my P&L"*, *"should I buy AAPL"*, or *"run committee on gold"*.

## What actually gets installed

The plugin ships the **agent skill layer** (`invest` + `invest-setup`) plus an
**MCP server** (`.mcp.json`, auto-registered on install — 15 tools: `status`,
`live_prices`, `decisions`, `explain_decision`, `record_execution`, `buy`,
`sell`, `run_committee`, …). The backend ships on **PyPI**
([`openinvest`](https://pypi.org/project/openinvest/)): on first call `run.sh`
fetches it via `uvx` (cached by uv, no clone, no venv juggling) and keeps your
data in `~/openInvest` (portfolio, ledgers, `.env`). Update anytime with
`run.sh update`. Nothing is installed system-wide.

> **First-run note**: the very first call downloads the package from PyPI
> (a few seconds); if the first MCP connect times out, just retry once — after
> that uvx serves from cache and MCP starts in ~1s.

Division of labor (per [issue #133](https://github.com/longsizhuo/openInvest/issues/133)):
**MCP tools** = what the agent can call; **skills** = how to orchestrate the
committee (Coordinator protocol, decision discipline). Your agent keeps
conversation, memory, and personalization; openInvest keeps the investing.

Two ways to run the committee:

- **Coordinator** (default in Claude Code): Claude spawns the 4 roles as
  subagents — **no API key needed, no third-party LLM token cost**.
- **Direct** (any agent / script): `run.sh run_committee <SYM>` runs the 4
  roles via a backend LLM — needs `DEEPSEEK_API_KEY`.

The 4 roles: **Macro Strategist** (VIX / rates / FX), **Quant Analyst**
(technicals, blind to your holdings), **Risk Officer** (concentration / tail
risk, blind to the signals), then a **CIO** synthesizes a verdict + confidence.
The system never places orders — decisions stay with you.

## Requirements

- Claude Code
- [`uv`](https://docs.astral.sh/uv/) — the launcher checks for it and tells you
  how to install if missing (backend + Python come from PyPI via uvx)
- Optional: `DEEPSEEK_API_KEY` (only for the Direct path / cron daily report)

## Disclaimer

LLM-driven decision-support tool. **Not investment advice.** LLMs make
mistakes, get overconfident, and miss things. The system never auto-trades — try
`what_if` on a small amount for two weeks before using real money. The internal
ledger (`db/trades_db.py`) is a local SQLite record and **does not connect to
any real payment or brokerage**. Any public hit-rate / PnL data is the author's
own account history; **past performance does not predict future returns**.

## Links

- Repository & full docs: <https://github.com/longsizhuo/openInvest>
- Wiki: <https://github.com/longsizhuo/openInvest/tree/main/docs/wiki>
- License: [MIT](https://github.com/longsizhuo/openInvest/blob/main/LICENSE)
