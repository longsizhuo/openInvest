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
_CURRENCY_PREFIX=r'(?:CNY|CNH|USD|AUD|EUR|GBP|JPY|HKD|CAD|CHF|NZD|SGD|INR|KRW|TWD|THB|IDR|MYR|PHP|VND|ZAR|BRL|MXN|RUB|TRY|AED|SAR|ILS|SEK|NOK|DKK|PLN|CZK|HUF|ISK|人民币|澳元|美元|港元|日元|欧元|円)'
_NUMBER=r'[-+−]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)'
_MONEY=re.compile(r'(?:[$¥￥€£]\s*'+_NUMBER+r'|(?<![A-Za-z_])'+_CURRENCY_PREFIX+r'\s*'+_NUMBER+r'|'+_NUMBER+r'\s*'+_CURRENCY+r'(?![A-Za-z_])|'+_NUMBER+r'\s*[$¥￥€£])')
_NUMERIC=re.compile(r'(?<![A-Za-z0-9_])'+_NUMBER+r'|(?:归|为|等于|约为)\s*[零〇]|[零〇]\s*(?:元|损失|回撤|盈亏)|\bzero\b',re.I)
_NEGATED_WORD_ZERO=re.compile(r'(?:不(?:为|等于|是)|并非|不是|非)\s*[零〇]|\b(?:not|never)(?:\s+equal\s+to)?\s+zero\b',re.I)
_ACCOUNT_METRIC=re.compile(r'账面|浮盈|浮亏|已实现|未实现|持仓成本|平均成本|均价|成本基准|持仓数量|持仓市值|现金|预算|金额|限额|市值变化|盈亏|压力测试|情景损失|损失金额|\b(?:pnl|p&l|realized|unrealized|cash|budget|cost[ -]basis|position[ -](?:value|size|quantity)|portfolio[ -]value|mark[ -]to[ -]market|scenario[ -]loss|stress[ -]test)\b',re.I)
_PNL_METRIC=re.compile(r'账面|浮盈|浮亏|已实现|未实现|盈亏|\b(?:pnl|p&l|realized|unrealized|book[ -]drawdown|mark[ -]to[ -]market)\b',re.I)
_PROTOCOL=re.compile(r'^\s*(?:\*\*)?(?:VERDICT|CONFIDENCE|DOMINANT_VIEW|SUGGESTED_ALLOC_[A-Z]{3}|TRIM_REASON|EXIT_INTENT|HOLD_REASON|(?:REVIEW_PLAN|RISK_REVIEW_PLAN|SCENARIO_ANALYSIS|MARKET_ASSESSMENT|ALTERNATIVES|MARKET_CITATIONS)_JSON|REENTRY_PRICE)(?:\*\*)?\s*:',re.I)
_MARKET_SOURCES={'market':('market_data','market_input'),'macro':('macro_data','macro_input'),
                 'events':('event_brief','normalized_reports_not_verified_truth')}
# Quotes are a narrow market-report channel. Select a shorter source span if
# surrounding text discusses an account, funds, quantities, costs or PnL.
_CITATION_ACCOUNT_SCOPE=re.compile(r'账户|帐户|余额|你的|我的|个人|持仓|仓位|组合|资金|成本|止损|交易金额|数量|份额|股数|手数|损益|亏损|盈利|'
    r'\b(?:account|balance|your|my|personal|portfolio|position|holdings?|quantity|cost|profit|loss|stop[ -]loss)\b',re.I)
_REFERENCE_FINANCIAL_LABEL=re.compile(_ACCOUNT_METRIC.pattern+'|'+_PNL_METRIC.pattern+
    r'|账户|帐户|余额|持仓|仓位|组合|资金|成本|数量|份额|股数|手数|损益|亏损|盈利|'
    r'\b(?:account|balance|portfolio|position|holdings?|quantity|cost|profit|loss)\b',re.I)
_VALUE_LINK=re.compile(r'^(?:[\s,，:：=;；.。\[\]()（）"\'“”‘’*_`<>-]|'
    r'等于|不等于|约为|低于|高于|小于|大于|不少于|不超过|不为|不是|为|是|约|值|的|见|参考|'
    r'金额|数额|余额|数量|成本|市值|损失|亏损|盈亏|'
    r'\b(?:is|are|equals?|equal|to|not|about|approximately|value|amount|total|balance|loss|pnl|of|at|see|reference|above|below)\b)*$',re.I)


def market_citation_catalog(facts):
    """Project only original market inputs; hashes bind bytes, never truth."""
    facts=facts if isinstance(facts,dict) else {}
    result={}
    for source_id,(key,kind) in _MARKET_SOURCES.items():
        text=facts.get(key)
        if isinstance(text,str) and text.strip() and text.strip()!='not_provided':
            result[source_id]={'kind':kind,'text':text,'source_sha256':hashlib.sha256(text.encode()).hexdigest()}
    return result


