# Operational incident closure

An incident disappearing from notifications is not evidence that its underlying
condition has been repaired. Keep original requests, source observations,
proposals and financial records. Distinguish outcomes explicitly:

- An empty generated-plan placeholder means no plan. An unreadable or nonempty
  invalid record remains an error. Unqualified plans cannot trigger paid review.
- A budget/cooldown deferral has a reason and next eligible time. It is neither
  a completed analysis nor a failed dispatch. Changes to strategy, budget or
  revocation/eligibility state wake it for revalidation.
- A confirmed dispatch is shared only for identical retained source content and
  still-qualified original parent evidence. Unknown/in-flight or revoked-parent
  receipts cannot authorize a second call or silently become completion.
- Expired legacy requests without versioned inputs may be explicitly recorded
  as `historical_missed`. Their original rows remain, the disposition has a
  hash chain and operator/reason, and `completed` is always false.
- Search publication-date drift is acquisition metadata, not a different
  headline/snippet. Original snapshot hashes and dates remain unchanged.
  Existing qualified extraction can be reused; new or corrected text can still
  be normalized. Reuse never grants publisher truth, PIT or learning rights.
- A rejected proposal has already been reviewed and failed. Retain it as a
  reasoning-quality negative; do not call it perpetually unreviewed or retry it
  until it happens to pass.

`operational_reconcile --plan-file FILE` previews a ledger-scoped, hash-bound
reviewed plan. `--apply --operator NAME` can record historical misses, retire
unqualified generated plans without moving targets, close text-identical
metadata conflicts and register insufficient historical evidence for audit.
It cannot modify balances, grant source/learning rights or enable trading.
`--bootstrap-dispatches` independently compares old exact source snapshots with
current qualified input identity; a bridge means dispatch confirmation only.
All evidence reads and conflict writes use a consistent locked transaction.

Missing original acquisition evidence remains missing. Current fetching cannot
prove an old availability time. A current balance observation cannot establish
old trades, fees, costs or historical returns.
