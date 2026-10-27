# openInvest installed

The skills are registered. Two steps are left:

1. Give the agent its tools — register the MCP server (Hermes plugins cannot do this themselves). It is registered twice: `openinvest` (reads, news queue, committee; full trust so the cron jobs can run) and `openinvest_ledger` (the tools that move money; untrusted, so Hermes asks you before every ledger write):

   ```bash
   R="$HOME/.hermes/plugins/openinvest/skills/invest/scripts/run.sh"
   hermes config set mcp_servers.openinvest.command bash
   hermes config set mcp_servers.openinvest.args "[\"$R\", \"mcp\"]"
   hermes config set mcp_servers.openinvest.env.INVEST_MCP_SCOPE agent
   hermes config set mcp_servers.openinvest_ledger.command bash
   hermes config set mcp_servers.openinvest_ledger.args "[\"$R\", \"mcp\"]"
   hermes config set mcp_servers.openinvest_ledger.env.INVEST_MCP_SCOPE ledger
   hermes config set mcp_servers.openinvest_ledger.trust untrusted
   ```

   With a custom `HERMES_HOME`, use that directory instead of `$HOME/.hermes`. (Onboarding in step 2 also does this for you if it is missing.)

2. Start a new chat and say **"set up openInvest"** (帮我初始化 openInvest). Onboarding asks for your name, risk tolerance and current holdings, then sets up a daily committee report and a news sentinel as Hermes cron jobs (they run on your model quota; remove them with `hermes cron remove <id>`).

While Hermes runs, the openInvest MCP server also keeps a background scheduler alive (news queue, price sentinel, NAV snapshots, daily backup). To keep it running when Hermes is closed, ask the agent to run `run.sh service install`.
