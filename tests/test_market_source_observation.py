"""Source attempts, provider session proof and reference-only values."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import pandas as pd
import pytest

from openinvest.utils import market_observation as evidence


NOW = datetime(2026, 11, 9, 15, 5, tzinfo=timezone.utc)


def metadata(now=NOW, *, closed=False):
    start = now.replace(hour=9,minute=0,second=0,microsecond=0)
    end = now.replace(hour=16,minute=0,second=0,microsecond=0)
    return {"exchangeTimezoneName":"UTC","instrumentType":"ETF","currency":"USD",
        "regularMarketTime":int((end if closed else now-timedelta(minutes=5)).timestamp()),
        "marketState":"CLOSED" if closed else "REGULAR",
        "currentTradingPeriod":{"regular":{"start":int(start.timestamp()),"end":int(end.timestamp())}}}


def frame(now=NOW):
    return pd.DataFrame({"Close":[100.0,99.0,98.0],"High":[101.0,100.0,99.0],"Low":[99.0,98.0,97.0]},
        index=pd.date_range(now.date()-timedelta(days=2),periods=3))


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(evidence,"observation_now",lambda:NOW)
    return NOW


def test_provider_end_certifies_a_close_but_no_period_evidence_never_guesses_host_today():
    rows = frame()
    unknown = evidence.history_observation(rows,metadata={},now=NOW)
    assert unknown["completed_session_dates"] == [] and unknown["quote_fresh"] is False
    close_time = NOW.replace(hour=16,minute=5)
    closed = evidence.history_observation(rows,metadata=metadata(closed=True),now=close_time)
    assert closed["latest_bar_completed"] is True and closed["daily_alert_eligible"] is True
    assert closed["quote_fresh"] is False and closed["market_state"] == "closed"
    assert closed["completion_ends"][NOW.date().isoformat()] == NOW.replace(hour=16,minute=0).isoformat()


@pytest.mark.parametrize('closed',[False,True])
def test_real_yfinance_metadata_mapping_does_not_request_lazy_trading_periods(closed):
    from yfinance.scrapers.history import HistoryMetadata
    current = NOW.replace(hour=16,minute=5) if closed else NOW
    values = {**metadata(current,closed=closed),'symbol':'SYNTH'}
    def unexpected_network():
        pytest.fail('metadata conversion requested the lazy intraday network fetch')
    mapping = HistoryMetadata(SimpleNamespace(_history_metadata=values,
        _load_history_metadata_trading_periods=unexpected_network))
    captured = evidence.provider_metadata(SimpleNamespace(history_metadata=mapping))
    assert captured['currency'] == 'USD' and captured['symbol'] == 'SYNTH'
    assert 'tradingPeriods' not in captured
    observed = evidence.history_observation(frame(),metadata=mapping,now=current)
    assert observed['quote_fresh'] is (not closed)
    assert observed['daily_alert_eligible'] is closed


def test_a_cached_intraday_daily_bar_does_not_become_complete_as_the_wall_clock_passes_close():
    result = evidence.history_observation(frame(),metadata=metadata(),now=NOW.replace(hour=17),
        received_at=NOW,fetch_status="cached")
    assert NOW.date().isoformat() not in result["completed_session_dates"]
    assert result["daily_alert_eligible"] is False and result["quote_fresh"] is False


def test_freshness_uses_provider_quote_time_and_cannot_accept_future_time(clock):
    rows = frame()
    rows.attrs["source_observation"] = evidence.history_observation(rows,metadata=metadata(),now=NOW)
    assert evidence.frame_observation(rows)["quote_fresh"] is True
    future = metadata()
    future["regularMarketTime"] = int((NOW+timedelta(hours=1)).timestamp())
    assert evidence.history_observation(rows,metadata=future,now=NOW)["quote_fresh"] is False
    rows.attrs["yf_fetch_failed"] = True
    assert evidence.frame_observation(rows)["quote_fresh"] is False


def test_failed_today_refresh_keeps_the_success_clock_and_retry_throttle(tmp_path,monkeypatch,clock):
    from openinvest.db.market_store import MarketStore
    from openinvest.utils import exchange_fee as source
    monkeypatch.setattr('openinvest.db.market_store.DB_PATH',str(tmp_path/'market.sqlite'))
    store = MarketStore()
    monkeypatch.setattr(source,"_STORE",store)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)
    monkeypatch.setattr(source,"datetime",Clock)
    store.save_generic_price("SYNTH",NOW.date().isoformat(),98.0,high=99.0,low=97.0)
    store.mark_fetched("SUCCESS:yfinance:SYNTH")
    before = store.conn.execute("SELECT fetched_at FROM fetch_log WHERE symbol='SUCCESS:yfinance:SYNTH'").fetchone()[0]
    calls = []
    def unavailable(*args,**kwargs):
        calls.append(True)
        raise ConnectionError("synthetic provider failure")
    monkeypatch.setattr(source.yf,"Ticker",lambda symbol:SimpleNamespace(history=unavailable))
    monkeypatch.setattr(source,"_betashares_fallback",lambda symbol:False)
    monkeypatch.setattr(source,"_equibles_fallback",lambda *args:False)
    first = source.get_history_data("SYNTH")
    second = source.get_history_data("SYNTH")
    assert len(calls) == 1
    assert first.attrs["yf_fetch_failed"] is True and second.attrs["yf_fetch_failed"] is True
    assert evidence.frame_observation(second)["quote_fresh"] is False
    assert store.conn.execute("SELECT fetched_at FROM fetch_log WHERE symbol='SUCCESS:yfinance:SYNTH'").fetchone()[0] == before
    assert store.conn.execute("SELECT 1 FROM fetch_log WHERE symbol='ATTEMPT:yfinance:SYNTH'").fetchone()
    assert not store.conn.execute("SELECT 1 FROM fetch_log WHERE symbol='SYNTH'").fetchone()
    store.conn.close()


def test_successful_selected_alternative_is_not_called_a_failed_yahoo_quote(tmp_path,monkeypatch,clock):
    from openinvest.db.market_store import MarketStore
    from openinvest.utils import exchange_fee as source
    monkeypatch.setattr('openinvest.db.market_store.DB_PATH',str(tmp_path/'market.sqlite'))
    store = MarketStore()
    monkeypatch.setattr(source,"_STORE",store)
    def unavailable(*args,**kwargs):
        raise ConnectionError("synthetic")
    monkeypatch.setattr(source.yf,"Ticker",lambda symbol:SimpleNamespace(history=unavailable))
    monkeypatch.setattr(source,"_betashares_fallback",lambda symbol:False)
    def alternative(symbol,*args):
        store.save_generic_price(symbol,NOW.date().isoformat(),98.0,source="equibles")
        store.mark_fetched("SUCCESS:equibles:"+symbol)
        return True
    monkeypatch.setattr(source,"_equibles_fallback",alternative)
    result = source.get_history_data("SYNTH")
    assert result.attrs["yf_fetch_failed"] is False
    observed = evidence.frame_observation(result)
    assert observed["provider"] == "equibles" and observed["fetch_status"] == "ok"
    assert observed["quote_fresh"] is False  # A date-only close is still not a new live quote.
    store.conn.close()


def test_actual_history_entry_records_the_post_fetch_clock_and_reuses_qualified_cache(tmp_path,monkeypatch):
    from openinvest.db.market_store import MarketStore
    from openinvest.utils import exchange_fee as source,quotes
    from yfinance.scrapers.history import HistoryMetadata
    monkeypatch.setattr('openinvest.db.market_store.DB_PATH',str(tmp_path/'market.sqlite'))
    store = MarketStore()
    monkeypatch.setattr(source,'_STORE',store)
    state = SimpleNamespace(now=NOW,calls=0)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return state.now if tz is not None else state.now.replace(tzinfo=None)
    monkeypatch.setattr(source,'datetime',Clock)
    monkeypatch.setattr(evidence,'observation_now',lambda:state.now)
    meta = metadata()
    meta['symbol'] = 'SYNTH'
    meta['regularMarketTime'] = int((NOW+timedelta(seconds=1)).timestamp())
    def fetched(**kwargs):
        state.calls += 1
        state.now = NOW+timedelta(seconds=2)
        return frame()
    def unexpected_lazy_fetch():
        pytest.fail('native quote metadata triggered additional provider IO')
    native_metadata = HistoryMetadata(SimpleNamespace(_history_metadata=meta,
        _load_history_metadata_trading_periods=unexpected_lazy_fetch))
    monkeypatch.setattr(source.yf,'Ticker',lambda symbol:SimpleNamespace(history=fetched,history_metadata=native_metadata))
    first = source.get_history_data('SYNTH')
    second = source.get_history_data('SYNTH')
    assert state.calls == 1
    for observed in (evidence.frame_observation(first),evidence.frame_observation(second)):
        assert observed['provider'] == 'yfinance' and observed['quote_fresh'] is True
        assert observed['quote_asof'] == (NOW+timedelta(seconds=1)).isoformat()
        assert observed['received_at'] == (NOW+timedelta(seconds=2)).isoformat()
        assert observed['history_basis_qualified'] is True
        assert observed['last_attempt_at'] and observed['last_successful_fetch_at']
        assert observed['currency'] == 'USD' and observed['instrument_type'] == 'ETF'
    quote = quotes.get_quote({'symbol':'SYNTH'})
    assert quote.currency == 'USD' and quote.unit == 'share' and quote.is_stale is False
    assert state.calls == 1
    store.conn.close()


def test_an_alternative_response_without_valid_prices_is_only_an_attempt(tmp_path,monkeypatch):
    from openinvest.db.market_store import MarketStore
    from openinvest.utils import exchange_fee as source
    monkeypatch.setattr('openinvest.db.market_store.DB_PATH',str(tmp_path/'market.sqlite'))
    store = MarketStore()
    monkeypatch.setattr(source,'_STORE',store)
    monkeypatch.setenv('EQUIBLES_API_KEY','synthetic-offline-only')
    rows = frame()
    rows['Close'] = [float('nan'),float('inf'),-1.0]
    monkeypatch.setattr('openinvest.utils.equibles.to_mic',lambda symbol:'SYNTH')
    monkeypatch.setattr('openinvest.utils.equibles.fetch_daily_prices',lambda *args:rows)
    assert source._equibles_fallback('SYNTH',pd.DataFrame(),NOW.date().isoformat()) is False
    assert store.conn.execute("SELECT 1 FROM fetch_log WHERE symbol='ATTEMPT:equibles:SYNTH'").fetchone()
    assert not store.conn.execute("SELECT 1 FROM fetch_log WHERE symbol='SUCCESS:equibles:SYNTH'").fetchone()
    assert store.get_history_df('SYNTH').empty
    store.conn.close()


def test_old_vix_reference_cannot_turn_current_defense_on_or_off(monkeypatch,clock):
    from openinvest.utils import sentiment
    from openinvest.utils import exchange_fee
    rows = pd.DataFrame({"Close":list(range(10,70))},index=pd.date_range("2026-01-01",periods=60))
    monkeypatch.setattr(exchange_fee,"get_history_data",lambda *args:rows)
    report = sentiment.build_sentiment_brief(cnn_enabled=False)
    assert "FEAR_GREED_REFERENCE:" in report and "INDEP_DEFENSE_FLAG: unknown" in report
    assert "INDEP_DEFENSE_FLAG: on" not in report and "INDEP_DEFENSE_FLAG: off" not in report
    assert report.input_health["valid"] is False


def test_gold_needs_both_leg_times_and_remains_a_reference(monkeypatch,clock):
    from openinvest.utils import gold_price,quotes
    rows = frame()
    monkeypatch.setattr(gold_price,"observation_now",lambda:NOW)
    monkeypatch.setattr(quotes,"observation_now",lambda:NOW)
    monkeypatch.setattr(gold_price.yf,"Ticker",lambda symbol:SimpleNamespace(history=lambda **kwargs:rows,history_metadata={}))
    snap = gold_price.get_gold_snapshot(0)
    assert snap.is_stale is True and snap.gold_asof is None and snap.fx_asof is None
    monkeypatch.setattr(quotes,"get_gold_snapshot",lambda **kwargs:snap)
    quote = quotes.get_quote({"symbol":"GC=F","proxy_kind":"gold_cny_per_gram","cost_currency":"CNY"})
    assert quote.is_stale is True and quote.is_reference is True and quote.extra["bank_quote_observed"] is False
    assert quote.source_observation["quote_fresh"] is False
    text = gold_price.format_gold_report(snap)
    assert "COMEX" in text and "非银行牌价" in text and "伦敦金现货" not in text


@pytest.mark.parametrize('bad_fx_time',[False,True])
def test_dated_gold_legs_can_be_fresh_reference_but_never_an_observed_bank_quote(monkeypatch,clock,bad_fx_time):
    from openinvest.utils import gold_price,quotes
    gold, fx = frame(), frame()
    gold['Close'] = [2000.0,2001.0,2002.0]
    fx['Close'] = [7.0,7.1,7.2]
    fx_meta = metadata()
    fx_meta['currency'] = 'CNY'
    if bad_fx_time:
        fx_meta['regularMarketTime'] = int((NOW+timedelta(hours=1)).timestamp())
    monkeypatch.setattr(gold_price,'observation_now',lambda:NOW)
    monkeypatch.setattr(quotes,'observation_now',lambda:NOW)
    monkeypatch.setattr(gold_price.yf,'Ticker',lambda symbol:SimpleNamespace(
        history=lambda **kwargs:gold if symbol=='GC=F' else fx,
        history_metadata=metadata() if symbol=='GC=F' else fx_meta))
    snap = gold_price.get_gold_snapshot(0)
    assert snap.is_stale is bad_fx_time
    monkeypatch.setattr(quotes,'get_gold_snapshot',lambda **kwargs:snap)
    quote = quotes.get_quote({'symbol':'GC=F','proxy_kind':'gold_cny_per_gram'})
    assert quote.is_stale is bad_fx_time and quote.is_reference is True
    assert quote.source_observation['quote_fresh'] is False and quote.extra['bank_quote_observed'] is False


def test_tip_price_changes_are_not_deterministic_real_yield_or_gold_direction(monkeypatch,clock):
    from openinvest.utils import exchange_fee
    rows = frame(datetime.now(timezone.utc))
    monkeypatch.setattr(exchange_fee,"get_history_data",lambda *args,**kwargs:rows)
    text = exchange_fee.get_macro_data()
    assert "TIPS bond ETF price (TIP" in text
    assert "does not establish" in text and "real yields rising" not in text and "gold tailwind" not in text


def test_mixed_history_cannot_supply_day_returns_but_a_native_absolute_quote_remains_valid(monkeypatch,clock):
    from openinvest.utils import quotes,exchange_fee
    from openinvest.services import price_alerts
    rows = frame()
    rows.attrs['source_observation'] = {**evidence.history_observation(rows,metadata=metadata(),now=NOW),
                                      'history_basis_qualified':False}
    monkeypatch.setattr(quotes,'get_history_data',lambda *args:rows)
    monkeypatch.setattr(exchange_fee,'get_history_data',lambda *args:rows)
    quote = quotes.get_quote({'symbol':'SYNTH','cost_currency':'USD'})
    assert quote.is_stale is False and quote.is_reference is False
    assert quote.extra['day_change_pct'] is None and quote.extra['prev_close'] is None
    result = price_alerts.evaluate_symbol('SYNTH',{'price_below':99,'day_drop_pct':1},None,
        price=quote.price,extra=quote.extra,last_updated=quote.last_updated)
    assert result[0]['breached'] is True and result[1]['current'] is None
    assert price_alerts._day_change('SYNTH',None,{},quote.last_updated) == (None,None)


def test_mixed_history_has_no_macro_monthly_return_or_current_vix_defense(monkeypatch,clock):
    from openinvest.utils import exchange_fee,sentiment
    rows = pd.DataFrame({'Close':list(range(20,80))},index=pd.date_range(end=datetime.now(timezone.utc).date(),periods=60))
    rows.attrs['source_observation'] = {'quote_fresh':True,'history_basis_qualified':False}
    monkeypatch.setattr(exchange_fee,'get_history_data',lambda *args,**kwargs:rows)
    macro = exchange_fee.get_macro_data()
    assert macro.count('unknown (history scale unverified)') == 4
    assert 'TIPS bond ETF price (TIP, MoM): unknown' in macro
    brief = sentiment.build_sentiment_brief(cnn_enabled=False)
    assert 'INDEP_DEFENSE_FLAG: unknown' in brief and brief.input_health['valid'] is False


@pytest.mark.parametrize('response',['nan_empty','nan_error','missing','nan_closed','direct','rebuild','only_rebuild'])
def test_new_provider_metadata_cannot_date_an_old_unobserved_cache_price(tmp_path,monkeypatch,response):
    from openinvest.db.market_store import MarketStore
    from openinvest.utils import exchange_fee as source
    monkeypatch.setattr('openinvest.db.market_store.DB_PATH',str(tmp_path/'market.sqlite'))
    store = MarketStore()
    monkeypatch.setattr(source,'_STORE',store)
    current = NOW.replace(hour=16,minute=5) if response=='nan_closed' else NOW
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return current if tz is not None else current.replace(tzinfo=None)
    monkeypatch.setattr(source,'datetime',Clock)
    monkeypatch.setattr(evidence,'observation_now',lambda:current)
    cached = frame()
    for stamp,price in cached['Close'].items():
        store.save_generic_price('SYNTH',stamp.date().isoformat(),55.0 if stamp==cached.index[-1] else price,
                                 high=101.0,low=54.0,source='yfinance')
    incoming = frame()
    if response=='missing':
        incoming = incoming.iloc[:-1]
    elif response!='direct':
        incoming.iloc[-1,incoming.columns.get_loc('Close')] = float('nan')
        if response=='only_rebuild':
            incoming = incoming.iloc[-1:]
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs.get('interval','1d'))
        if kwargs.get('interval')=='60m':
            if response=='nan_error':
                raise ConnectionError('synthetic hourly provider failure')
            if response in {'rebuild','only_rebuild'}:
                return pd.DataFrame({'Close':[98.0],'High':[99.0],'Low':[97.0],'Volume':[10.0]},
                                    index=[pd.Timestamp(current-timedelta(minutes=5))])
            return pd.DataFrame()
        return incoming
    ticker = SimpleNamespace(history=fetch,history_metadata={**metadata(current,closed=response=='nan_closed'),'symbol':'SYNTH'})
    monkeypatch.setattr(source.yf,'Ticker',lambda symbol:ticker)
    monkeypatch.setattr(source,'_betashares_fallback',lambda *args:False)
    monkeypatch.setattr(source,'_equibles_fallback',lambda *args:False)
    result = source.get_history_data('SYNTH')
    observed = evidence.frame_observation(result)
    positive = response in {'direct','rebuild','only_rebuild'}
    assert result['Close'].iloc[-1] == (98.0 if positive else 55.0)
    assert observed['latest_price_observed'] is positive and observed['quote_fresh'] is positive
    if not positive:
        assert observed['daily_alert_eligible'] is False
        assert NOW.date().isoformat() not in observed['completed_session_dates']
    before = list(calls)
    again = evidence.frame_observation(source.get_history_data('SYNTH'))
    assert again['quote_fresh'] is positive and calls == before
    store.conn.close()


def test_repairing_an_older_hole_does_not_borrow_the_later_request_quote_time(tmp_path,monkeypatch):
    from openinvest.db.market_store import MarketStore
    from openinvest.utils import exchange_fee as source
    monkeypatch.setattr('openinvest.db.market_store.DB_PATH',str(tmp_path/'market.sqlite'))
    store = MarketStore()
    monkeypatch.setattr(source,'_STORE',store)
    state = SimpleNamespace(now=NOW)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return state.now if tz is not None else state.now.replace(tzinfo=None)
    monkeypatch.setattr(source,'datetime',Clock)
    monkeypatch.setattr(evidence,'observation_now',lambda:state.now)
    incoming = frame()
    incoming.iloc[0,incoming.columns.get_loc('Close')] = float('nan')
    ticker = SimpleNamespace(history_metadata={})
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs.get('interval','1d'))
        if kwargs.get('interval')=='60m':
            state.now = NOW+timedelta(minutes=10)
            ticker.history_metadata = {**metadata(state.now),'regularMarketTime':int((state.now-timedelta(seconds=1)).timestamp())}
            return pd.DataFrame({'Close':[100.0],'High':[101.0],'Low':[99.0],'Volume':[10.0]},
                index=[pd.Timestamp(NOW-timedelta(days=2))])
        state.now = NOW+timedelta(seconds=2)
        ticker.history_metadata = {**metadata(),'regularMarketTime':int((NOW+timedelta(seconds=1)).timestamp())}
        return incoming
    ticker.history = fetch
    monkeypatch.setattr(source.yf,'Ticker',lambda symbol:ticker)
    result = source.get_history_data('SYNTH')
    observed = evidence.frame_observation(result)
    assert calls == ['1d','60m'] and result['Close'].iloc[-1] == 98.0
    assert observed['latest_price_observed'] is True and observed['quote_fresh'] is True
    assert observed['quote_asof'] == (NOW+timedelta(seconds=1)).isoformat()
    assert observed['received_at'] == (NOW+timedelta(seconds=2)).isoformat()
    store.conn.close()


@pytest.mark.parametrize('reader',['second_store','subprocess'])
def test_shared_price_cache_restores_its_bound_source_without_another_fetch(tmp_path,monkeypatch,reader):
    from openinvest.db import market_store
    from openinvest.utils import exchange_fee as source,quotes
    path = tmp_path/'market.sqlite'
    monkeypatch.setattr(market_store,'DB_PATH',str(path))
    first, second = market_store.MarketStore(),market_store.MarketStore()
    monkeypatch.setattr(source,'_STORE',first)
    monkeypatch.setattr(evidence,'observation_now',lambda:NOW)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW if tz is not None else NOW.replace(tzinfo=None)
    monkeypatch.setattr(source,'datetime',Clock)
    calls = []
    def fetch(**kwargs):
        calls.append(True)
        return frame()
    monkeypatch.setattr(source.yf,'Ticker',lambda symbol:SimpleNamespace(history=fetch,history_metadata=metadata()))
    original = source.get_history_data('SYNTH').attrs['source_observation']
    if reader=='second_store':
        monkeypatch.setattr(source,'_STORE',second)
        quote = quotes.get_quote({'symbol':'SYNTH'})
        actual = {'currency':quote.currency,'unit':quote.unit,'stale':quote.is_stale,
                  'asof':quote.observed_at,'received':quote.source_observation['received_at']}
    else:
        import os,subprocess,sys
        code = '''
import json,sys
from datetime import datetime
from openinvest.db import market_store
from openinvest.utils import exchange_fee,market_observation,quotes
market_store.DB_PATH=sys.argv[1]
exchange_fee._STORE=market_store.MarketStore()
market_observation.observation_now=lambda:datetime.fromisoformat(sys.argv[2])
def forbidden(*args,**kwargs):raise AssertionError('cache reader attempted provider fetch')
exchange_fee.yf.Ticker=forbidden
q=quotes.get_quote({'symbol':'SYNTH'})
print(json.dumps({'currency':q.currency,'unit':q.unit,'stale':q.is_stale,'asof':q.observed_at,'received':q.source_observation['received_at']}))
'''
        result = subprocess.run([sys.executable,'-c',code,str(path),NOW.isoformat()],
            env={**os.environ,'INVEST_HOME':str(tmp_path/'child-home')},text=True,capture_output=True,check=True)
        actual = json.loads(result.stdout)
    assert actual == {'currency':'USD','unit':'share','stale':False,
                      'asof':original['quote_asof'],'received':original['received_at']}
    assert len(calls) == 1
    first.conn.close();second.conn.close()


@pytest.mark.parametrize('change',['price','high','broken_receipt','aged_receipt','new_same_price_receipt'])
def test_shared_source_cache_revalidates_content_time_and_newer_receipts(tmp_path,monkeypatch,change):
    from openinvest.db import market_store
    from openinvest.utils import exchange_fee as source
    import time
    monkeypatch.setattr(market_store,'DB_PATH',str(tmp_path/'market.sqlite'))
    first, second = market_store.MarketStore(),market_store.MarketStore()
    monkeypatch.setattr(source,'_STORE',first)
    state = SimpleNamespace(now=NOW)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return state.now if tz is not None else state.now.replace(tzinfo=None)
    monkeypatch.setattr(source,'datetime',Clock)
    monkeypatch.setattr(evidence,'observation_now',lambda:state.now)
    calls = []
    def fetch(**kwargs):
        calls.append(True)
        return frame()
    monkeypatch.setattr(source.yf,'Ticker',lambda symbol:SimpleNamespace(history=fetch,history_metadata=metadata(state.now)))
    original = source.get_history_data('SYNTH').attrs['source_observation']
    monkeypatch.setattr(source,'_STORE',second)
    assert source.get_history_data('SYNTH').attrs['source_observation']['quote_fresh'] is True
    if change in {'price','high'}:
        column = 'close' if change=='price' else 'high'
        with first.conn:
            first.conn.execute(f'UPDATE daily_prices SET {column}=123.0 WHERE symbol=? AND date=?',('SYNTH',NOW.date().isoformat()))
    elif change=='broken_receipt':
        # Preserve the original digest so the malformed bytes cannot qualify.
        with first.conn:first.conn.execute("UPDATE source_observation_cache SET payload='{}' WHERE symbol='SYNTH'")
        # Use a new instance rather than a previously cached in-process witness.
        second.conn.close();second = market_store.MarketStore()
        monkeypatch.setattr(source,'_STORE',second)
    elif change=='aged_receipt':
        state.now += timedelta(minutes=26)
    else:
        state.now += timedelta(minutes=31)
        with first.conn:first.conn.execute('UPDATE fetch_log SET fetched_at=?',(time.time()-source._TODAY_BAR_TTL_S-1,))
        monkeypatch.setattr(source,'_STORE',first)
        source.get_history_data('SYNTH')  # An allowed refresh with identical prices, newer source time.
        monkeypatch.setattr(source,'_STORE',second)
    observed = source.get_history_data('SYNTH').attrs['source_observation']
    if change=='new_same_price_receipt':
        assert observed['quote_fresh'] is True and observed['received_at'] == state.now.isoformat()
        assert len(calls) == 2
    else:
        assert observed['quote_fresh'] is False and len(calls) == 1
        if change=='aged_receipt':assert observed['received_at'] == original['received_at']
    first.conn.close();second.conn.close()