def validate_market_citations(citations, *, market_sources=None, context_id=None):
    """Bind exact report quotations without granting any financial authority."""
    errors=[];rows=[]
    def fail(code,index=None):
        errors.append({'code':code,'path':'market_citations'+('' if index is None else f'.{index}')})
    if not isinstance(citations,list):
        fail('market_citations_schema_invalid');citations=[]
    sources=market_sources if isinstance(market_sources,dict) else {}
    ids=[item.get('citation_id') for item in citations if isinstance(item,dict) and isinstance(item.get('citation_id'),str)]
    for index,item in enumerate(citations):
        before=len(errors)
        if not isinstance(item,dict):fail('market_citation_schema_invalid',index);continue
        if (not {'citation_id','source_id','source_quote'}<=set(item)
                or set(item)-{'citation_id','source_id','source_quote','context_ref'}):
            fail('market_citation_schema_invalid',index)
        ident=item.get('citation_id');source_id=item.get('source_id');quote=item.get('source_quote')
        if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}',ident):
            fail('market_citation_id_invalid',index)
        elif ids.count(ident)!=1:fail('market_citation_id_duplicate',index)
        if (item.get('context_ref') is not None
                and (not isinstance(item['context_ref'],str) or not context_id or item['context_ref']!=context_id)):
            fail('market_citation_context_binding_invalid',index)
        source=sources.get(source_id) if isinstance(source_id,str) and source_id in _MARKET_SOURCES else None
        if (not isinstance(source,dict) or source.get('kind')!=_MARKET_SOURCES.get(source_id,('',None))[1]
                or not isinstance(source.get('text'),str) or not source['text'].strip()):
            fail('market_citation_source_invalid',index);source=None
        if source is not None:
            source_hash=hashlib.sha256(source['text'].encode()).hexdigest()
            if source.get('source_sha256')!=source_hash:fail('market_citation_source_binding_invalid',index)
        if not isinstance(quote,str) or not quote.strip():fail('market_citation_quote_invalid',index)
        elif source is not None and quote not in source['text']:fail('market_citation_quote_not_in_source',index)
        if isinstance(quote,str) and (_ACCOUNT_METRIC.search(quote) or _PNL_METRIC.search(quote) or _CITATION_ACCOUNT_SCOPE.search(quote)):
            fail('market_citation_account_scope_forbidden',index)
        if len(errors)==before:
            rows.append({'citation_id':ident,'source_id':source_id,'source_quote':quote,'source_kind':source['kind'],
                         'source_sha256':source_hash,'quote_sha256':hashlib.sha256(quote.encode()).hexdigest(),
                         'context_ref':context_id,'scope':'quoted_market_report_not_verified_truth'})
    return {'schema':'openinvest.market_citation_validation.v1','status':'invalid' if errors else 'bound' if rows else 'not_requested',
            'context_ref':context_id,'citations':rows,'errors':errors,'source_validity_changed':False,
            'native_position_quote_qualification_granted':False,'trading_or_learning_permission_granted':False}


def render_market_citations(validation):
    """Display only bound reports, separately from advice and account math."""
    receipt=validation.get('market_citation_validation') or {}
    if receipt.get('status')!='bound':return ''
    from openinvest.capabilities.committee.i18n import bilingual
    lines=['\n\n### '+bilingual('来源报道（仅核对原文存在）','Source reports (quotation existence only)'),
           bilingual('以下为输入来源的逐字报道，不证明其真实、时效或独立性；不是持仓原生报价、账户计算、交易或学习许可。',
                     'These are verbatim input reports, not verified truth, freshness or independent evidence. They are not native position quotes, account calculations, trade authority or learning permission.')]
    def escape(text):return re.sub(r'([\\`*_{}\[\]<>~|$])',r'\\\1',text)
    for row in receipt.get('citations') or []:
        lines.append(f"- [{row['citation_id']}] {row['source_id']} ({row['source_kind']})")
        lines.extend('  > '+escape(line) for line in row['source_quote'].splitlines())
    return '\n'.join(lines)


def _text_shape(value):
    return value is None or isinstance(value,str) or (isinstance(value,list) and all(isinstance(item,str) for item in value))


def _has_numeric_value(text):
    # "Opportunity cost is not zero" is qualitative; it does not assert a
    # zero account value. Keep digits, money and positive zero assertions.
    presentation=_NEGATED_WORD_ZERO.sub(lambda m:' '*len(m.group(0)),text)
    return _NUMERIC.search(presentation) is not None


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


