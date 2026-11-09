"""Bind proposed price scenarios to sealed facts; retain mistakes for audit."""
from __future__ import annotations

import hashlib
import json
import math
import re

from openinvest.calc.position_scenarios import no_trade_price_scenario,number,canonical_unit
from openinvest.core.evidence_eligibility import digest

# This legacy protocol field mixed prose, different PnL bases and scenario
# amounts. Financial scenarios now use typed requests and backend arithmetic.
LEGACY_STRESS=re.compile(r'(?im)^\s*(?:[-*]\s*)?(?:\*\*)?stress_scenario_loss_[a-z]{3}(?:\*\*)?\s*:')
# Narrative is qualitative. Prices/amounts/quantities belong in their typed
# fields; the backend owns financial arithmetic and the official scenario block.
_CURRENCY=r'(?:CNY|CNH|USD|AUD|EUR|GBP|JPY|HKD|CAD|CHF|NZD|SGD|INR|KRW|TWD|THB|IDR|MYR|PHP|VND|ZAR|BRL|MXN|RUB|TRY|AED|SAR|ILS|SEK|NOK|DKK|PLN|CZK|HUF|ISK|人民币|澳元|美元|港元|日元|欧元|元|块|円)'
_NUMBER=r'[-+−]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)'
_MONEY=re.compile(r'(?:[$¥￥€£]\s*'+_NUMBER+r'|(?<![A-Za-z_])'+_CURRENCY+r'\s*'+_NUMBER+r'|'+_NUMBER+r'\s*'+_CURRENCY+r'(?![A-Za-z_])|'+_NUMBER+r'\s*[$¥￥€£])')
_NUMERIC=re.compile(r'(?<![A-Za-z0-9_])'+_NUMBER+r'|(?:归|为|等于|约为)\s*[零〇]|[零〇]\s*(?:元|损失|回撤|盈亏)|\bzero\b',re.I)
_ACCOUNT_METRIC=re.compile(r'账面|浮盈|浮亏|已实现|未实现|持仓成本|平均成本|均价|成本基准|持仓数量|持仓市值|现金|预算|金额|限额|市值变化|盈亏|压力测试|情景损失|损失金额|\b(?:pnl|p&l|realized|unrealized|cash|budget|cost[ -]basis|position[ -](?:value|size|quantity)|portfolio[ -]value|mark[ -]to[ -]market|scenario[ -]loss|stress[ -]test)\b',re.I)
_PNL_METRIC=re.compile(r'账面|浮盈|浮亏|已实现|未实现|盈亏|\b(?:pnl|p&l|realized|unrealized|book[ -]drawdown|mark[ -]to[ -]market)\b',re.I)
_PROTOCOL=re.compile(r'^\s*(?:\*\*)?(?:VERDICT|CONFIDENCE|DOMINANT_VIEW|SUGGESTED_ALLOC_[A-Z]{3}|TRIM_REASON|EXIT_INTENT|HOLD_REASON|(?:REVIEW_PLAN|RISK_REVIEW_PLAN|SCENARIO_ANALYSIS|MARKET_ASSESSMENT|ALTERNATIVES)_JSON|REENTRY_PRICE)(?:\*\*)?\s*:',re.I)


def _text_shape(value):
    return value is None or isinstance(value,str) or (isinstance(value,list) and all(isinstance(item,str) for item in value))


