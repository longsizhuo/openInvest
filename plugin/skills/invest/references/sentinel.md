# Intel Sentinel — scheduled proactive news feeding（情报哨兵）

The backend already has a **baseline sentinel**: `event_check` on a 30-minute
schedule (wired during invest-setup step 6) pulls multi-source news, LLM-normalizes
and severity-grades it, and auto-triggers the committee on high-severity holdings
hits. **This doc is the optional layer on top**: you (the host agent) schedule
*yourself* to search with your own tools and feed what the crawler can't see —
Chinese-language sources, regional markets, breaking geopolitics.

**Skip this doc entirely unless ALL of these hold**:

- your platform has a scheduling facility (Claude Code `CronCreate` / Hermes
  scheduled reminders / OpenClaw cron jobs)
- you have web-search capability
- the backend has an LLM key (`ingest_event` normalization is LLM-mandatory)

Any of them missing → the backend sentinel from invest-setup **is** your sentinel.
Done, close this file.

## The recipe (the cron prompt is the whole implementation)

Register a scheduled job (15–30 min cadence) whose prompt is:

> 1. Run the invest skill's `status` to get current holdings + tracked symbols.
> 2. Search finance news from the last 30 minutes relevant to those assets —
>    prioritize Chinese/regional sources and geopolitical events (the backend
>    crawler's blind spots).
> 3. For each relevant hit, call `ingest_event` (MCP tool, or CLI
>    `run.sh ingest_event --title "…" --url "…" [--snippet "…" --source "…"]`).
> 4. Reply with one line: `📡 哨兵入库 N 条：<short phrases>`. Nothing relevant →
>    reply "no hits" and stop.

Platform registration:

- **Hermes**: "set a recurring reminder every 15 minutes with this prompt: …"
- **OpenClaw**: add a cron job whose message is the prompt above
- **Claude Code**: `CronCreate` with the prompt above

## Why this is safe to run unattended

- **Idempotent**: the backend dedups by url + claim hash — refeeding the same item
  never double-books (ADR-016).
- **You don't grade**: severity / affected symbols / stance are assigned by the
  backend normalizer, not by you. Feed raw title/url/snippet; don't editorialize.
- **You don't trade**: ingestion only fills the event ledger; the committee decides
  downstream. Never buy/sell/run-committee inside a sentinel job.
- **Low improvisation surface** (one search + one tool call per hit) — which is why
  an unattended *agent* cron is acceptable here, while unattended committee runs
  must go Direct (SKILL.md "Choosing a path", 2026-07-14 incident).
