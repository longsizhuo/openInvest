# openInvest — agent install guide (llms-install.md)

Everything ships on PyPI; no cloning, no venv setup. Only prerequisite: [`uv`](https://docs.astral.sh/uv/)
(`curl -LsSf https://astral.sh/uv/install.sh | sh`).

## Install as MCP server (recommended)

Register the stdio server (15 tools) with the user's data directory:

```bash
claude mcp add openinvest -e INVEST_HOME=~/openInvest -- uvx openinvest mcp
```

For other MCP clients, the server command is: `uvx openinvest mcp`
with env `INVEST_HOME=<data dir>` (defaults to `~/openInvest`).

## First-time setup

Run the interactive onboarding (creates portfolio/config under `INVEST_HOME`):

```bash
INVEST_HOME=~/openInvest uvx openinvest init
```

Agents can pipe answers instead: `uvx openinvest init --from-stdin` with a JSON object
`{"name", "risk_tolerance", "monthly_income_cny", "monthly_expenses_cny", "current_assets": {"cash_cny"}}`.

Verify health:

```bash
INVEST_HOME=~/openInvest uvx openinvest doctor
```

`status: "ready"` means done. Any failing check comes with a `hint` field explaining the fix.

## Optional

- `DEEPSEEK_API_KEY` in `$INVEST_HOME/.env` — only needed for the *Direct* committee path
  (`run_committee` tool). Read tools and portfolio tools work without any API key.
- Update later: `uvx --refresh openinvest doctor`

No daemon, no ports: the MCP client spawns the process per session over stdio.