def narrative_schema_errors(fields):
    """Only declared numeric/reference slots are exempt from prose checking."""
    errors=[]
    def fail(path):errors.append({'code':'proposal_schema_invalid','path':path})
    for name in ('market_assessment','review_plan','risk_review_plan'):
        value=fields.get(name)
        if value is not None and not isinstance(value,dict):fail(name);continue
        if not value:continue
        allowed=({'direction','horizon','support','counterevidence','execution_data_gaps'} if name=='market_assessment'
                 else {'conditions','logic','expires_at','replaces_plan_id','change_reason'})
        if set(value)-allowed:fail(name+'.unknown_fields')
        if name=='market_assessment':
            if any(not _text_shape(v) for v in value.values()):fail(name+'.text_shape')
        else:
            conditions=value.get('conditions',[])
            if not isinstance(conditions,list):fail(name+'.conditions');continue
            for i,item in enumerate(conditions):
                if not isinstance(item,dict) or set(item)-{'metric','op','value','unit'}:fail(name+f'.conditions.{i}')
            if any(not _text_shape(value.get(k)) for k in ('expires_at','replaces_plan_id','change_reason')):fail(name+'.text_shape')
    alternatives=fields.get('alternatives')
    if alternatives is not None:
        if not isinstance(alternatives,list):fail('alternatives')
        else:
            allowed={'kind','applicability','market_evaluation','exit_intent','support','counterevidence','constraint_refs','assumptions','data_gaps'}
            for i,item in enumerate(alternatives):
                if not isinstance(item,dict):fail(f'alternatives.{i}');continue
                if set(item)-allowed or any(not _text_shape(v) for v in item.values()):fail(f'alternatives.{i}.shape')
                refs=item.get('constraint_refs') or []
                if not isinstance(refs,list) or any(not isinstance(ref,str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:/#-]*',ref) for ref in refs):
                    fail(f'alternatives.{i}.constraint_refs')
    return errors


def _string_leaves(value,path):
    if isinstance(value,str):yield path,value
    elif isinstance(value,dict):
        for name,item in value.items():yield from _string_leaves(item,path+'.'+str(name))
    elif isinstance(value,list):
        for i,item in enumerate(value):yield from _string_leaves(item,path+'.'+str(i))


def untyped_financial_statements(memo, fields=None, *, currencies=()):
    """Check every retained prose leaf; only schema-shaped slots are exempt.

    This is an output-format contract, not a claim that a natural-language
    parser can prove every possible sentence. Official amounts are backend
    calculations; original model bytes remain audit evidence.
    """
    from openinvest.core.committee.cio_parse import strict_cio_json,STRUCTURED_CIO_FIELDS
    fields=fields if isinstance(fields,dict) else {}
    structure=narrative_schema_errors(fields)
    structure.extend({'code':'invalid_protocol_json','path':name}
        for name in STRUCTURED_CIO_FIELDS if fields.get('_'+name+'_error'))
    fragments=list(_string_leaves(fields,'fields'))
    for line in str(memo or '').splitlines():
        matched=_PROTOCOL.match(line)
        if matched:
            value=line[matched.end():].strip().removesuffix('**').strip()
            header=line[:matched.end()].replace('**','').strip().rstrip(':').upper()
            if header.endswith('_JSON'):
                try:
                    parsed=strict_cio_json(value)
                    name=header[:-5].lower()
                    structure.extend(narrative_schema_errors({name:parsed}))
                    fragments.extend(_string_leaves(parsed,'protocol.'+name))
                    continue
                except (ValueError,TypeError):pass
            elif re.fullmatch(r'[A-Za-z_]+|N/A|null|[-+]?\d+(?:\.\d+)?',value):
                continue
        fragments.append(('memo',line))
    clauses=[(path,part.strip()) for path,text in fragments
             for part in re.split(r'[，；。！？;!?\n]|(?<!\d),(?!\d)|(?<!\d)\.(?!\d)',text) if part.strip()]
    native=[re.escape(c) for c in currencies if isinstance(c,str) and re.fullmatch('[A-Z]{3}',c)]
    native_money=(re.compile(r'(?:'+('|'.join(native))+r')\s*'+_NUMBER+r'|'+_NUMBER+r'\s*(?:'+('|'.join(native))+r')') if native else None)
    return structure+[{'code':'untyped_financial_narrative','path':path,'claim_sha256':hashlib.sha256(text.encode()).hexdigest(),
             'claim_quote':text,'reason':'financial_quantities_require_typed_source_or_backend_calculation'}
            for path,text in clauses if _MONEY.search(text) or (native_money is not None and native_money.search(text))
            or any(_NUMERIC.search(text[m.end():]) for m in _ACCOUNT_METRIC.finditer(text))
            or (_PNL_METRIC.search(text) and _NUMERIC.search(text))]


BASES={'current_market_value':'quantity_times_current_mark','scenario_market_value':'quantity_times_scenario_mark',
       'mark_to_market_change':'scenario_mark_minus_current_mark','current_unrealized_pnl':'current_mark_minus_cost',
       'scenario_unrealized_pnl':'scenario_mark_minus_cost','realized_pnl_change':'assumed_no_trade_increment',
       'realized_pnl_total':'execution_history_required','maximum_drawdown':'price_path_required'}


def validate_scenarios(position, requests, *, memo='', context_id=None, fields=None):
    supplied=position if isinstance(position,dict) else {}
    currencies=[(supplied.get('quote') or {}).get('currency'),(supplied.get('cost') or {}).get('currency')]
    errors=untyped_financial_statements(memo,{**(fields or {}),'scenario_analysis':requests},currencies=currencies);results=[]
    if (fields or {}).get('_duplicate_protocol_fields'):
        errors.append({'code':'duplicate_protocol_fields','fields':fields['_duplicate_protocol_fields']})
    if (fields or {}).get('_unrecognized_json_fields'):
        errors.append({'code':'unrecognized_cio_json_fields','fields':sorted(fields['_unrecognized_json_fields'])})
    presentation=str(memo).replace('**','')
    for line in presentation.splitlines():
        if LEGACY_STRESS.search(line) and line.split(':',1)[-1].strip().lower() not in {'n/a','unknown','null','未知','未提供'}:
            errors.append({'code':'untyped_financial_statement','reason':'legacy_stress_loss_prose_is_not_a_verified_amount'})
            break
    if requests is None:requests=[]
    if not isinstance(requests,list):
        errors.append({'code':'scenario_schema_invalid'});requests=[]
    position=supplied;expected_ref=position.get('position_ref')
    if position and expected_ref!=digest({k:v for k,v in position.items() if k!='position_ref'}):
        errors.append({'code':'position_snapshot_binding_invalid'});position={};expected_ref=None
    seen=set()
    for request in requests:
        if not isinstance(request,dict):errors.append({'code':'scenario_schema_invalid'});continue
        ident=request.get('scenario_id');row_errors=[]
        if set(request)-{'scenario_id','position_ref','context_ref','target_price','currency','unit','assumption','reason','claimed_metrics'}:
            row_errors.append('scenario_unknown_field')
        errors.extend(untyped_financial_statements(request.get('reason',''),currencies=currencies))
        if not isinstance(ident,str) or not ident.strip() or ident in seen:
            row_errors.append('scenario_id_invalid_or_duplicate')
        else:seen.add(ident)
        if not expected_ref or request.get('position_ref')!=expected_ref:row_errors.append('scenario_position_binding_invalid')
        if request.get('context_ref') is not None and request['context_ref']!=context_id:row_errors.append('scenario_context_binding_invalid')
        if request.get('assumption')!='no_trade':row_errors.append('unsupported_scenario_assumption')
        target=request.get('target_price');currency=request.get('currency');unit=request.get('unit')
        if not number(target) or target<0:row_errors.append('scenario_target_price_invalid')
        quote=position.get('quote') or {};quantity=position.get('quantity') or {}
        if not isinstance(currency,str) or not re.fullmatch('[A-Z]{3}',currency):row_errors.append('scenario_currency_invalid')
        if canonical_unit(unit) is None:row_errors.append('scenario_unit_invalid')
        if quote.get('currency') and currency!=quote['currency']:row_errors.append('scenario_currency_mismatch')
        if ((quote.get('unit') and canonical_unit(unit)!=canonical_unit(quote['unit'])) or (quantity.get('unit') and canonical_unit(unit)!=canonical_unit(quantity['unit']))):
            row_errors.append('scenario_unit_mismatch')
        if row_errors:
            errors.extend({'code':code,'scenario_id':ident} for code in row_errors);continue
        unit=quote.get('unit') or unit
        result=no_trade_price_scenario(position,target,currency,unit)
        result.update(scenario_id=ident,position_ref=expected_ref,context_ref=context_id,
                      model_reason=request.get('reason') or '')
        claims=request.get('claimed_metrics',[])
        if not isinstance(claims,list):errors.append({'code':'scenario_claims_invalid','scenario_id':ident});claims=[]
        for claim in claims:
            if not isinstance(claim,dict):errors.append({'code':'scenario_claim_invalid','scenario_id':ident});continue
            if set(claim)-{'metric','value','basis','currency'}:
                errors.append({'code':'scenario_claim_unknown_field','scenario_id':ident})
            metric=claim.get('metric');expected=result['metrics'].get(metric)
            reason=None
            if expected is None:reason='scenario_metric_unknown'
            elif claim.get('basis')!=expected['basis']:reason='scenario_metric_basis_mismatch'
            elif claim.get('currency')!=currency:reason='scenario_metric_currency_mismatch'
            elif expected['value'] is None:
                if claim.get('value') is not None:reason='scenario_unknown_metric_claimed_as_known'
            elif (not number(claim.get('value')) or not math.isclose(claim['value'],expected['value'],rel_tol=1e-9,abs_tol=.005)):
                # Half a native cent is a rounding check, never a market rule.
                reason='scenario_amount_mismatch'
            if reason:errors.append({'code':reason,'scenario_id':ident,'metric':metric})
        results.append(result)
    return {'schema':'openinvest.scenario_validation.v1','status':'invalid' if errors else 'not_requested' if not requests else 'evaluated',
            'position_ref':expected_ref,'results':results,'errors':errors,
            'memo_sha256':hashlib.sha256(str(memo).encode()).hexdigest(),
            'source_validity_changed':False,'trading_or_learning_permission_granted':False}


def apply_scenario_validation(verdict, validation):
    verdict['scenario_validation']=validation
    if not validation['errors']:return
    verdict.setdefault('raw_market_verdict',(verdict.get('_raw_proposal') or {}).get('verdict',verdict.get('verdict')))
    verdict.setdefault('_original_alloc',verdict.get('alloc_cny'))
    verdict['alloc_cny']=0
    if verdict.get('decision_status')!='DATA_UNTRUSTED':
        verdict.update(decision_status='REVIEW_REQUIRED',qualification_reason='financial_scenario_invalid')


def render_scenarios(validation):
    rows=validation.get('results') or []
    if not rows:return ''
    lines=['\n\n### 场景金额（后端复算，原生币种）',
           '固定数量、不交易的假设；不含费用、分红或汇率变化。金额未知时不填零。']
    for row in rows:
        def amount(name):
            value=row['metrics'][name]['value']
            return '未知' if value is None else (f"{value:+.6g} {row['currency']}" if 0<abs(value)<.005 else f"{value:+.2f} {row['currency']}")
        lines.extend([f"- 目标价 {row['target_price']:g} {row['currency']}/{row['unit']}；报价时点 {row.get('quote_asof') or '未知'}",
                      f"  相对当前市值变化：{amount('mark_to_market_change')}；场景未实现盈亏：{amount('scenario_unrealized_pnl')}。",
                      '  不交易时的已实现盈亏增量为 0；历史累计已实现盈亏未知。'])
    lines.append('上述为价位假设，不是最大回撤、损失上限、发生概率或交易指令。')
    return '\n'.join(lines)


def proposed_scenarios(verdict):
    if verdict.get('_scenario_analysis_error'):
        return {'parse_error':verdict['_scenario_analysis_error']}
    return verdict.get('scenario_analysis')


def decision_display_memo(verdict, original, validation):
    """Display checked amounts; keep unqualified model prose only in evidence."""
    from openinvest.core.decision_evidence import advice_qualified
    from openinvest.capabilities.committee.i18n import bilingual
    if advice_qualified(verdict):
        from openinvest.core.committee.cio_parse import single_invest_cap_note
        note=single_invest_cap_note(verdict)
        if note and note not in original:original+='\n\n⚠️ '+note
        return original+render_scenarios(validation)
    status=verdict.get('decision_status','REVIEW_REQUIRED')
    raw=(verdict.get('_raw_proposal') or {}).get('verdict') or verdict.get('raw_market_verdict')
    if raw not in {'BUY','ACCUMULATE','HOLD','TRIM','SELL'}:raw='UNCLEAR'
    text=bilingual(
        f"[QUALITY_ALERT] {status}：当前没有已核验的执行建议。原始市场判断：{raw}（未通过完整资格核验）。\n"
        "执行金额为零；未核验的财务数字不进入正式报告，完整原文已封存供审计。",
        f"[QUALITY_ALERT] {status}: no qualified executable advice. Original market proposal: {raw} (unqualified).\n"
        "Executable allocation is zero. Unchecked financial prose is withheld from the report; original bytes remain in audit evidence.")
    return text+(render_scenarios(validation) if status!='DATA_UNTRUSTED' else '')
