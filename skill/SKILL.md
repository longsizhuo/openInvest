---
name: invest
version: 0.3.0
description: Multi-asset investment system with proper Coordinator-Worker architecture. Read portfolio / live prices / strategy / committee history; run Quant + Macro + Risk Officer + CIO investment committee with optional async worker fan-out (mirrors Claude Code's Agent Teams protocol). Auto-bootstraps the invest project from GitHub on first use. Trigger when user asks about positions, P&L, gold/NDQ price, "should I buy/sell", or "run committee / analyze [asset]".
---

# Invest Skill (v0.3 — Agent Teams aligned)

User runs a multi-asset investment system at `$INVEST_HOME`
(default `~/projects-review/invest`, repo: `https://github.com/longsizhuo/invest`).
Tracks two assets:
- **NDQ.AX** — BetaShares Nasdaq 100 ETF (Australia / AUD / via CommSec)
- **GC=F / 浙商积存金** — Gold via Zheshang Bank (CNY / per-gram)

**Key insight**: this skill **shares the same code as the cron-driven DeepSeek
pipeline**. The only difference is the LLM doing the talking — when user invokes
the skill, **YOU (Claude) play coordinator + worker roles**. Same `agents/`,
same `core/committee.py`, same `memory/` layout. The skill auto-clones the
project on first use, so anyone deploying it gets the same behaviour.

## Bootstrap

`run.sh` checks `$INVEST_HOME`; if missing it:
1. `git clone --branch feat/openclaw-overhaul https://github.com/longsizhuo/invest.git $INVEST_HOME`
2. `cd $INVEST_HOME && uv sync --frozen --python 3.13`
3. Hints user to copy `user_profile.example.json` and run `migrate_profile.py`

Override via env: `INVEST_HOME`, `INVEST_REPO`, `INVEST_BRANCH`.

## Subcommands (read-only, fast)

All via `~/.claude/skills/invest/run.sh <cmd> [args]`. Output is JSON.

- `run.sh status` — portfolio + live prices + unrealized P&L. **Default entry for "how am I doing"**.
- `run.sh strategy` — target_assets list + Dreaming long-term insights.
- `run.sh history [-n N]` — last N trades + last N committee verdicts.
- `run.sh live_prices` — VIX / TNX / USDCNY / AUDCNY / NDQ / GC=F.
- `run.sh what_if [--gold-price X | --gold-pct ±N | ...]` — arithmetic P&L scenario, no LLM.

## Investment Committee (Coordinator-Worker mode)

When the user asks **"should I buy/sell X"** or **"run committee on X"**, you
play coordinator and orchestrate workers to play 4 specialist roles. This
mirrors the Claude Code v2.1.88 Coordinator Mode protocol — the only difference
is workers are spawned via your `Agent` tool instead of `forkedAgent`.

### Step 1: Get the brief

```bash
~/.claude/skills/invest/run.sh prepare_committee <SYMBOL>
```

Returns JSON with:
- `asset` — the target asset config from `strategy.md`
- `portfolio_summary` — user's current positions + dry powder + risk profile
- `macro_data` — live VIX / TNX / USDCNY snapshot
- `market_data` — multi-timeframe technical analysis
- `prior_insights` — Dreaming long-term patterns (may be empty)
- `prompts.{macro_strategist, quant_round1, risk_round1, quant_round2_after_risk, risk_round2_after_quant, cio}` —
  the EXACT prompts the cron pipeline uses, pulled from `agents/*.py`
- `save_command` — how to persist results

### Step 2: Round 1 — Spawn 3 workers in parallel

**This is the Coordinator-Worker fan-out** — equivalent to Claude Code's
`Agent` tool with `subagent_type: worker`. Send all three Agent calls in a
single message to launch concurrently.

```
Agent({
  description: "Macro analysis",
  subagent_type: "general-purpose",
  prompt: "<paste prompts.macro_strategist verbatim>\n\n# 当前宏观数据:\n<paste macro_data>"
})
Agent({
  description: "Quant analysis (Round 1)",
  subagent_type: "general-purpose",
  prompt: "<paste prompts.quant_round1 verbatim>\n\n# 市场数据:\n<paste market_data>"
})
Agent({
  description: "Risk Officer (Round 1)",
  subagent_type: "general-purpose",
  prompt: "<paste prompts.risk_round1 verbatim>\n\n# 用户持仓:\n<paste portfolio_summary>\n\n# 长期模式:\n<paste prior_insights>"
})
```

Each worker has its own context window — pure information separation, no
cross-contamination. They return as `<task-notification>` messages.

### Step 3: Round 2 — Cross-challenge (2 workers in parallel)

Once Macro / Quant / Risk Round 1 results are all back, spawn 2 more workers
to do cross-challenge. Each gets the OTHER analyst's R1 output as context to
adjust their own view.

```
Agent({
  description: "Quant Round 2 (sees Risk's report)",
  subagent_type: "general-purpose",
  prompt: "<paste prompts.quant_round2_after_risk>\n\n# Round 1 你自己的输出:\n<quant R1 result>\n\n# Risk Officer 的报告:\n<risk R1 result>"
})
Agent({
  description: "Risk Round 2 (sees Quant's signals)",
  subagent_type: "general-purpose",
  prompt: "<paste prompts.risk_round2_after_quant>\n\n# Round 1 你自己的输出:\n<risk R1 result>\n\n# Quant 的技术信号:\n<quant R1 result>"
})
```

### Step 4: Round 3 — You synthesize as CIO

**Don't spawn another worker** — the CIO role is yours. Read all 5 outputs
(Macro + Quant R1/R2 + Risk R1/R2) plus portfolio_summary, then write a
complete CIO memo following `prompts.cio` format.

This step is **never delegated**. Per Claude Code Coordinator Mode:
> "You are a coordinator. Synthesize results and communicate with the user.
> Never write 'based on your findings' — that delegates understanding."

### Step 5: Persist transcript

```bash
cat <<EOF | ~/.claude/skills/invest/run.sh save_committee <SYMBOL>
=== MACRO ===
<macro worker result>

=== QUANT_R1 ===
<quant R1 worker result>

=== RISK_R1 ===
<risk R1 worker result>

=== QUANT_R2 ===
<quant R2 worker result>

=== RISK_R2 ===
<risk R2 worker result>

=== CIO ===
<your CIO memo>
EOF
```

Saved to `memory/.committee/<date>/<asset>.md`, identical schema to cron
pipeline output (with `Provider: claude (skill mode)` marker so Dreaming
can mine both providers' transcripts).

## Why parallel workers vs single-conversation roles

**v0.2 (single-conversation)**: You played 6 roles sequentially in your reply.
Same context window, no real isolation. Cheap, but 角色串味 — Quant might
leak macro knowledge from earlier in your reply.

**v0.3 (worker fan-out)**: True async parallel, each worker pure context.
Mirrors Claude Code Coordinator Mode protocol. Costs more workers but quality
significantly higher.

**Fallback to v0.2 mode**: If `Agent` tool isn't available in your current
context (rare), fall back to single-conversation 6-role output — the
`save_committee` parser accepts both formats.

## Acting on a verdict

If the user agrees, **don't modify memory directly**. Tell them the NapCat
command (whitelisted to QQ 1169771750):

- `/gold_buy 5g @1040` — record purchase
- `/gold_sell 5g @1050` — record sale (auto-computes 0.38% sell fee)
- `/gold_offset 1040` — calibrate Zheshang spread
- `/deposit 5000` / `/withdraw 1000` — adjust CNY cash
- `/risk balanced` — adjust risk profile
- `/run` — trigger heavyweight DeepSeek daily_report (~6 min)

## Constraints

- **Don't trigger `daily_report` cron job** unless user explicitly says
  "run full report / 跑深度分析". That uses DeepSeek and costs tokens.
- **Don't fabricate live prices.** Always `run.sh status` or `live_prices`.
- **Don't write to `memory/` directly.** State changes go through NapCat
  `/cmd` for audit trail.
- **JSON output is fresh.** The markdown files in `memory/` are slightly
  stale — always cite from JSON.
- **Don't run multiple committees on the same asset same day** — if
  `memory/.committee/<today>/<asset>.md` exists, just read it instead.
