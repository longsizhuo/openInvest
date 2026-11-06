# openInvest installed

The skills are registered. Two steps are left:

1. Give the agent its tools by approving MCP registration (Hermes plugins cannot do this themselves). It is registered twice: `openinvest` (reads, news queue, committee) and `openinvest_ledger` (portfolio writes and destructive operations). Both start with `trust: untrusted`, so approval prompts remain. This is a fresh-install example; preserve existing configuration instead of overwriting it:

   ```bash
   R="$HOME/.hermes/plugins/openinvest/skills/invest/scripts/run.sh"
   hermes config set mcp_servers.openinvest.command bash
   hermes config set mcp_servers.openinvest.args "[\"$R\", \"mcp\"]"
   hermes config set mcp_servers.openinvest.env.INVEST_MCP_SCOPE agent
   hermes config set mcp_servers.openinvest.trust untrusted
   hermes config set mcp_servers.openinvest_ledger.command bash
   hermes config set mcp_servers.openinvest_ledger.args "[\"$R\", \"mcp\"]"
   hermes config set mcp_servers.openinvest_ledger.env.INVEST_MCP_SCOPE ledger
   hermes config set mcp_servers.openinvest_ledger.trust untrusted
   ```

   With a custom `HERMES_HOME`, use that directory instead of `$HOME/.hermes`. Onboarding can offer registration if it is missing. Starting MCP also starts the local background scheduler described below.

2. Start a new chat and say **"set up openInvest"** (帮我初始化 openInvest). Onboarding asks for your name, risk tolerance and current holdings. External parsing of holdings requires approval for the specific data and model provider; structured `holdings_v2` avoids that extra backend parsing call.

Daily committee reports and news-sentinel cron jobs are **optional**. Before creating them, approve each schedule/timezone, model/data destination, quota costs and private delivery recipient. If unattended operation needs `openinvest.trust: full`, separately approve removing prompts for that scope; keep the ledger scope untrusted. Installation is not approval for these changes. Declining leaves interactive use available. Remove an approved job with `hermes cron remove <id>`.

A one-paragraph ledger digest (holdings, today's verdicts, pending news) is automatically injected only in local CLI sessions. Gateway chats, including DMs and groups, receive no automatic holdings digest because their audience cannot be verified by the hook. To keep only the routing rule everywhere, run `hermes config set plugins.entries.openinvest.settings.inject_digest false`. Explicit sharing of financial data still requires approval for the audience.

While Hermes runs, the openInvest MCP server also keeps a background scheduler alive (news queue, price sentinel, NAV snapshots, daily backup). To keep it running when Hermes is closed, ask the agent to run `run.sh service install`.
