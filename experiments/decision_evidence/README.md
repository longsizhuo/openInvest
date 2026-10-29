# Decision evidence: offline feasibility experiment

This package validates a measurement proposal: retain distinct evidence stages,
score explicit forecast events, and test real post-processing functions with fixed
inputs. It does **not** validate investment alpha or change trading strategy defaults.

The study design and recorded deviations are in
[wiki 25](../../docs/wiki/25-decision-evidence-experiment.md).
The machine-readable protocol is [inputs/protocol.json](inputs/protocol.json).
Historical results had already been examined: this is exploratory engineering
validation, not a newly preregistered prospective efficacy study.

## Reproduce

From the repository root with project dependencies installed:

```bash
uv sync --frozen
PYTHON_DOTENV_DISABLED=1 uv run python -B -m experiments.decision_evidence.run \
  --output memory/.decision_evidence_experiment/demo
uv run python -B -m pytest experiments/decision_evidence/tests/ -q --noconftest
```

Use a fresh output name for each run. The command refuses existing directories
and outputs outside this worktree's ignored experiment directory. It does not
read credentials or start an LLM, scheduler, notification service, or broker.
The calculation/parser worker has a clean environment and blocks network,
private-input reads, external writes, and nested subprocesses.

`public_demo.json` should exactly reproduce [data/public_demo.json](data/public_demo.json).
It contains synthetic cases only. Expected controls include Brier 0.34 for two
explicit-event predictions, interval scores 3/13 for the hand-calculated examples,
and paired synthetic loss differences -0.24 / 0 / +0.16 for known signal / no signal /
uninformative overconfidence. These are constructed correctness controls, not market findings.

[data/legacy_boundary.json](data/legacy_boundary.json) preserves the initial synthetic
40-cell/16-failure baseline before the worker-failure repair. The final reference expands
coverage to 160 profile/context combinations. Tests deliberately reintroduce the old
boundary behavior in one subgroup and require the original failures to be detected.

## Optional private historical inventory

```bash
PYTHON_DOTENV_DISABLED=1 uv run python -B -m experiments.decision_evidence.run \
  --output memory/.decision_evidence_experiment/private-history \
  --committee-root /path/to/private-invest-home/memory/.committee
```

Only latest live date-directory transcripts and immediate sidecars up to the fixed
cutoff are selected. Archived runs and blinded experimental arms are never opened.
The explicit execution-declaration ledger, if present, is separately frozen; a
declaration is not treated as a verified broker fill. Original files are double-read,
copied, hashed, and rechecked against the snapshot after analysis. The protocol and
source files are fingerprinted at start and checked again at completion.

All historical snapshots, private manifests, counts, extracted amounts and records
stay under ignored `memory/`. **Do not copy them into this public package.** The
CLI prints boolean completion status only; even aggregate private counts may be
sensitive in small or complementary groups.

Some saved proposals can supply original verdict/confidence/explicit CNY amount to
a **partial-field current-rule probe with synthetic neutral context**. Missing
trim/reentry details, account state and past rule versions are not reconstructed.
This is neither an exact replay of historical decisions nor an executed portfolio
simulation. Other currencies and ambiguous amounts are excluded from that probe.

## Evidence and limitations

- `evidence.py`: complete-once local bundles with explicit unknown stages; hashes
  check integrity, not authenticity, original publication time, or source truth.
- `history.py`: fail-closed source checks; malformed/unknown/conflicting material
  remains counted. Matching date/asset sidecars never prove a shared original run.
- `scoring.py`: explicit event/horizon/record identity, mature outcomes and timestamps;
  never reinterpret HOLD or unspecified action confidence as a market probability.
- `sensitivity.py`: actual production calculations in an isolated subprocess;
  includes the worker-failure boundary counterexample and a regression simulation.
- `run.py`: integrated synthetic controls and optional private frozen inventory.

The date-block helper conditions on fixed forecasts and the existing asset pool;
it does not estimate generalization to all assets. It uses observed decision dates,
not independent dates or automatically known trading-day horizons. Short histories
do not produce an interval. Scientific validity, profitability and strategy adoption
are always separate claims requiring additional evidence.

New prospective capture is prototyped here, **not integrated into production
prepare/submit storage**. Legacy input/model/prompt/rule identity is not invented.
The current source repair is limited to the demonstrated worker-failure backstop
and its provenance; the running service is not deployed by this package.
