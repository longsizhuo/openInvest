# Decision evidence experiment: feasibility and correctness

Status: protocol fixed before this experiment's historical scan; **exploratory** because the
project's historical results have already been examined. This is not a new prospective
investment trial and does not amend D21/D22 or any blinded fleet experiment.

## Purpose and scope

Validate the proposed measurement design before changing investment behavior:

1. Preserve input/proposal/rule/execution evidence separately, with immutable run records.
2. Score explicitly defined forecast events; do not infer a market forecast from HOLD,
   a missing decision, or an unspecified action-confidence value.
3. Use identical inputs to test whether calibration and post-processing actually respond.
4. Preserve paired observations and market-date dependence in offline comparisons.

The work runs on `test_decision_evidence` in an isolated worktree. No running production
configuration, accounts, notifications, or broker/scheduler paths are changed.
No new LLM calls or trading orders are necessary for this feasibility study.

## Experiments and fixed acceptance conditions

| Stage | Test | Acceptance condition |
|---|---|---|
| E0 | Isolated execution and immutable evidence | Imports/replay do not use credentials, network, production writes, or persisted account overrides. Snapshotted input hashes remain identical. Conflicting rewrites of a run are rejected. |
| E1 | Historical evidence inventory | Every scanned record is accounted for, including malformed/missing/unknown records. Date-and-asset sidecars are only candidate associations, never proof of the same run. Missing execution evidence is unknown, not 'not traded'. |
| E2 | Event semantics and arithmetic | Hand-calculated Brier/interval scores match; wrong event/horizon, nonfinite probabilities, future information, immature outcomes and duplicate paired rows are rejected. Legacy confidence without an explicit event is not scored. |
| E3 | Fixed-input sensitivity | Real production pure functions pass identity, negative-control and boundary cases. Calibration responds to conditional inputs when its weight is nonzero; equal conditional/unconditional inputs and gamma-only probability changes are null controls. Rule changes are attributed to the rule, not the model. |
| E4 | Paired statistical protocol | All assets on a sampled market date stay paired; repeated same-date entities do not create independent dates. A known constant loss difference has the exact expected estimate/interval. Short histories explicitly lack enough blocks for inference. |
| E5 | End-to-end reproducibility | The public synthetic run reproduces its checked-in artifact. A private frozen historical scan finishes with unchanged input hashes, honest eligibility counts and no private content in the public artifact. |

Passing these gates establishes feasibility of the **measurement and replay tools**, not
forecast alpha, profitability, model superiority, or the value of a new trading strategy.
Historical replay eligibility may legitimately be zero: that result blocks a historical
effectiveness claim rather than being silently repaired with invented input/configuration.

## Data handling and selection

- Use only completed live transcript directories `.committee/YYYY-MM-DD/*.md` up to a
  specified cutoff, plus their immediate JSON sidecars. Do not inspect `runs/`, fleet,
  sealed, debate, macro-soft or other not-yet-due experiments.
- Capture files into a new ignored `memory/.decision_evidence_experiment/` directory.
  Refuse overlapping source/output directories, symlink inputs and changed source files.
- Preserve file hashes and relative locations privately. Never publish raw transcripts,
  actual account amounts, identifiers, exact decision dates, or private absolute paths.
- Public reproducibility data are synthetic. Private inventory outputs stay ignored;
  published narrative may say only whether the real-data run completed and what evidence
  was insufficient. No small-group private rates are published.
- Do not relabel legacy records as having known model/prompt versions, explicit probability
  events or actual executions. Do not replay historical rules using today's defaults and
  call the result a reproduction of the past.

## Statistical interpretation

The paired comparison estimates the average **market-date** difference after equal-weight
averaging the entities observed on each date. It is not a random-asset-population estimate.
Moving date blocks retain within-date cross-asset dependence; for overlapping targets,
block length must cover at least the forecast horizon in observed market dates. Report
rows, dates, span and usable blocks. A cross-zero interval is not equivalence.

Synthetic known-signal/null controls check implementation. They do not establish universal
false-positive control under every financial process. Actual predictive comparisons need
frozen forecast outputs, matured labels, training-only baselines, an adequate untouched
time window and prespecified inference. Those prerequisites cannot be supplied by
retroactively changing the historical confidence semantics.

