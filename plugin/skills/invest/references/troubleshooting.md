# Troubleshooting (read when doctor is all green but things still fail)

## `status` succeeds but live prices are 0 or missing

**Cause**: yfinance is rate-limited, or the asset's market is closed and the DB cache fallback
hasn't been built.

Look at the returned quotes — each carries an `is_stale` flag. `true` means the price came from
the local DB cache (`db/market_data.db`), not live data.

**Fix**: tell the user "the live data source is unreachable; showing cached data (X days old).
Try again in a few minutes, or check whether `db/market_data.db` is being updated regularly."

## `prepare_committee X` returns `{"error": "asset X not in strategy.target_assets"}`

`prepare_committee` only works on assets in `strategy.target_assets`. If the user wants to
analyze an untracked symbol:

1. First add it to `target_assets` via CLI `run.sh buy` (when there's a real position) or
   `POST /api/strategy/asset` — see `references/adding-assets.md`
2. Or add it as a tracking-only holding via `POST /api/holdings` (`is_tracking_only: true`) —
   same effect, without touching strategy

The committee can analyze an asset whether or not it's held, but it needs the `target_assets`
config (cap / fee / channel info).

## Worker (`Agent` call) errors with "no such tool"

You are not inside Claude Code (or the current context has no `Agent` tool). Skill mode requires
an orchestrator that can spawn workers.

**Degraded fallback**: single-conversation 6-role output. Read `prompts.{macro_strategist, quant_round1,
risk_round1, quant_round2_after_risk, risk_round2_after_quant, cio}` from the brief, then write
all 6 sections inline (you play every role yourself). Use the same `=== MACRO ===` /
`=== QUANT_R1 ===` / ... separators. `save_committee` accepts both formats.

The fallback loses true context isolation (information bleeds across roles inside your single
context) but at least produces a verdict.

## `save_committee` rejects the input

Most common causes:
- One of the 6 section headers is missing (`=== MACRO ===` etc.)
- A header is misspelled
- The CIO section is empty (you forgot to write it)

The parser is strict because the saved file is consumed by Dreaming and by the `decisions` /
`explain_decision` decision replay. Check that all 6 sections are present, then resend.

## Same-day check says a verdict exists but you never ran one

Check who wrote it:

```bash
head -5 "$INVEST_HOME/memory/.committee/$(date +%F)/<SYMBOL>.md"
```

The frontmatter contains `Provider: claude (skill mode)` or `Provider: deepseek`. If it's
`deepseek`, the cron `daily_report` has already run and written a verdict — you should read that
one and present it to the user. Only rerun if the user explicitly wants the Claude perspective.

## Remote mode (INVEST_API_BASE) forwarding errors / can't reach the hub

`.env` has `INVEST_API_BASE` set but subcommand forwarding fails. Check the hub machine:

1. `ps aux | grep uvicorn` — is web_api running on :8765 on the hub?
2. `curl $INVEST_API_BASE/api/health` — does it return 200? (If the hub has auth enabled,
   include `Authorization: Bearer $INVEST_API_TOKEN`)

## `.env` has a DeepSeek key but `daily_report` still returns 401

The key is most likely mistyped or revoked. Test it directly:

```bash
curl -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  https://api.deepseek.com/v1/models
```

200 = key valid, 401 = key invalid. Have the user reissue one at
https://platform.deepseek.com/api_keys.

## Deeper failures

[docs/wiki/09-troubleshooting.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/09-troubleshooting.md)
(in the project repository) has a complete catalog of 10 symptom classes → fixes.
