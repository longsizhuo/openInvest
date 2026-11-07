# Hermes Coordinator adapter

Follow [committee-protocol.md](committee-protocol.md) for the entire data and decision contract. It is authoritative for orchestration; the prepared backend prompts are authoritative for decision rules. This adapter only describes task submission.

Use `delegate_task(tasks=[...])` for the three independent first-round roles, wait for every result, then use a second batch for Quant R2 and Risk R2. Each task receives its verbatim prepared prompt and all original inputs assigned to that role by the shared protocol. Use isolated contexts; do not replace failed roles with your own invented reports. Preserve actual model/composition and submit the exact `context_id`.

```text
delegate_task(tasks=[
  {"goal":"<prompts.macro_strategist + original macro_data/event_brief>"},
  {"goal":"<prompts.quant_round1 + original market/regime/sentiment/valuation>"},
  {"goal":"<prompts.risk_round1 + original portfolio/market/regime/sentiment/qualified insights>"}
])
```

The rebuttal batch includes both R1 reports and the same original role inputs. CIO receives `prompts.cio`, `cio_source_context`, original portfolio/source data and complete reports. The return fields and statuses come from the prepared backend contract; this file supplies no additional market gates, confidence adjustments or cash thresholds.

For `verification_required`, use another isolated `delegate_task` with the server-returned review prompts. Submit the returned JSON in `reasoning_review_json` alongside the unchanged six-section transcript/context. Do not treat the draft as an ordinary HOLD or regenerate the committee to force a different investment verdict.

After an interrupted task, first call `committee_review(context_id=...)` (hosted: `analysis_id=...`). It returns the exact saved transcript, original provider/model and issued review packet. Continue only if `resume_allowed=true`. Missing originals or expired/changed inputs remain audit-only; never reconstruct the transcript from trimmed sections or memory.

For scheduled queue processing, inspect each durable request, prepare fresh covered inputs and check its completion receipt after submission. A queue read is not completion. An incomplete role, stale context, unregistered risk condition or unconfirmed notification must remain visible. When the host cannot meet this contract, use the configured Direct API path or report unavailable capability rather than returning an ordinary HOLD.