## Decision after this experiment

- Open a research-package PR only when E0–E5 pass and independent review has no unresolved
  correctness/privacy issues. Include negative and missing-evidence findings.
- Do not change production actions based on a successful engineering experiment.
- Subsequent strategy experiments must separately fix the main event/horizon/baseline,
  economic effect threshold, cost/account model, out-of-sample split and multiple-testing
  policy. Existing delayed-DCA evidence is not evidence about production defense downgrades
  or batched gold purchases.

## References used for design, not numerical authority

- [CLQT v3, §4 / §7.9](https://arxiv.org/html/2606.29771v3): separate intent,
  rule projection and execution; its composite score is not our ground truth.
- [Calibration-induced degeneracy, §9](https://arxiv.org/html/2608.20304v1): test
  feature-to-output sensitivity before acquiring more expensive features.
- [Production fleet study, §9](https://arxiv.org/html/2609.05663v1): historical versions,
  date-aware comparisons and complete reporting of failed attempts.
- [Gneiting and Raftery (2007)](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf):
  proper scores assess probabilistic forecasts; calibration alone is not discrimination.

The literature audit found unresolved numerical/availability issues in some recent
preprints. Their reported performance numbers are not acceptance thresholds for this work.

## Recorded deviation D1: worker-failure boundary repair

The frozen sensitivity cases exposed a real parser defect: with a valid worker-failure
sentinel and confidence at or below its 0.4 ceiling, the old implementation left directional
actions and nonzero allocations in place. The isolated baseline passed mechanism checks
but failed 16 synthetic safety boundary cases; it made zero network/private-read/outside-write
attempts. Baseline logs remain in the ignored private experiment directory.

The scope therefore includes a minimal source fix **on the experimental branch only**:
apply the existing failure backstop whenever the failure sentinel is present. The resulting
confidence remains capped with `min`, so this never raises confidence. Add regression cases
for text and structured input, below/at/above the ceiling, both buy and sell directions,
and a negated-marker control. The original experiment thresholds are unchanged; record
before and after results. This is an input-failure correctness repair, not a new investment
strategy and not a deployment authorization.

The same check also exposed missing provenance: ordinary worker-failure holds did not
retain `_original_verdict`, causing `forced_hold()` to report false. Preserve the earliest
original action/allocation with `setdefault`, matching the existing parser's tracing contract.
Input failures remain excluded from the economic intervention ledger: a failed-input
proposal is not a valid forgone trading opportunity. Its diagnostic provenance is retained
in Input Health, including when an earlier rule had already changed the proposal.

Follow-up boundary review also checks both risk profiles and rising/range-bound contexts.
The aggressive uptrend rule must not promote a failed-input HOLD back to ACCUMULATE.
The backstop therefore remains authoritative through the end of post-processing.
This expands the original 40-cell matrix to 160 profile/context combinations without
changing any safety threshold. Preserve the original 40-cell/16-failure baseline;
the expanded post-fix matrix is a follow-up validation, not a rewritten initial result.

## Completed feasibility readout

The integrated synthetic run passed all engineering gates. The saved original 40-cell
baseline has 16 failed worker-boundary cells; the repaired, expanded 160-cell grid has
zero failures. The isolated worker recorded no network, private-input, external-write or
nested-process attempts. Tests also actively attempt forbidden operations and simulate
the original defect, so a passing JSON response alone is not the acceptance criterion.

The private frozen historical inventory and partial-field current-rule probes completed,
with original inputs and experiment code unchanged throughout the run. Historical rule
reconstruction and probability-scoring eligibility remain unavailable under this strict
evidence definition. This is an evidence gap, not a failed parsing case to fill with
current defaults. No private counts, identifiers, dates, amounts or transcripts are
included in the public package.

Validation on the branch: 2,455 production tests passed (3 skipped); 92 experiment tests
and 28 subtests passed; all three import-layer contracts passed. The synthetic reference
artifact reproduces byte-for-byte. CI runs the research tests explicitly.

Signal impact of the minimal repair: a failed worker prevents a directional proposal
regardless of its self-reported confidence, and aggressive mode cannot revive that
failed-input hold. Failure provenance is retained without counting it as a valid
forgone investment in the economic intervention ledger. No strategy performance or
profitability improvement has been established by this experiment.
