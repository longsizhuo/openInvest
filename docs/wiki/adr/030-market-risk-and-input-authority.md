# ADR-030: Separate market judgment, account feasibility and input authority

Status: accepted for engineering behavior; no investment-performance or learning approval.

## Problem

An account's unknown loss budget, an asset's usual volatility and a new market
shock answer different questions. Combining them in a single cautious label
can anchor every subsequent debate to elevated risk. Requiring new independent
measurements before correcting an earlier misreading makes that anchor persist.
Likewise, unavailable inputs must not be recorded as an ordinary HOLD.

## Decision

Risk explicitly reports comparative market risk, account capacity and liquidity.
The common formatter supplies prior-session return and ATR references, excluding
the current observation from its comparison sample. These are descriptive
statistics, not forecast probabilities or investment-action thresholds. Unknown
capacity neither authorizes an order nor proves that the market is unfavorable.
Round two can correct reasoning using the same properly attributed facts.

CIO compares holding, adding, risk reduction and tactical reentry symmetrically.
Market desirability is separate from feasibility and execution permission. Lack
of a qualified historical forecast forbids claiming its probabilities; it is not
a universal veto on adding. A valid HOLD remains possible and needs specific
counterevidence. Raw market proposals and failed qualification remain visible.

Unvalidated self-confidence and aggressive-regime direction rewrites become
explicit audit-only comparisons. Legacy PnL bands are not injected into market
judgment. Cash and concentration preferences do not create universal portfolio
percentage rules. Retained funding limits, explicit user constraints and defense
policies are separate execution protections and remain disclosed.

## Observation and authority contracts

* Quote freshness comes from provider time, successful price observation, native
  currency/units and instrument identity. Accounting currency is not conversion.
  A new partial response cannot lend its timestamp to an old cached price.
* Intraday and completed-session monitoring have independent availability.
  A session-completion witness must have been observed by its original capture
  time. A cached partial session cannot become complete just as time passes.
* Futures/FX gold is a reference, not an observed bank quote. TIP ETF price is
  not a directly measured real yield. Stale VIX is unknown.
* Mixed price bases cannot supply MA/RSI/ATR/returns or qualified advice. Missing
  required inputs produce UNCLEAR/DATA_UNTRUSTED, preserving the raw proposal.
* Unrelated audit-only lineage additions do not revoke an in-flight context.
  Real permission/revocation changes and material changes to referenced facts,
  sources, audit dependencies or review plans still invalidate that context.
* News collection defaults to every thirty minutes throughout the day. Material
  body changes, horizons and withdrawals affect risk-notice identity; timestamps,
  URL aliases and model paraphrases do not create extra mail entitlement.
* Role output structure and reasoning are distinct from source validity. Missing,
  repeated or illegal Risk fields are bound to role/round/output hash and require
  review with zero executable allocation; they do not rewrite market direction.
  A generic CIO review cannot clear an uncovered role-output defect.
* Billed empty, truncated or refused model output is observable and unavailable,
  with actual token usage retained. It is not an automatic paid retry.

## Validation and limits

Use preserved inputs and cached role responses before making new paid calls.
Freeze controlled cases, source/configuration versions, budget and expectations
before validation. Preserve failures and rejected proposals; never retry until a
preferred action appears. Synthetic controls establish behavior, not forecast
skill. Historical LLM memory and point-in-time limitations remain under ADR-022.
Native artifact acceptance and independent review precede activation. This change
does not grant source validity, learned-parameter publication or trading authority.
Automatic learning still requires independent eligible temporal evaluation.