def _reference_groups(value,path):
    """Keep multiline/list value relationships together, before clause splitting."""
    if isinstance(value,str):yield path,value
    elif isinstance(value,list) and all(isinstance(item,str) for item in value):yield path,'\n'.join(value)
    elif isinstance(value,dict):
        for key,item in value.items():yield from _reference_groups(item,path+'.'+str(key))
    elif isinstance(value,list):
        for index,item in enumerate(value):yield from _reference_groups(item,path+'.'+str(index))


def _account_citation_value(text, identifiers):
    labels=list(_REFERENCE_FINANCIAL_LABEL.finditer(text))
    for ident in identifiers:
        for reference in re.finditer(r'(?<![A-Za-z0-9_-])'+re.escape(ident)+r'(?![A-Za-z0-9_-])',text,re.I):
            for label in labels:
                if label.end()<=reference.start():between=text[label.end():reference.start()]
                elif reference.end()<=label.start():between=text[reference.end():label.start()]
                else:continue  # A financial word inside the ID is not an account assertion.
                if _VALUE_LINK.fullmatch(between):return True
    return False


def _market_price_reference_text(text, reference):
    """Recognize a supplied market mark, never a coincident account amount."""
    reference=reference or {}
    if (reference.get('status')!='qualified' or not number(reference.get('price')) or reference['price']<=0 or not reference.get('source_ref')
            or not reference.get('asof') or reference.get('scope')!='market_snapshot_not_position_quote'):
        return text
    if _ACCOUNT_METRIC.search(text) or _PNL_METRIC.search(text) or re.search(r'银行|账户|你的|我的|bank|account',text,re.I):
        return text
    aliases={'CNY':('CNY','人民币','元'),'USD':('USD','美元'),'AUD':('AUD','澳元'),
             'HKD':('HKD','港元'),'JPY':('JPY','日元'),'EUR':('EUR','欧元'),'GBP':('GBP','英镑')}
    currency=reference.get('currency');names=aliases.get(currency,(currency,))
    names=[re.escape(name) for name in names if isinstance(name,str)]
    if not names:return text
    marker=re.compile(r'(?:现价|市场价格|行情快照价格|current[ -]price|market[ -]price)\s*(?:约|为|是|about|=|:|：)?\s*$',re.I)
    pattern=re.compile(r'(?P<value>'+_NUMBER+r')\s*(?P<currency>'+('|'.join(names))+
        r')(?:(?:/|每)\s*(?P<unit>[A-Za-z_\u4e00-\u9fff]+))?(?!\s*(?:/|每))')
    def replace(match):
        if not marker.search(text[:match.start()]):return match.group(0)
        try:value=float(match['value'].replace(',',''))
        except ValueError:return match.group(0)
        # Only the original mark or its sealed, shared source display can
        # round-trip. This is not an arbitrary price-rounding tolerance.
        from openinvest.calc.timeframe_analysis import display_market_price
        literal=reference.get('display_price_literal')
        display_matches=(isinstance(literal,str) and literal==display_market_price(reference['price'])
                         and value==float(literal))
        if value<=0 or (value!=reference['price'] and not display_matches):return match.group(0)
        if match['unit'] and canonical_unit(match['unit'])!=canonical_unit(reference.get('unit')):return match.group(0)
        return 'MARKET_QUOTE_REFERENCE'
    return pattern.sub(replace,text)


