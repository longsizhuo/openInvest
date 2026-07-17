# Committee Protocol — Hermes / other agents that support subtask delegation

The user says, **live in chat**, **"should I buy/sell X / 该不该买/卖X"** /
**"analyze X / 分析一下X"** / **"run committee on X / 跑委员会X"**, and you (the
calling agent) have no `LLM_API_KEY` configured — follow the 6 stages strictly,
with zero API cost throughout (your own subscribed model plays the 4 roles).

## ⚠️ First confirm you can take this path

**Interactive scenarios only (user present and able to watch what you are doing in
real time).** If you were triggered unattended by cron / a scheduled job — **do not
use this protocol**; use the Direct path's `daily_report` / `run_committee` instead
(configure `LLM_API_KEY`; a free-tier provider works, it does not have to be paid).
On 2026-07-14 an unattended run of this protocol was actually observed: the agent
did not faithfully call `delegate_task`, picked a different route on its own, then
hit the "unattended cron cannot approve dangerous commands" safety block and stalled —
the protocol relies on you deciding on the fly which tool to call and how to assemble
the prompts, and no one can correct you when you go off track; in unattended scenarios
this is a failure mode that really happens, not a hypothetical.

This document is the **Hermes variant of the Coordinator path** — you use the
`delegate_task` tool to spawn isolated subtasks that play the roles; the logic is
exactly the same as [committee-protocol.md](committee-protocol.md) (Claude Code uses
`Agent({...})`), only the spawn syntax differs.

**You need**: the `delegate_task` tool (or an equivalent isolated-subtask delegation
capability) + a terminal/shell execution tool (to run `run.sh` commands). **Without
these** (e.g. you can only do single-turn conversation and cannot spawn isolated
subtasks) → use the **Direct path** instead:

```bash
run.sh run_committee <SYMBOL>
```

One command gets you the verdict + CIO memo + transcript, but requires `DEEPSEEK_API_KEY`.

---

> **Background**: In the Coordinator path Macro is not shared across assets — it is
> spawned together with the others each time Round 1 starts → R1 has 3 workers total.
> In the Direct/Cron path Macro is shared across assets, so R1 has only 2 workers
> (Quant + Risk) — do not mix up citations of the two LLM call-count comparisons.

## Stage 0: Same-day check (avoid duplicate runs)

Run with your terminal tool:

```bash
ls "$INVEST_HOME/memory/.committee/$(date +%F)/<SYMBOL>.md" 2>/dev/null
```

File exists → **read it directly, do not re-run**, and tell the user: "<SYMBOL> has
already been run today; the verdict was X (confidence Y). Want to re-run?" — a full
committee takes several minutes + a number of LLM calls; don't burn it twice for the
same answer.

## Stage 1: Get the brief

```bash
run.sh prepare_committee <SYMBOL>
```

Returns JSON containing all the fields you need (full table in committee-protocol.md
Stage 1 — this step is identical across the two paths). **⚠ `regime_brief` MUST be
injected verbatim into the Quant Round 1 + Round 2 prompts**; omit it and Quant will
wrongly scream bearish at a range_bound bottom (the old bug path).

## Stage 2: Round 1 — one `delegate_task` call batch-spawns all 3 roles

**Spawn them all at once with the `tasks` array — do not split into 3 separate
calls** — `delegate_task`'s batch mode already waits synchronously until all tasks
complete before returning; separate calls actually lose the parallelism:

```
delegate_task(tasks=[
  {
    "goal": "<paste prompts.macro_strategist verbatim>\n\n# Current macro data:\n<paste macro_data verbatim>\n\nYour entire final reply must be, and be nothing but, the structured format the committee requires (SIGNAL/STRENGTH/etc. fields — see the format notes in the prompt above). Do not output a 'what I did / which tools I used' style summary — that is not what this task wants; hand over the structured result as-is."
  },
  {
    "goal": "<paste prompts.quant_round1 verbatim>\n\n# Market Regime (deterministically computed, must be followed):\n<paste regime_brief verbatim>\n\n# Market data:\n<paste market_data verbatim>\n\n(Same as above: the entire reply must be the structured format only; do not summarize what you did.)"
  },
  {
    "goal": "<paste prompts.risk_round1 verbatim>\n\n# User portfolio:\n<paste portfolio_summary verbatim>\n\n# Long-term patterns:\n<paste prior_insights verbatim>\n\n(Same as above: the entire reply must be the structured format only; do not summarize what you did.)"
  }
])
```

