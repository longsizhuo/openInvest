"""Describe model output qualification separately from source validity/actions."""
from __future__ import annotations
import hashlib
import re

CONTRACT = 'risk_output_v1'
ENUMS = {'SIGNAL':{'ok','concerned','high_risk','unknown'},
         'MARKET_RISK':{'ordinary','elevated','unknown'},
         'LIQUIDITY':{'available','insufficient','unknown'}}


def risk_output_health(text, round_label):
    """Never correct a judgment or infer capacity from missing budget/cash."""
    from openinvest.core.committee.agent_io import worker_output_status
    raw=str(text or '')
    output_sha=hashlib.sha256(raw.encode()).hexdigest()
    fields={};missing=[];invalid=[]
    for name,values in ENUMS.items():
        source_name='ADJUSTED_SIGNAL' if name=='SIGNAL' and round_label!='RISK_R1' else name
        # Match complete fields, including ordinary Markdown emphasis. Quoted
        # prose mentioning a label is not a structured output field.
        matches=re.findall(r'^\s*(?:\*\*)?'+source_name+r'(?:\*\*)?:\s*(?:\*\*)?([A-Za-z_]+)',raw,re.M)
        if not matches:missing.append(source_name)
        elif len(matches)!=1 or matches[0].lower() not in values:invalid.append(source_name)
        else:fields[name]=matches[0].lower()
    unavailable=worker_output_status(text)['unavailable']
    structure='unavailable' if unavailable else 'invalid' if invalid else 'incomplete' if missing else 'complete'
    issue_id=hashlib.sha256((CONTRACT+':'+str(round_label)+':'+output_sha).encode()).hexdigest()
    return {'role':'risk','round':round_label,'output_sha256':output_sha,'contract_version':CONTRACT,
        'transport':worker_output_status(text)['transport'],'structure':structure,
        'missing_fields':missing,'invalid_fields':invalid,'fields':fields,
        'reasoning':'needs_reasoning_review','issue_refs':[] if structure in {'complete','unavailable'} else [issue_id],
        'source_validity_changed':False,'market_action_selected':False}
