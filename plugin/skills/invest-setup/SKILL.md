---
name: invest-setup
version: 0.6.0 # x-release-please-version
description: Initialize or connect openInvest only when requested or doctor reports needs_setup. Prefer the official remote MCP for new users; preserve existing local connections. Confirm base currency; optional risk preference and tracked assets must not be inferred.
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [investing, setup, onboarding, 初始化, 投资]
---

# Set up openInvest

Reply in the user's language. Use the user's existing openInvest connection and ledger. If multiple connections or ledgers are available and the intended one is unclear, confirm before reading or writing. Do not switch a working local installation to remote, import its files or overwrite client configuration automatically.

For a new user without a chosen deployment, recommend the official MCP at `https://openinvest.involutionhell.com/mcp` and the client connection flow at https://openinvest.involutionhell.com/connect/. Authentication belongs to the client; never request passwords, codes or access tokens in chat. A login failure is not a reason to start a second local ledger.

Call the connected `doctor` first. If it is ready, keep the existing ledger; do not reset or initialize it again. For the official remote connection follow [MCP onboarding](references/mcp-onboarding.md): confirm base currency, allow risk preference and symbols to remain unset, and use `setup_ledger` with only confirmed values.

A bundled local MCP being auto-registered by a client is not evidence that the user chose local deployment. If its ledger still needs setup and no local choice was made, recommend the remote connection first. Do not initialize that local ledger or replace its configured server automatically.

For an explicitly chosen local deployment, follow [local onboarding](references/local-onboarding.md). Do not run local installation, cron creation, service setup or legacy REST configuration for a hosted connection. If no MCP tools are connected yet, finish the client connection before initializing a ledger.