def untyped_financial_statements(memo, fields=None, *, currencies=(), market_reference=None, market_sources=None, context_id=None):
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
    exempt={};bound_citation_ids=set()
    def bind_citations(value,path):
        receipt=validate_market_citations(value,market_sources=market_sources,context_id=context_id)
        structure.extend(receipt['errors'])
        # A malformed collection grants no exemptions, including duplicate IDs.
        if not receipt['errors']:
            for i,item in enumerate(value):
                exempt[f'{path}.{i}.source_quote']=item['source_quote']
                bound_citation_ids.add(item['citation_id'])
    if 'market_citations' in fields:bind_citations(fields['market_citations'],'fields.market_citations')
    fragments=list(_string_leaves(fields,'fields'))
    for line in str(memo or '').splitlines():
        matched=_PROTOCOL.match(line)
        if matched:
            value=line[matched.end():].strip().removesuffix('**').strip()
            header=line[:matched.end()].replace('**','').strip().rstrip(':').upper()
            if header.endswith('_JSON'):
                name=header[:-5].lower()
                try:
                    parsed=strict_cio_json(value)
                    structure.extend(narrative_schema_errors({name:parsed}))
                    if name=='market_citations':bind_citations(parsed,'protocol.market_citations')
                    fragments.extend(_string_leaves(parsed,'protocol.'+name))
                    continue
                except (ValueError,TypeError):
                    if name=='market_citations':structure.append({'code':'invalid_protocol_json','path':name})
            elif re.fullmatch(r'[A-Za-z_]+|N/A|null|[-+]?\d+(?:\.\d+)?',value):
                continue
        fragments.append(('memo',line))
    clauses=[(path,part.strip()) for path,text in fragments if exempt.get(path)!=text
             for part in re.split(r'[，；。！？;!?\n]|(?<!\d),(?!\d)|(?<!\d)\.(?!\d)',text) if part.strip()]
    clauses=[(path,_market_price_reference_text(text,market_reference)) for path,text in clauses]
    groups=[('memo',str(memo or '')),*_reference_groups(fields,'fields')]
    for path,text in groups:
        if path not in exempt and _account_citation_value(text,bound_citation_ids):
            structure.append({'code':'market_citation_account_reference_forbidden','path':path,
                              'claim_quote':text,'reason':'market_report_reference_cannot_supply_an_account_value'})
    native=[re.escape(c) for c in currencies if isinstance(c,str) and re.fullmatch('[A-Z]{3}',c)]
    native_money=(re.compile(r'(?:'+('|'.join(native))+r')\s*'+_NUMBER+r'|'+_NUMBER+r'\s*(?:'+('|'.join(native))+r')') if native else None)
    return structure+[{'code':'untyped_financial_narrative','path':path,'claim_sha256':hashlib.sha256(text.encode()).hexdigest(),
             'claim_quote':text,'reason':'financial_quantities_require_typed_source_or_backend_calculation'}
            for path,text in clauses if _MONEY.search(text) or (native_money is not None and native_money.search(text))
            or any(_has_numeric_value(text[m.end():]) for m in _ACCOUNT_METRIC.finditer(text))
            or (_PNL_METRIC.search(text) and _has_numeric_value(text))]


BASES={'current_market_value':'quantity_times_current_mark','scenario_market_value':'quantity_times_scenario_mark',
       'mark_to_market_change':'scenario_mark_minus_current_mark','current_unrealized_pnl':'current_mark_minus_cost',
       'scenario_unrealized_pnl':'scenario_mark_minus_cost','realized_pnl_change':'assumed_no_trade_increment',
       'realized_pnl_total':'execution_history_required','maximum_drawdown':'price_path_required'}


def validate_scenarios(position, requests, *, memo='', context_id=None, fields=None, market_reference=None, market_sources=None):
    supplied=position if isinstance(position,dict) else {}
    currencies=[(supplied.get('quote') or {}).get('currency'),(supplied.get('cost') or {}).get('currency')]
    errors=untyped_financial_statements(memo,{**(fields or {}),'scenario_analysis':requests},currencies=currencies,
        market_reference=market_reference,market_sources=market_sources,context_id=context_id);results=[]
    citations=(fields or {}).get('market_citations',[])
    if 'market_citations' not in (fields or {}):
        # Primitive callers can provide the same text protocol as Coordinator.
        # Never make an unparsed/duplicate collection displayable.
        lines=re.findall(r'^\s*(?:\*\*)?MARKET_CITATIONS_JSON(?:\*\*)?\s*:\s*(.+)$',str(memo),re.M|re.I)
        if len(lines)>1:citations={'duplicate_protocol':True}
        elif lines:
            from openinvest.core.committee.cio_parse import strict_cio_json
            try:citations=strict_cio_json(lines[0].removesuffix('**').strip())
            except ValueError:citations={'parse_error':True}
    citation_validation=validate_market_citations(citations,market_sources=market_sources,context_id=context_id)
    for error in citation_validation['errors']:
        if error not in errors:errors.append(error)
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
        if isinstance(unit,str) and '/' in unit:
            prefix,denominator=unit.split('/',1)
            if prefix!=currency:row_errors.append('scenario_unit_currency_prefix_mismatch')
            unit=denominator
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
            'position_ref':expected_ref,'results':results,'errors':errors,'market_citation_validation':citation_validation,
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
        return original+render_scenarios(validation)+render_market_citations(validation)
    status=verdict.get('decision_status','REVIEW_REQUIRED')
    raw=(verdict.get('_raw_proposal') or {}).get('verdict') or verdict.get('raw_market_verdict')
    if raw not in {'BUY','ACCUMULATE','HOLD','TRIM','SELL'}:raw='UNCLEAR'
    text=bilingual(
        f"[QUALITY_ALERT] {status}：当前没有已核验的执行建议。原始市场判断：{raw}（未通过完整资格核验）。\n"
        "执行金额为零；未核验的财务数字不进入正式报告，完整原文已封存供审计。",
        f"[QUALITY_ALERT] {status}: no qualified executable advice. Original market proposal: {raw} (unqualified).\n"
        "Executable allocation is zero. Unchecked financial prose is withheld from the report; original bytes remain in audit evidence.")
    return text+(render_scenarios(validation)+render_market_citations(validation) if status!='DATA_UNTRUSTED' else '')
