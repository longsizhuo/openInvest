# Committee Protocol (read this when the user asks "should I buy/sell X / 该不该买/卖X")

The user said **"should I buy/sell X / 该不该买/卖X"** / **"analyze X / 分析一下X"** /
**"run committee on X / 跑委员会X"** — follow the 6 stages strictly.

## ⚠️ First confirm you can take this path

This document is the **Coordinator path** — you (Claude Code) use the `Agent({...})` tool
to spawn 4 subagents and play the roles yourself.

**Interactive scenarios only (user present).** If you were triggered unattended by cron /
a scheduled job (no user watching you in real time), **do not use this protocol** — it
relies on you deciding on the fly which tool to call and how to assemble the prompts,
and with no one to correct you it can stall or go off track. For unattended runs, always
use the Direct path instead (see `run_committee` / `daily_report` below), regardless of
whether you have subagent capability.

**If you are not Claude Code**, first check whether you have isolated-subtask delegation
capability (Hermes's `delegate_task`, etc.):

- **Yes** (Hermes / other agents that support subtask delegation) → read
  [committee-protocol-hermes.md](committee-protocol-hermes.md) instead — same
  6-stage protocol, zero API cost, only the spawn syntax differs
- **No** (Codex / Cursor / Cline / local DeepSeek / plain scripts and other agents
  limited to single-turn conversation) → use the **Direct path** instead:

```bash
~/.claude/skills/invest/scripts/run.sh run_committee <SYMBOL>
```

One command gets you the verdict + CIO memo + transcript, but requires `DEEPSEEK_API_KEY`.
See the "choosing a path" section in SKILL.md and `references/two-paths.md` for details.

---

> **Coordinator path background**: Macro is not shared — it is spawned together with the
> others each time Round 1 starts → R1 has 3 workers total. In the Direct/Cron path Macro
> is shared across assets, so R1 has only 2 workers (Quant + Risk); see
> [docs/wiki/02-agents.md](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/02-agents.md#两条路径-llm-调用数对照).
> Do not mix up citations between the two.

## Stage 0: Same-day check (avoid duplicate runs)

```bash
ls "$INVEST_HOME/memory/.committee/$(date +%F)/<SYMBOL>.md" 2>/dev/null
```

If the file exists, **read it directly — do not re-run**. Tell the user:
> "<SYMBOL> has already been run today; the verdict was X (confidence Y). Want to re-run?"

A full committee run consumes ~15-60s of the user's Claude budget — don't burn it
twice for the same answer.

## Stage 1: Get the brief

```bash
~/.claude/skills/invest/scripts/run.sh prepare_committee <SYMBOL>
```

Returns JSON containing all the fields you need:

| Field | Used in |
|------|------|
| `asset` | Referenced by all 4 workers |
| `portfolio_summary` | Risk Officer prompt |
| `macro_data` | Macro Strategist prompt |
| `market_data` | Quant prompt |
| `regime_brief` | **Critical** — must go into both the Quant Round 1 + Round 2 prompts (see warning) |
| `prior_insights` | Risk Officer prompt (empty if Dreaming has never run) |
| `prompts.{...}` | Prompt templates from `capabilities/committee/<role>/<role>.py` (use verbatim) |
| `instructions` | Single-asset orchestration tip (**read it**!) |

**⚠ regime_brief warning**: this is the market regime computed by Python (uptrend / downtrend /
range_bound / crash / recovery) + that regime's neutral probability stats (median 30d forward
return / probability of falling below the current price / sample count — the directional call
is handed back to Quant to decide from the data; there is no hard directional lock).
**Forget to inject it into Quant and you fall back onto the old bug path** — Quant wrongly
screaming bearish at a range_bound bottom. It **must be injected verbatim** into both the
Round 1 + Round 2 Quant prompts.

## Stage 2: Round 1 — 3 workers in parallel

**All 3 `Agent({...})` calls MUST be sent in a single message** — that is how they truly
run in parallel. Each worker gets its own context window; information is physically isolated:

```javascript
Agent({
  description: "Macro analysis",
  subagent_type: "general-purpose",
  prompt: "<paste prompts.macro_strategist verbatim>\n\n# Current macro data:\n<paste macro_data verbatim>"
})

Agent({
  description: "Quant analysis (Round 1)",
  subagent_type: "general-purpose",
  prompt: "<paste prompts.quant_round1 verbatim>\n\n# Market Regime (deterministically computed, must be followed):\n<paste regime_brief verbatim>\n\n# Market data:\n<paste market_data verbatim>"
})

Agent({
  description: "Risk Officer (Round 1)",
  subagent_type: "general-purpose",
  prompt: "<paste prompts.risk_round1 verbatim>\n\n# User portfolio:\n<paste portfolio_summary verbatim>\n\n# Long-term patterns:\n<paste prior_insights verbatim>"
})
```

Each worker returns via `<task-notification>`. **Wait for all 3 to come back before
entering Stage 3.**

## Stage 3: Round 2 — Cross-challenge (2 workers in parallel)

Quant and Risk can now see each other's R1 output and adjust their own views. Macro does
not need Round 2 — the cross-challenge is only between Quant and Risk; Macro is
market-level context and skips Round 2 on both paths. Send both Agent calls in a single message:

```javascript
Agent({
  description: "Quant Round 2 (sees Risk's report)",
  subagent_type: "general-purpose",
  prompt: "<paste prompts.quant_round2_after_risk verbatim>\n\n# Market Regime (the facts you were given in Round 1, still valid in Round 2):\n<paste regime_brief verbatim>\n\n# Your own Round 1 output:\n<quant R1 result>\n\n# Risk Officer's report:\n<risk R1 result>"
})

Agent({
  description: "Risk Round 2 (sees Quant's signals)",
  subagent_type: "general-purpose",
  prompt: "<paste prompts.risk_round2_after_quant verbatim>\n\n# Your own Round 1 output:\n<risk R1 result>\n\n# Quant's technical signals:\n<quant R1 result>"
})
```

## Stage 4 (optional): Run Round 3+ if not converged

The Web/Cron path has built-in convergence detection and runs up to `max_debate_rounds=4`.
In practice, skill mode rarely needs more than 2 rounds — run Round 3 only when **both**
of the following hold:

- Quant's and Risk's SIGNALs flipped between R1→R2 (persuaded by the other side), AND
- the new post-flip SIGNAL+STRENGTH still seriously diverge from each other

Otherwise skip to Stage 5.

**Convergence rules** (when to stop the debate):
- Quant SIGNAL is the same as the previous round, and |STRENGTH delta| ≤ 1.0
- Risk SIGNAL is the same as the previous round, and |STRENGTH delta| ≤ 1.0
- Both satisfied → converged, proceed to CIO

## Stage 5: CIO synthesis — **you write it**, do not delegate

The CIO role is **you** (the orchestrator). Per the Claude Code Coordinator Mode principle:

> "You are a coordinator. Synthesize results and communicate with the user.
> Never write 'based on your findings' — that delegates understanding."

After reading all worker outputs (Macro + Quant R1/R2 + Risk R1/R2) + `portfolio_summary`,
write the full CIO memo in the `prompts.cio` format.

### Required fields in the CIO output

- `VERDICT`: one of `BUY` / `ACCUMULATE` / `HOLD` / `TRIM` / `SELL`
- `CONFIDENCE`: 0.0–1.0
- `DOMINANT_VIEW`: which side persuaded you (`macro` / `quant` / `risk`)
- `SUGGESTED_ALLOC_CNY`: integer (positive = buy more, negative = reduce position)
- `EXECUTION_PLAN`: how to actually execute (lump-sum / DCA / grid)
- `RISK_PLAN`: stop-loss trigger conditions + worst-case PnL estimate
- `PERSONAL_NOTE`: bullet-point message to the user

### CIO sanity self-check (run through it before outputting)

| Rule | Why |
|------|--------|
| `confidence ≥ 0.95` → lower to 0.85 | Guards against overconfidence. LLMs love to over-commit on ambiguous signals |
| `alloc_cny > 100_000` → clamp to 100_000 | Per-trade cap. Forces the user to be deliberate about larger moves |
| REGIME = `crash` → force `HOLD` or `TRIM` | REGIME takes priority over signals. crash = uncertainty too high to add risk |
| Workers seriously divided → `confidence: 0.4-0.5` | Don't fake consensus. Honest low confidence > pretended high confidence |

## Stage 6: Persist the transcript

```bash
cat <<EOF | ~/.claude/skills/invest/scripts/run.sh save_committee <SYMBOL>
=== MACRO ===
<macro worker output>

=== QUANT_R1 ===
<quant R1 output>

=== RISK_R1 ===
<risk R1 output>

=== QUANT_R2 ===
<quant R2 output>

=== RISK_R2 ===
<risk R2 output>

=== CIO ===
<the CIO memo you wrote>
EOF
```

Lands in `memory/.committee/<date>/<asset>.md`, with exactly the same schema as the
DeepSeek cron path, just tagged `Provider: claude (skill mode)` so Dreaming can later
distinguish the transcripts of the two paths.

## After the verdict

If the user agrees:
1. **Do not write to `memory/` yourself** (see SKILL.md Constraints).
2. After the user confirms the trade executed, record it with the CLI `buy` / `sell`
   (for cash use `deposit` / `withdraw`, or the same-named MCP tools) — going through
   the ledger leaves an audit trail.
3. After the user responds to the recommendation (bought / didn't buy / declined),
   write back to the decision ledger with `record_execution <decision_id>` (if they
   decline, first ask why).
