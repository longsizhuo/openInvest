"""Daily completion remains useful without intraday data, without backfilled alarms."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.jobs import price_sentinel as sentinel
from openinvest.utils import market_observation
from tests.test_market_source_observation import metadata


@pytest.fixture
def context(tmp_path,monkeypatch):
    state = SimpleNamespace(now=datetime(2026,11,9,16,5,tzinfo=timezone.utc),intra=None,calls=[],daily=None)
    store = MemoryStore(tmp_path/"memory")
    store.write("strategy","strategy",{"target_assets":[]},"")
    class Clock:
        @staticmethod
        def now(tz=None):
            return state.now
        @staticmethod
        def fromisoformat(value):
            return datetime.fromisoformat(value)
    monkeypatch.setattr(sentinel,"datetime",Clock)
    monkeypatch.setattr(market_observation,"observation_now",lambda:state.now)
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",tmp_path/'memory')
    monkeypatch.setattr("openinvest.paths.INVEST_ROOT",tmp_path)
    monkeypatch.setattr("openinvest.jobs.event_watch._load_user_context",lambda:{"holdings":["SYNTHETIC"],"watching":[]})
    state.evaluate_user_lines = sentinel._check_user_alerts
    monkeypatch.setattr(sentinel,"_check_user_alerts",lambda *args:[])
    monkeypatch.setattr("openinvest.services.review_plans.update_plan",lambda *args,**kwargs:{})
    monkeypatch.setattr("openinvest.services.review_plans.queue_plan_reviews",lambda *args,**kwargs:None)
    monkeypatch.setattr("openinvest.services.review_plans.observe_quote",lambda *args,**kwargs:pytest.fail("completed close became a live plan quote"))
    ticker = SimpleNamespace(history=lambda **kwargs:state.intra,history_metadata=metadata(state.now,closed=True))
    monkeypatch.setattr("yfinance.Ticker",lambda symbol:ticker)
    monkeypatch.setattr("openinvest.utils.exchange_fee.get_history_data",lambda *args,**kwargs:state.daily)
    def queue(ms,event,*,key,**kwargs):
        state.calls.append({"event":event,"key":key,"options":kwargs})
        return {"notification_id":key,"accepted":False}
    monkeypatch.setattr(sentinel,"_queue_event",queue)
    state.store = store
    state.ticker = ticker
    return state


def daily(state,close,*,proof=True):
    prices = [100+(i%3)*.1 for i in range(300)] + [close]
    days = pd.date_range(end=state.now.date(),periods=len(prices))
    frame = pd.DataFrame({"Close":prices,"High":[p+1 for p in prices],"Low":[p-1 for p in prices]},index=days)
    frame.attrs["source_observation"] = market_observation.history_observation(frame,
        metadata=metadata(state.now,closed=True) if proof else {},now=state.now)
    state.daily = frame
    state.ticker.history_metadata = metadata(state.now,closed=True)


def test_intraday_empty_does_not_prevent_fetching_and_computing_completed_day_observation(context):
    daily(context,80)
    frames = sentinel._fetch_frames("SYNTHETIC")
    assert frames["intraday"]["status"] == "unavailable"
    assert frames["daily"]["status"] == "observed"
    assert frames["daily"]["risk_observations"]["observations"][0]["breached"] is True
    assert frames["daily"]["source_observation"]["quote_fresh"] is False


def test_first_complete_history_only_seeds_baseline_then_a_new_close_can_alert(context):
    daily(context,80)
    first = sentinel.run(dry_run=False)
    assert first["notifications_queued"] == 0 and context.calls == []
    assert first["coverage"]["SYNTHETIC"]["status"] == "observed_reference"
    context.now += timedelta(days=1)
    daily(context,70)
    second = sentinel.run(dry_run=False)
    assert second["notifications_queued"] == 1
    event = context.calls[0]["event"]
    witness = event['_market_witness']
    assert witness['kind'] == 'completed_session_return' and witness['quote_fresh'] is False
    assert witness['source_observation']['daily_alert_eligible'] is True
    assert witness['return_reference']['current_session_excluded'] is True
    assert witness['return_reference']['prior_max_session_date'] < context.now.date().isoformat()
    assert context.now.replace(hour=16,minute=0).isoformat() in event["one_line_claim"]
    assert "观察价" in event["one_line_claim"] and "预测" in event["one_line_claim"]
    sentinel.run(dry_run=False)
    assert len({call["key"] for call in context.calls}) == 1


def test_missing_completion_metadata_is_not_a_healthy_or_completed_observation(context):
    daily(context,80,proof=False)
    result = sentinel.run(dry_run=False)
    assert result["status"] == "coverage_limited"
    assert result["coverage"]["SYNTHETIC"]["daily"]["status"] == "reference_only"
    assert context.calls == []


def test_intraday_provider_failure_does_not_erase_the_independent_daily_component(context,monkeypatch):
    daily(context,80)
    def failed(**kwargs):
        raise ConnectionError("synthetic intraday outage")
    context.ticker.history = failed
    frames = sentinel._fetch_frames("SYNTHETIC")
    assert frames["intraday"]["status"] == "failed"
    assert frames["daily"]["status"] == "observed"
    assert frames["daily"]["risk_observations"]["observations"][0]["breached"] is True


def test_mixed_history_does_not_supply_atr_or_returns_to_a_valid_intraday_observation(context):
    daily(context,80)
    context.daily.attrs['source_observation']['history_basis_qualified'] = False
    context.intra = pd.DataFrame({'Close':[100.0,101.0,120.0]},index=pd.date_range(
        end=context.now-timedelta(minutes=5),periods=3,freq='5min'))
    frames = sentinel._fetch_frames('SYNTHETIC')
    assert frames['intraday']['status'] == 'observed' and frames['intraday']['atr_pct'] is None
    assert frames['intraday']['risk_observations']['observations'] == []
    assert frames['daily']['risk_observations']['observations'] == []


@pytest.mark.parametrize('kind',['native','true_future','derived_reference'])
def test_user_alert_witness_requires_native_time_and_uses_the_post_fetch_clock(context,monkeypatch,kind):
    start = context.now
    tick = start+timedelta(seconds=30 if kind=='true_future' else 1)
    source = {'provider':'synthetic_native_provider','quote_fresh':kind!='derived_reference',
        'quote_asof':tick.isoformat(),'bar_asof':tick.isoformat(),'received_at':(start+timedelta(seconds=2)).isoformat(),
        'completed_session_dates':[]}
    quote = SimpleNamespace(price=80.0,currency='USD',unit='share',is_stale=False,
        observed_at=tick.isoformat(),last_updated=start.date().isoformat(),extra={},
        is_reference=kind=='derived_reference',price_basis='adjusted_close',source_observation=source)
    def fetch(*args):
        context.now = start+timedelta(seconds=2)
        return quote
    monkeypatch.setattr('openinvest.services.price_alerts.fetch_quote',fetch)
    monkeypatch.setattr(sentinel,'_alert_targets',lambda:[{'symbol':'SYNTHETIC','alerts':{'price_below':90},'holding':None}])
    result = context.evaluate_user_lines(context.store,start,False)
    if kind == 'native':
        assert result == ['SYNTHETIC:price_below'] and len(context.calls) == 1
        witness = context.calls[0]['event']['_market_witness']
        assert witness['kind'] == 'native_quote_alert' and witness['quote_fresh'] is True
        assert witness['quote_asof'] == tick.isoformat() and witness['is_reference'] is False
    else:
        assert result == [] and context.calls == [] and result.coverage['SYNTHETIC']['status']=='reference_only'
