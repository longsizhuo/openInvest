# Set up the hosted ledger

1. Use the already selected openInvest MCP connection and call `doctor`.
2. If authentication is required, direct the user to the client's OAuth connection flow and the official connection page: https://openinvest.involutionhell.com/connect/. Do not request passwords, OAuth codes or personal tokens in chat.
3. If a ledger already exists, use it. Do not reset, overwrite or initialize it again.
4. For `needs_setup`, ask which base currency the user wants. Do not infer it from locale or an unrelated account. Risk preference and tracked assets may remain unset; do not require a legal name, portfolio amount or holdings to connect.
5. Call `setup_ledger` with the confirmed currency. Include risk or symbols only if the user provided them, using the values in the current tool schema.
6. Verify setup with `doctor` and `ledger_settings`. Import existing holdings or reported transactions only after the user supplies their actual details, following the `invest` skill.

This setup operates through MCP tools. It does not install software, run local scripts, read local portfolio files, transfer a local ledger, or set up broker access. Moving existing records requires a separate explicit user request.
