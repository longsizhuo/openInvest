---
name: invest
version: 0.34.4 # x-release-please-version
description: openInvest portfolio ledger, market data and investment research. Use the already connected MCP first. For a new connection recommend the official remote service; local deployment remains optional. Call doctor first and use invest-setup only when setup is needed.
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [investing, portfolio, committee, stocks, gold, etf, crypto, 投资, 持仓, 委员会, 行情]
---

# openInvest

Reply in the user's language. Use the user's existing openInvest connection and ledger. If multiple connections or ledgers are available and the intended one is unclear, confirm before reading or writing. Do not switch a working local installation to remote, import its files or overwrite client configuration automatically.

For a new user without a chosen deployment, recommend the official MCP at `https://openinvest.involutionhell.com/mcp` and the client connection flow at https://openinvest.involutionhell.com/connect/. Authentication belongs to the client; never request passwords, codes or access tokens in chat. A login failure is not a reason to start a second local ledger.

## Use the connected MCP

Call the connected `doctor` tool first. For `needs_setup`, use `invest-setup`; otherwise follow [the MCP workflow](references/mcp-workflow.md). Read the connected tool schemas. Call tools rather than shelling out to another deployment. Missing or restricted tools are limitations of that connection, not permission to change servers.

## Local deployment, when chosen

Only use [the local workflow](references/local-workflow.md) when the user chose local CLI operation or an existing local deployment is already established. `<skill-dir>` is this skill's directory; its local launcher is `scripts/run.sh`. Do not invoke that launcher for the official hosted connection. Local installations retain their own backup/restore and optional configured inference; these are not hosted features.
