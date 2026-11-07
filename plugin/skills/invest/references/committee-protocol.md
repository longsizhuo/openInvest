# Committee Coordinator protocol

Use this path only when the host can run the required roles in isolated contexts and submit their actual outputs. Otherwise use `run_committee` with a configured backend API. Follow `doctor` and the runtime reasoning mode. A scheduled host may drain its durable queue through this same protocol when it supports the complete contract.

The backend's freshly prepared `prompts` and `instructions` are the single source of decision rules. This document describes orchestration and data handling; it adds no market thresholds or forced verdicts. Host calls consume the host provider's allowance or API budget. For an explicitly requested real API experiment, use the configured API and record actual responses rather than substituting host subagents.

## Stage 0: Check current evidence

Use the backend's current decision/cache and `quality_status` checks. A same-day Markdown file is a historical record, not proof that its inputs, model, policy or pending triggers remain current. Fresh risk requests and explicit user reruns require fresh preparation. Do not silently reuse an old answer after market conditions change.

## Stage 1: Prepare one immutable context

Call `prepare_committee <SYMBOL>` with the actual provider/model or composition. Retain the returned `context_id`, `analysis_as_of` / `context_prepared_at`, `prompts`, and all supplied facts. Submit that exact context; another prepare must not replace it.

| Input | Roles that receive it |
| --- | --- |
| `macro_data`, `event_brief` | Macro; CIO also receives the original source context |
| `market_data`, `regime_brief`, `sentiment_brief`, `valuation_brief` | Quant in every round, Risk in every round, CIO as supplied |
| `portfolio_summary` | Risk in every round and CIO |
| `prior_insights` | Risk, only the qualified text returned by the backend |
| `prompts.cio`, `cio_source_context` | CIO verbatim, including raw evidence and analysis time |

Regime is a rule-based classification. It does not establish that risk is normal, and a historical group return is not an independent forecast for the current observation. Missing inputs stay unknown. Do not fill them from model memory, an old conversation, or a different-date query.

## Stage 2: Independent first round

Run Macro, Quant R1 and Risk R1 in isolated tasks, using their supplied prompt verbatim and the corresponding original inputs above. Send independent tasks together when the host supports it. Wait for all outputs and retain the actual model identity and raw text for each role. Worker failure must remain visible, not become a fabricated neutral opinion.

## Stage 3: Cross-challenge

Run Quant R2 and Risk R2 with their supplied rebuttal prompts, both R1 reports, and the same original inputs they had in R1. Keep evidence, not just compressed opinions. A change must explain new evidence, a corrected reading or a changed horizon. Both adjusted signal and adjusted strength are required. Missing fields are unknown and cannot prove convergence.

## Stage 4: Optional further rounds

Use the backend's configured round limit. Agreement does not create independent evidence or a calibrated probability. Preserve genuine disagreement and unsupported-data flags; do not lower or raise a signal merely to make the group agree.

## Stage 5: CIO

Use `prompts.cio` verbatim with the complete worker history, `portfolio_summary`, original source context, sentiment, valuation and reentry reference. The host may perform this role or use a declared model according to its supported execution path; it must not invent missing worker outputs.

Separate market direction, current market risk, liquidity and user-authorized constraints. Model-proposed limits are proposals. Self-reported confidence is not calibrated probability. Preserve raw intent and let the backend apply protection once; never manipulate confidence or verdicts to avoid a guard.

Follow the current CIO output schema, including `exit_intent`, `hold_reason`, alternatives, `review_plan` / `REVIEW_PLAN_JSON` and `risk_review_plan` / `RISK_REVIEW_PLAN_JSON`. Risk/needs exits do not require a lower buyback price. A tactical reentry proposal uses its own applicable constraints. Unknown exit purpose is review-required, not evidence that holding is economically best.

A promised downside review needs independently registered, unit-correct conditions and a future expiry. Unsupported monitoring conditions must be explicitly labelled unregistered. Compare existing plan IDs before proposing replacements; do not silently move a waiting target or discard the previous commitment.

## Stage 6: Submit and verify completion

Submit all six sections with the exact `context_id` and actual model/composition:

```text
=== MACRO ===
<actual output>
=== QUANT_R1 ===
<actual output>
=== RISK_R1 ===
<actual output>
=== QUANT_R2 ===
<actual output>
=== RISK_R2 ===
<actual output>
=== CIO ===
<actual output>
```

Use `submit_committee` or the returned `save_command --context-id <context_id>`. Check the validated status, intervention reasons, plan registration and request completion receipts. Reading the queue or writing a later transcript does not complete a trigger unless its prepared context covers that request and event revision.

If submission returns `verification_required`, the six original sections are a saved draft. Delegate a separate isolated worker with the returned `system_prompt` and `user_prompt`; it checks source/logic only and must not choose a trade. Submit its JSON as `reasoning_review_json` with the **unchanged original transcript and context_id** (CLI: `--reasoning-review-file`). Repeatedly generating CIO until a preferred action or passing judgment appears is forbidden. A verification request is not completed advice; unavailable or malformed verification remains visible.

For a later task, use `committee_review CONTEXT_ID` / MCP `committee_review(context_id=...)` (hosted: `analysis_id=...`) to retrieve the exact original transcript and issued packet. `resume_allowed` must be true. A missing original is an explicit audit/recovery gap; do not guess its bytes from summaries or repeat the committee to seek approval.

`DATA_UNTRUSTED`, `REVIEW_REQUIRED`, delivery failure and an expired or triggered plan require explicit reporting even when the financial allocation is zero. Ordinary HOLD silence does not silence risk or quality alerts. Delivery is established by channel receipts, not by calling a send function. A review condition or investment verdict does not authorize an order.