**Why the "do not summarize what you did" line is added**: `delegate_task`'s default
subtask system prompt asks the sub-agent to end with a "what I did / what I found /
which files I changed" style summary (designed for coding scenarios). What we want is
for the sub-agent's **entire reply** to be the committee format itself, not the format
wrapped in a layer of "summary" — so the end of every `goal` must explicitly override
that default instruction.

The `context` field (if your `delegate_task` version passes it separately from `goal`)
can hold the data blocks instead, with the same effect — keep the information isolated:
Macro must not see the portfolio, Risk must not see market technical indicators, the
same isolation requirements as the Claude Code version.

Batch mode waits synchronously by default (`background` omitted or `false`); on return,
each element of the `results` array corresponds to one task's final reply. **Wait for
this `delegate_task` call to fully return before entering Stage 3** — the 3 roles run
within the same call, so no extra waiting logic is needed.

## Stage 3: Round 2 — Cross-challenge (one `delegate_task` call, 2 roles)

Quant and Risk can now see each other's R1 output and adjust their own views. Macro
does not need Round 2 — the cross-challenge is only between Quant and Risk; Macro is
market-level context and skips Round 2 on both paths:

```
delegate_task(tasks=[
  {
    "goal": "<paste prompts.quant_round2_after_risk verbatim>\n\n# Market Regime (the facts you were given in Round 1, still valid in Round 2):\n<paste regime_brief verbatim>\n\n# Your own Round 1 output:\n<quant R1 result>\n\n# Risk Officer's report:\n<risk R1 result>\n\n(The entire reply must be the structured format only; do not summarize what you did.)"
  },
  {
    "goal": "<paste prompts.risk_round2_after_quant verbatim>\n\n# Your own Round 1 output:\n<risk R1 result>\n\n# Quant's technical signals:\n<quant R1 result>\n\n(The entire reply must be the structured format only; do not summarize what you did.)"
  }
])
```

## Stage 4 (optional): Run Round 3+ if not converged

Same as committee-protocol.md — run Round 3 (another two-role `delegate_task`) only
if Quant's/Risk's SIGNALs flipped between R1→R2 **and** they still seriously diverge
after the flip. Convergence rule: both sides' SIGNALs are the same as the previous
round, and `|STRENGTH delta| ≤ 1.0` → converged, proceed to Stage 5.

## Stage 5: CIO synthesis — **write it yourself**, do not delegate

The CIO role is **you** (the agent that issued the `delegate_task` calls); do not
spawn another subtask to do it for you — you have already seen all the worker outputs,
synthesize directly.

After reading Macro + Quant R1/R2 + Risk R1/R2 + `portfolio_summary`, write the full
CIO memo in the `prompts.cio` format, including the required fields (`VERDICT`/`CONFIDENCE`/
`DOMINANT_VIEW`/`SUGGESTED_ALLOC_CNY`/`EXECUTION_PLAN`/`RISK_PLAN`/
`PERSONAL_NOTE`) + the sanity self-check (full rules in committee-protocol.md Stage 5 —
this step is identical across the two paths: confidence ≥ 0.95 lowered to 0.85,
alloc > 100k clamped, crash regime forces HOLD/TRIM, and if workers seriously diverge
give an honestly low confidence).

## Stage 6: Persist the transcript

```bash
cat <<EOF | run.sh save_committee <SYMBOL> --provider hermes
=== MACRO ===
<macro role output>

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

**`--provider hermes` MUST be included** — the persisted transcript will then be
accurately tagged as run by Hermes (not hardcoded to claude); Dreaming's per-provider
bucketed pattern mining and after-the-fact troubleshooting both depend on this field.

Lands in `memory/.committee/<date>/<asset>.md` with exactly the same schema as the
other two paths; downstream tools such as `explain_decision` / `decisions` can read
it with zero changes.

## After the verdict

Same as committee-protocol.md: do not write to `memory/` yourself; after the user
confirms the trade executed, record it with the `buy` / `sell` / `deposit` /
`withdraw` MCP tools; after the user responds to the recommendation, write back to
the decision ledger with `record_execution <decision_id>` (if they decline, first
ask why).
