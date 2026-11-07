# Committee execution paths

Both paths use the backend's prepared evidence, role prompts, decision validation
and sealed submission. They differ in who calls the model APIs.

| Path | Caller | Requirements |
|------|--------|--------------|
| Coordinator | Host delegates real isolated role workers | Complete `prepare_committee` / submit contract, recorded provider/model; follow [committee protocol](committee-protocol.md) or [Hermes protocol](committee-protocol-hermes.md) |
| Direct | Backend calls configured model API | Backend credentials and available provider; `run.sh run_committee SYM` |

A scheduled host may use Coordinator only if it can fulfill the same contract as
an interactive host. If delegation or prepared prompts are unavailable, use Direct
when configured, otherwise expose the incomplete analysis and pending work. Never
simulate worker outputs or manufacture HOLD. Honor the user's chosen research API.

Always prepare current inputs. Only the backend may approve cache reuse after
checking source versions, model/prompt/config versions and all pending requests.
A same-day transcript is not a cache eligibility check. Complete only requests
actually covered by the submitted context and final recorded decision.

Record real provider/model and API usage. Different models may disagree; compare
their source dates, input completeness, user constraints and rules. Agreement by
models using the same sources does not create independent market evidence and
does not calibrate confidence. Evaluation requires qualified, mature outcomes and
time-separated validation under the registered evaluation protocol.

Costs depend on provider, model, input size, rounds and retries. Use actual usage
receipts; do not promise a fixed per-run price or provider free tier. Risk alerts
are observations and can be sent without waiting for a trading verdict; delivery
and subsequent analysis have separate recorded states. Pending, constrained,
untrusted or failed work must remain visible.

For source architecture see `docs/wiki/04-execution-paths.md` and ADR-001. Backend
prepared prompts are the authority for role instructions; do not maintain a second
set of market thresholds in this reference.
