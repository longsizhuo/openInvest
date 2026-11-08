import json
import numpy as np
import pandas as pd

from openinvest.calc.risk_context import comparative_risk_context
from openinvest.calc.timeframe_analysis import analyze_multi_timeframe
from openinvest.core.committee.cio_parse import parse_cio_memo
from openinvest.core.decision_evidence import enforce_input_health,advice_qualified


def history(scale=1.0):
    x=np.arange(200,dtype=float)
    close=(100+x*.03+np.sin(x/3))*scale
    return pd.DataFrame({'Close':close,'High':close*1.01,'Low':close*.99,'Volume':10000},index=pd.bdate_range('2025-01-01',periods=len(x)))


def test_comparison_excludes_the_observation_from_its_reference():
    frame=history();original=comparative_risk_context(frame)
    shock=frame.copy();shock.iloc[-1,shock.columns.get_loc('Close')]*=.8
    shock.iloc[-1,shock.columns.get_loc('Low')]=shock['Close'].iloc[-1]
    changed=comparative_risk_context(shock)
    assert changed['reference_last_bar']==str(frame.index[-2])
    assert changed['atr']['prior_median_pct']==original['atr']['prior_median_pct']
    for left,right in zip(original['returns']['observations'],changed['returns']['observations']):
        assert left['threshold_pct']==right['threshold_pct']
        assert right['return_pct']<left['return_pct']
    assert changed['market_risk_label'] is None and changed['account_loss_tolerance']=='not_inferred'


def test_reference_is_scale_invariant_and_does_not_emit_a_forecast_or_action():
    a=comparative_risk_context(history());b=comparative_risk_context(history(100))
    assert abs(a['atr']['ratio_to_prior_median']-b['atr']['ratio_to_prior_median'])<1e-9
    for x,y in zip(a['returns']['observations'],b['returns']['observations']):
        assert abs(x['return_pct']-y['return_pct'])<1e-9
        assert x['forecast_probability'] is None and not x['execution_authorized']
    assert 'verdict' not in a
    json.dumps(a,allow_nan=False)


def test_missing_reference_remains_unknown_and_formatter_carries_same_comparison():
    short=history().tail(8)
    assert comparative_risk_context(short)['status']=='insufficient_reference'
    text=analyze_multi_timeframe(history(),'Synthetic reference')
    line=next(s for s in text.splitlines() if s.startswith('PRIOR_SESSION_RISK_REFERENCE_JSON:'))
    assert json.loads(line.split(': ',1)[1])==comparative_risk_context(history())


def test_self_reported_confidence_does_not_select_an_action():
    for confidence in (.94,.95,.99,1.0):
        result=parse_cio_memo('',fields={'verdict':'BUY','confidence':confidence,'suggested_alloc_cny':1000},alloc_ceiling=10000)
        assert result['verdict']=='BUY' and result['alloc_cny']==1000
        assert result['confidence']==confidence
        if confidence>=.95:assert result['_legacy_confidence_rule_shadow']['applied'] is False


def test_positive_market_assessment_does_not_grant_missing_account_execution():
    view={'direction':'bullish','horizon':'one month','support':'supplied trend','counterevidence':'uncertain continuation','execution_data_gaps':'account not provided'}
    result=parse_cio_memo('',fields={'verdict':'BUY','confidence':.8,'suggested_alloc_cny':1000,'market_assessment':view})
    enforce_input_health(result,{'has_df':True,'macro_data_empty':False,'empty_inputs':['portfolio_summary']})
    assert result['alloc_cny']==0 and result['decision_status']=='DATA_UNTRUSTED'
    assert result['verdict']=='UNCLEAR' and result['raw_market_verdict']=='BUY'
    assert result['market_assessment']==view and result['_raw_proposal']['market_assessment']==view
    assert not advice_qualified(result)


def test_current_close_only_does_not_pretend_observed_intraday_range_or_normal_volatility():
    frame=history();frame.loc[frame.index[-1],['High','Low']]=float('nan')
    frame.loc[frame.index[-1],'Close']*=.8
    result=comparative_risk_context(frame)
    assert result['atr']['status']=='current_ohlc_unavailable'
    assert any(item['breached'] for item in result['returns']['observations'])
    assert result['market_risk_label'] is None


def test_mixed_history_basis_is_not_market_evidence():
    import pandas as pd
    from openinvest.calc.risk_context import comparative_risk_context
    from openinvest.calc.timeframe_analysis import analyze_multi_timeframe
    from openinvest.core.decision_evidence import enforce_input_health
    frame=pd.DataFrame({'Close':[100.,101.,1.02]},index=pd.date_range('2026-01-01',periods=3))
    frame.attrs['source_observation']={'history_basis_qualified':False,'history_basis':'mixed_reference'}
    assert comparative_risk_context(frame)['status']=='history_basis_unqualified'
    brief=analyze_multi_timeframe(frame,'test')
    assert 'RSI(14' not in brief and 'MA120 (Trend)' not in brief and 'Timeframe Performance' not in brief
    verdict={'verdict':'BUY','alloc_cny':1000}
    enforce_input_health(verdict,{'market_source_observation':frame.attrs['source_observation']})
    assert verdict['verdict']=='UNCLEAR' and verdict['alloc_cny']==0 and verdict['raw_market_verdict']=='BUY'
