# Research schema v1

All JSON numbers must be finite. Private input files are not public fixtures.

## Immutable local bundle

`{schema_version:1, payload_sha256, payload}`. The digest covers canonical UTF-8
JSON (sorted keys, compact separators, final LF). Payload contains a safe `run_id`,
`evidence_type` (`synthetic`, `historical_capture`, `prospective`) and separate
`inputs`, `proposal`, `validated`, `execution` mappings or explicit nulls.

A null is unknown evidence, not a zero allocation, a HOLD prediction, an unfilled
order, or an instruction to trade. Retrying identical bytes is allowed; changing
an existing run is rejected. Different attempts need different run IDs even with
identical inputs. Historical capture IDs identify captured bytes, not original
run identities or independent statistical samples.

This format does not authenticate its producer or verify broker receipts. A
`prospective` label alone does not prove a run's timestamp or completeness.

## Explicit binary forecasts

`score_records` requires `record_id`, `event_id`, `horizon`, `probability`, bool
`outcome`, and timezone-aware ISO strings `forecast_at`, `information_available_at`,
`outcome_at`, `evaluated_at`. `record_id` is nonempty and unique within the batch.
Information must be available by forecast time; the outcome must occur strictly
later and be available by evaluation. The outcome timestamp must denote when the
label becomes observable, including publication delay where applicable.

The event and horizon must match the explicitly selected cohort exactly. The
helper treats the horizon as an opaque identifier; the label adapter must define
calendar/trading days, target price, return units and corporate actions beforehand.
The protocol's demo event is positive terminal return over 30 calendar days.

Action/confidence fields are not substitutes. No baseline is fitted using test
outcomes. The descriptive row-average Brier score does not imply independent rows.

## Paired date blocks

Rows contain `date` (strict YYYY-MM-DD), `entity_id`, `loss_a`, `loss_b`; duplicate
date/entity pairs are rejected. The estimand is an equal-date average of the
within-date equal-entity paired loss difference. Negative values favor A.

`block_days` counts sorted observed dates; all assets and methods remain paired.
`n_nonoverlapping_blocks` is a coverage diagnostic, not an independent sample count.
The caller must choose an appropriate length for overlapping outcomes. Fewer than
two complete blocks produces no interval. Results never certify strategy efficacy.

## Historical inventory and capture

Only `summary` is structurally anonymous, but private small-group counts still
must not be published automatically. `private_records` and `private_sources`
contain identifiers and source fingerprints. Raw action-confidence semantics are
unknown; probabilities and actual executions are never inferred from them.

The snapshot manifest explicitly distinguishes live committee files from the
execution-declaration ledger. It records relative locations, size and SHA-256;
unknown schema, duplicate entries, symlinks, blind-experiment paths and changed
source inventories are rejected. A manifest is not evidence of original pre-outcome
availability. Local protocol/code fingerprints likewise are not an external
timestamped preregistration.
