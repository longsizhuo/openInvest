# openInvest

Keep an existing openInvest connection. For a new connection, the recommended option is the official remote MCP:

```yaml
mcp_servers:
  openinvest:
    url: https://openinvest.involutionhell.com/mcp
    auth: oauth
    trust: untrusted
```

Check `hermes mcp list` first. Do not overwrite a configured local/private server or create a second ledger automatically. Follow https://openinvest.involutionhell.com/docs/hosted-mcp for OAuth, then ask your Agent to set up openInvest with your chosen base currency. The same skills operate on the selected connection. Hosted setup does not install a local backend or create scheduled jobs.

For an explicitly chosen local deployment, use the local onboarding reference in the setup skill. Local users retain their existing launcher, data and backup/restore. To enable the local ledger digest, choose `plugins.entries.openinvest.settings.connection_mode: local`; `inject_digest: false` disables it. A remote or unselected connection never invokes the local digest command. Digests may be visible to other people in a shared Agent conversation.
