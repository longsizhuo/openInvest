"""Producer-scoped evidence captured at the common journal write boundary."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date,datetime

_SCOPE=ContextVar('ledger_producer_scope',default=None)
DOMAINS=frozenset({'client_reported','broker_live','broker_paper','broker_unknown','paper_simulation','projection','unknown'})


@contextmanager
def recording_scope(*,evidence_basis,execution_domain,source_reference=None,trade_date=None,
                    execution_time=None,execution_time_precision=None,fee_status=None):
    """Internal producers declare observed facts without rewriting their values.

    An unknown commission is not a zero fee, and a day-only receipt is not an
    invented midnight execution. This does not place an order or confer learning
    eligibility. The runtime's client endpoints retain their separate authority.
    """
    if execution_domain not in DOMAINS or not isinstance(evidence_basis,str) or not evidence_basis:
        raise ValueError('invalid_ledger_producer_scope')
    if trade_date is not None:date.fromisoformat(str(trade_date))
    if execution_time is not None:
        instant=datetime.fromisoformat(str(execution_time).replace('Z','+00:00'))
        if instant.tzinfo is None:raise ValueError('broker execution timestamp requires timezone')
    if fee_status not in (None,'unknown','reported_zero','reported','estimated'):raise ValueError('invalid_fee_status')
    if execution_time_precision not in (None,'day','instant','unknown'):raise ValueError('invalid_execution_time_precision')
    from openinvest.core.decision_evidence import _plain
    value=_plain({'evidence_basis':evidence_basis,'execution_domain':execution_domain,
                  **({'source_reference':source_reference} if source_reference is not None else {}),
                  **({'trade_date':str(trade_date)} if trade_date is not None else {}),
                  **({'execution_time':str(execution_time)} if execution_time is not None else {}),
                  **({'execution_time_precision':execution_time_precision} if execution_time_precision else {}),
                  **({'fee_status':fee_status} if fee_status else {})})
    token=_SCOPE.set(value)
    try:yield
    finally:_SCOPE.reset(token)


def journal_provenance(row,*,ledger_domain=None):
    value=dict(_SCOPE.get() or {})
    if not value:
        from openinvest.calc.ledger_replay import execution_basis,execution_domain
        value={'evidence_basis':execution_basis(row),'execution_domain':execution_domain(row)}
        if ledger_domain in {'paper_simulation','broker_paper'} and not row.get('execution_domain'):
            value['execution_domain']=ledger_domain
    reference=row.get('source_reference')
    if isinstance(reference,dict) and isinstance(value.get('source_reference'),dict):
        value['source_reference']={**reference,**value['source_reference']}
    explicit=row.get('evidence_basis')
    if explicit and explicit not in {'client_reported_not_independently_verified','broker_observed_fill'}:
        value['evidence_basis']=explicit
    from openinvest.calc.ledger_replay import execution_basis,execution_domain
    merged={**row,**value}
    value.update(evidence_basis=execution_basis(merged),execution_domain=execution_domain(merged))
    return value
