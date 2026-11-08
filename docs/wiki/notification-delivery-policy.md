# Notification delivery policy

Detection, analysis, durable recording and external delivery are separate. An
SMTP acceptance receipt alone does not establish acceptable message volume.

- A watchdog cycle retains all findings and queues one concise operational
  summary, at most once per rolling 24 hours. Raw diagnostics stay local.
- All SMTP entry points share a persistent, per-recipient rolling budget.
  The default is five admissions per 24 hours. Concurrent or uncertain attempts
  consume a slot. Accepted and uncertain identities remain protected after the
  budget window ends.
- `event.email_enabled` controls email without stopping monitoring.
  `event.email_mode` accepts `all` (compatibility), `daily_and_risk`, `daily`,
  or `off`. `event.email_daily_cap` is an operational user preference, not a
  market signal. These settings use the existing CLI/API config interface.
- `daily_and_risk` allows one dated daily summary and new observed price risk
  or high-severity risk news. Cosmetic rewrites of the same risk condition do
  not create new urgent messages. Ordinary news and maintenance items are
  summarized. A cheap fallback summary is queued during local hour 11 if no
  daily report has been admitted. No LLM call is needed for this fallback.
- A deferred message has not been delivered. Its original content and retry
  state remain in the outbox. Old or undated reports cannot consume today's
  daily slot. Replacement notices inherit the original expiry.
- Market notices omit account quantities and PnL. Old queued notices are
  sanitized before delivery. Replacement channels do not resend an accepted,
  uncertain or in-flight original channel.

For example: `openinvest config --set event.email_mode daily_and_risk` and
`openinvest config --set event.email_enabled false`. This never changes a
committee verdict, trading authority or a learning qualification.
