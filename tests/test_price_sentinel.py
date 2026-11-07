"""价格异动哨兵（jobs/price_sentinel.py，ADR-025）单测

守的核心行为：
1. 检测：10 分钟涨跌 vs 日 ATR% 归一化阈值；ATR 缺失退绝对兜底
2. 冷却：同 symbol 同方向静默期内不重复报；反方向不受限
3. **时序契约：报警邮件先于委员会触发**（用户需求"先报给我，再跑 committee"）
4. 委员会触发失败不影响已发出的报警（不抛、不回滚）
5. 数据停滞（闭市）跳过；disabled 短路
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openinvest.jobs import price_sentinel as ps
from openinvest.services.event_notifier import send_event_alert as _REAL_SEND


# ---------- 纯函数 ----------

class TestDetectMove:
    def test_fires_on_vertical_move(self):
        # 10 分钟 +1.77%（2026-07-02 黄金实测值），日 ATR 1.16% → ratio 1.5 ≥ 0.8
        hit = ps._detect_move([100.0, 100.5, 101.77], atr_pct=1.16, mult=0.8)
        assert hit is not None
        assert hit["direction"] == "up"
        assert hit["ratio"] == pytest.approx(1.77 / 1.16, rel=1e-2)

    def test_holds_below_threshold(self):
        assert ps._detect_move([100.0, 100.1, 100.3], atr_pct=1.16, mult=0.8) is None

    def test_down_direction(self):
        hit = ps._detect_move([100.0, 99.5, 98.2], atr_pct=1.16, mult=0.8)
        assert hit is not None and hit["direction"] == "down"

    def test_atr_missing_falls_back_to_absolute(self):
        # ATR 缺失：1.2% ≥ 绝对兜底 1.0% → 触发；0.5% → 不触发
        assert ps._detect_move([100.0, 100.6, 101.2], atr_pct=None, mult=0.8) is None
        assert ps._detect_move([100.0, 100.2, 100.5], atr_pct=None, mult=0.8) is None

    def test_atr_missing_ratio_is_none_not_zero(self):
        """code review 发现：ratio 曾经用 0.0 当"ATR 缺失"哨兵值，和真实算出 0 倍
        混淆，且会让 run() 里的 severity 判断把巨幅异动错判成最低档。改用 None。"""
        hit = ps._detect_move([100.0, 100.6, 101.2], atr_pct=None, mult=0.8)
        assert hit is None

    def test_insufficient_bars(self):
        assert ps._detect_move([100.0, 101.0], atr_pct=1.0, mult=0.8) is None


class TestCooldown:
    NOW = datetime(2026, 7, 3, 2, 0, tzinfo=timezone.utc)

    def test_blocks_same_direction_within_window(self):
        state = {"GC=F:up": (self.NOW - timedelta(minutes=30)).isoformat()}
        assert ps._cooldown_ok(state, "GC=F", "up", self.NOW, 120) is False

    def test_allows_after_window(self):
        state = {"GC=F:up": (self.NOW - timedelta(minutes=130)).isoformat()}
        assert ps._cooldown_ok(state, "GC=F", "up", self.NOW, 120) is True

    def test_opposite_direction_not_blocked(self):
        """急涨后急跌是两回事，各自可报。"""
        state = {"GC=F:up": (self.NOW - timedelta(minutes=5)).isoformat()}
        assert ps._cooldown_ok(state, "GC=F", "down", self.NOW, 120) is True

    def test_garbage_state_treated_as_ok(self):
        assert ps._cooldown_ok({"GC=F:up": "not-a-date"}, "GC=F", "up", self.NOW, 120) is True


class TestLatestVerdict:
    def test_reads_newest_transcript(self, tmp_path):
        d = tmp_path / "2026-07-02"
        d.mkdir(parents=True)
        (d / "GC_F.md").write_text("# Committee\n\n**Verdict**: HOLD (confidence 0.65)\n")
        out = ps._latest_verdict("GC=F", committee_root=tmp_path)
        assert "HOLD" in out and "2026-07-02" in out

    def test_missing_transcript_graceful(self, tmp_path):
        assert "无委员会" in ps._latest_verdict("GC=F", committee_root=tmp_path)


# ---------- Producer -> durable outbox -> delivery -> analysis ----------

@pytest.fixture
def pipeline(tmp_path,monkeypatch):
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.config import set_config_override
    from openinvest.services.notification_outbox import NotificationOutbox
    from openinvest.jobs import notification_delivery
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",tmp_path/"memory")
    monkeypatch.setattr("openinvest.paths.INVEST_ROOT",tmp_path)
    monkeypatch.setenv("INVEST_REASONING_BY","backend")
    monkeypatch.setenv("INVEST_EVENT_ALERT","1")
    set_config_override({"event":{"risk_alerts_enabled":True}})
    env=SimpleNamespace(store=MemoryStore(),now=datetime.now(timezone.utc),calls=[],symbols=["GC=F"],targets=["GC=F"],lines=[])
    env.frames={"closes":[100.0,100.5,101.77],"price":101.77,"atr_pct":1.16,"last_bar_utc":env.now-timedelta(minutes=5)}
    env.quote=None
    class Clock:
        @staticmethod
        def now(tz=None):return env.now
    monkeypatch.setattr(ps,"datetime",Clock)
    monkeypatch.setattr(ps,"_fetch_frames",lambda symbol:env.frames)
    monkeypatch.setattr(ps,"_alert_targets",lambda:env.lines)
    monkeypatch.setattr("openinvest.services.price_alerts.fetch_quote",lambda *a,**kw:env.quote)
    monkeypatch.setattr("openinvest.jobs.event_watch._load_user_context",lambda:{"holdings":env.symbols,"watching":env.targets})
    def sender(channel,payload):
        env.calls.append((channel,payload));return True
    env.sender=sender
    monkeypatch.setattr("openinvest.services.notification_outbox.send_channel",lambda channel,payload:env.sender(channel,payload))
    monkeypatch.setattr("openinvest.services.analysis_dispatch.current_targets",lambda store:set(env.targets))
    env.trigger=MagicMock(side_effect=lambda *a,**kw:env.calls.append(("analysis",kw)) or "task-123")
    monkeypatch.setattr("openinvest.services.event_trigger._trigger_committee",env.trigger)
    env.deliver=lambda:notification_delivery.run(store=env.store,now=env.now)
    env.box=lambda:NotificationOutbox(env.store)
    return env


def test_scan_is_durable_and_nonblocking_then_delivery_precedes_analysis(pipeline):
    scan=ps.run(dry_run=False)
    assert scan["detected"]==1 and scan["notifications_queued"]==1 and scan["alerted"]==0
    assert pipeline.calls==[]
    delivered=pipeline.deliver()
    assert delivered["accepted_notifications"]==1 and delivered["accepted_channels"]==2
    assert [c[0] for c in pipeline.calls]==["discord","email","analysis"]
    scan2=ps.run(dry_run=False)
    assert scan2["detected"]==0
    pipeline.deliver()
    assert len(pipeline.calls)==3


def test_holdings_only_asset_is_not_sent_to_committee(pipeline):
    pipeline.targets=[]
    ps.run(dry_run=False);pipeline.deliver()
    assert [c[0] for c in pipeline.calls]==["discord","email"]
    pipeline.trigger.assert_not_called()


def test_analysis_failure_cannot_erase_accepted_alert(pipeline):
    pipeline.trigger.side_effect=RuntimeError("synthetic analysis outage")
    ps.run(dry_run=False)
    result=pipeline.deliver()
    assert result["accepted_notifications"]==1
    assert all(c[0] in ("discord","email") for c in pipeline.calls)
    with pipeline.box().connection() as db:
        assert db.execute("SELECT status FROM followups").fetchone()[0]=="unknown"
    pipeline.deliver()
    assert len(pipeline.calls)==2


def test_reference_market_is_observed_before_etf_open_without_using_its_quote(pipeline,monkeypatch):
    from openinvest.db.event_store import EventStore
    pipeline.symbols=["NDQ.AX","QQQ"];pipeline.targets=list(pipeline.symbols)
    frames={**pipeline.frames,"closes":[100,99,98],"price":98}
    monkeypatch.setattr(ps,"_fetch_frames",lambda sym:frames if sym=="^NDX" else None)
    quote_observer=MagicMock()
    monkeypatch.setattr("openinvest.services.review_plans.observe_quote",quote_observer)
    result=ps.run(dry_run=False)
    assert result["coverage"]["^NDX"]["status"]=="observed"
    assert result["coverage"]["NDQ.AX"]["status"]=="fetch_failed_or_unsupported"
    assert result["notifications_queued"]==1
    quote_observer.assert_not_called()
    pipeline.deliver()
    assert set(pipeline.trigger.call_args.kwargs["symbols"])=={"NDQ.AX","QQQ"}
    database=EventStore(str(pipeline.store.data_root/"db/events.db"))
    observation=database.list_recent()[0]
    assert set(observation["affected_symbols"])=={"^NDX","NDQ.AX","QQQ"}
    assert "不是这些资产的报价" in observation["one_line_claim"]
    database.conn.close()


def test_news_and_price_share_budget_and_deferred_causes_survive(pipeline):
    from openinvest.core.config import set_config_override
    from openinvest.services.event_trigger import reserve_analysis
    set_config_override({"event":{"risk_alerts_enabled":True,"committee_daily_cap":1}})
    assert reserve_analysis(pipeline.store,[("NEWS-ASSET",3)],now=pipeline.now-timedelta(hours=23,minutes=55))==["NEWS-ASSET"]
    ps.run(dry_run=False);pipeline.deliver()
    pipeline.trigger.assert_not_called()
    with pipeline.box().connection() as db:
        row=db.execute("SELECT status,error FROM followups").fetchone()
        assert tuple(row)==("pending","cooldown_or_cost_budget_deferred")
    pipeline.now+=timedelta(minutes=6)
    pipeline.deliver()
    pipeline.trigger.assert_called_once()
    assert pipeline.trigger.call_args.kwargs["symbols"]==["GC=F"]
    with pipeline.box().connection() as db:
        assert db.execute("SELECT status FROM followups").fetchone()[0]=="queued"


def test_review_and_risk_plans_queue_one_notice_before_analysis(pipeline):
    from openinvest.services.review_plans import update_plan
    pipeline.frames["closes"]=[100,100,100];pipeline.frames["price"]=100
    spec={"conditions":[{"metric":"quote_price","op":"<=","value":101,"unit":"USD/oz"}],
          "expires_at":(pipeline.now+timedelta(days=1)).isoformat()}
    for purpose in ("review","risk"):update_plan(pipeline.store,"GC=F",spec,now=pipeline.now,purpose=purpose)
    result=ps.run(dry_run=False)
    assert result["notifications_queued"]==1 and pipeline.calls==[]
    pipeline.deliver()
    assert [c[0] for c in pipeline.calls]==["discord","email","analysis"]
    notice=pipeline.calls[0][1]
    assert len(notice["plan_causes"])==2 and "events" not in notice
    assert pipeline.trigger.call_count==1


def test_plan_processing_failure_cannot_prevent_independent_market_warning(pipeline,monkeypatch):
    monkeypatch.setattr("openinvest.services.review_plans.observe_quote",lambda *a,**kw:(_ for _ in ()).throw(RuntimeError("synthetic plan failure")))
    result=ps.run(dry_run=False)
    assert result["notifications_queued"]==1 and result["status"]=="coverage_limited"
    assert result["coverage"]["GC=F"]["plan_error"]=="RuntimeError"
    assert pipeline.calls==[]
    pipeline.deliver()
    assert pipeline.calls[0][0]=="discord"


def test_partial_budget_tracks_submitted_symbols_and_retries_only_remaining(pipeline,monkeypatch):
    from openinvest.core.config import set_config_override
    from openinvest.services.event_trigger import reserve_analysis
    set_config_override({"event":{"risk_alerts_enabled":True,"committee_daily_cap":2}})
    pipeline.symbols=["NDQ.AX","QQQ"];pipeline.targets=list(pipeline.symbols)
    monkeypatch.setattr(ps,"_fetch_frames",lambda sym:pipeline.frames if sym=="^NDX" else None)
    reserve_analysis(pipeline.store,[("OLD-NEWS",3)],now=pipeline.now-timedelta(hours=23,minutes=55))
    ps.run(dry_run=False);pipeline.deliver()
    assert pipeline.trigger.call_count==1
    first=pipeline.trigger.call_args.kwargs["symbols"]
    assert len(first)==1
    pipeline.now+=timedelta(minutes=6)
    pipeline.deliver()
    second=pipeline.trigger.call_args.kwargs["symbols"]
    assert len(second)==1 and set(first+second)=={"NDQ.AX","QQQ"}
    assert pipeline.trigger.call_count==2


def test_same_batch_over_budget_survives_notification_expiry_and_runs_next_day(pipeline,monkeypatch):
    from openinvest.core.config import set_config_override
    set_config_override({"event":{"risk_alerts_enabled":True,"committee_daily_cap":1}})
    pipeline.symbols=["NDQ.AX","QQQ"];pipeline.targets=list(pipeline.symbols)
    monkeypatch.setattr(ps,"_fetch_frames",lambda sym:pipeline.frames if sym=="^NDX" else None)
    ps.run(dry_run=False);pipeline.deliver()
    assert pipeline.trigger.call_count==1
    first=pipeline.trigger.call_args.kwargs["symbols"]
    pipeline.now+=timedelta(hours=24,minutes=1)
    pipeline.deliver()
    assert pipeline.trigger.call_count==2
    assert set(first+pipeline.trigger.call_args.kwargs["symbols"])=={"NDQ.AX","QQQ"}
    with pipeline.box().connection() as db:
        assert db.execute("SELECT status FROM followups").fetchone()[0]=="queued"
    pipeline.deliver()
    assert pipeline.trigger.call_count==2


def test_channel_failure_keeps_pending_and_does_not_consume_success_cooldown(pipeline):
    pipeline.sender=lambda channel,payload:False
    ps.run(dry_run=False)
    assert pipeline.deliver()["accepted_notifications"]==0
    assert ps.run(dry_run=False)["detected"]==1
    with pipeline.box().connection() as db:
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]==1
    pipeline.sender=lambda channel,payload:pipeline.calls.append((channel,payload)) or True
    pipeline.now+=timedelta(minutes=6)
    assert pipeline.deliver()["accepted_notifications"]==1
    assert ps.run(dry_run=False)["detected"]==0


def test_stale_quotes_are_coverage_gaps_not_normal_risk(pipeline):
    pipeline.frames["last_bar_utc"]=pipeline.now-timedelta(hours=3)
    result=ps.run(dry_run=False)
    assert result["status"]=="coverage_limited"
    assert result["coverage"]["GC=F"]["status"]=="stale_session_unverified"
    assert result["notifications_queued"]==0


def test_dry_run_detects_without_writes_or_delivery(pipeline):
    result=ps.run(dry_run=True)
    assert result["detected"]==1 and result["alerted"]==0
    assert not (pipeline.store.data_root/"db").exists()
    assert pipeline.calls==[]


def test_disabled_scan_stops_before_fetch(pipeline,monkeypatch):
    from openinvest.core.config import set_config_override
    set_config_override({"event":{"sentinel_enabled":False}})
    monkeypatch.setattr(ps,"_fetch_frames",lambda symbol:pytest.fail("disabled scanner fetched data"))
    assert ps.run()["status"]=="disabled"


def test_missing_atr_uses_independent_historical_return_reference(pipeline):
    from openinvest.calc.risk_observations import return_risk_observations
    import math
    prior=[100+math.sin(i)*.1 for i in range(300)]
    pipeline.frames.update(closes=[95.0],price=95.0,atr_pct=None,risk_observations=return_risk_observations(prior,95))
    scan=ps.run(dry_run=False)
    assert scan["detected"]==1
    pipeline.deliver()
    event=pipeline.calls[0][1]["events"][0]
    assert "交易日累计" in event["one_line_claim"] and event["stance"]=="risk"


def test_event_index_failure_retains_alert_and_recovers_analysis(pipeline,monkeypatch):
    from openinvest.db.event_store import EventStore
    original=EventStore.upsert_event
    monkeypatch.setattr(EventStore,"upsert_event",lambda *a,**kw:(_ for _ in ()).throw(RuntimeError("index down")))
    assert ps.run(dry_run=False)["notifications_queued"]==1
    result=pipeline.deliver()
    assert result["accepted_notifications"]==1
    pipeline.trigger.assert_not_called()
    monkeypatch.setattr(EventStore,"upsert_event",original)
    pipeline.deliver()
    pipeline.trigger.assert_called_once()
    assert len([c for c in pipeline.calls if c[0]!="analysis"])==2


def _user_line(env,rule,value,price=880.0,day=None,change=None):
    from openinvest.utils.quotes import QuoteSnapshot
    env.frames=None
    env.lines=[{"symbol":"GC=F","alerts":{rule:value},"holding":None}]
    env.quote=QuoteSnapshot(symbol="GC=F",price=price,currency="CNY",unit="克",extra={})
    if day:
        env.quote.last_updated=day;env.quote.extra={"day_change_pct":change}


def test_user_line_preserves_quote_units_and_does_not_enter_committee(pipeline):
    from openinvest.db.event_store import EventStore
    _user_line(pipeline,"price_below",900)
    assert ps.run(dry_run=False)["price_alerts"]==["GC=F:price_below"]
    pipeline.deliver()
    event=pipeline.calls[0][1]["events"][0]
    assert "880.00 CNY" in event["one_line_claim"] and event["event_type"]=="price_alert"
    pipeline.trigger.assert_not_called()
    db=EventStore(str(pipeline.store.data_root/"db/events.db"))
    assert db.recall("GC=F",min_severity="low")==[]
    db.conn.close()


def test_user_line_cooldown_survives_midnight_and_allows_next_day_after_24h(pipeline):
    _user_line(pipeline,"price_below",900)
    pipeline.now=pipeline.now.replace(hour=23,minute=59,second=0,microsecond=0)
    ps.run(dry_run=False);pipeline.deliver()
    pipeline.now+=timedelta(minutes=2)
    assert ps.run(dry_run=False)["price_alerts"]==[]
    pipeline.now+=timedelta(hours=25)
    assert ps.run(dry_run=False)["price_alerts"]==["GC=F:price_below"]
    pipeline.deliver()
    assert len(pipeline.calls)==4


def test_day_drop_rule_is_keyed_by_market_session_not_wall_clock(pipeline):
    _user_line(pipeline,"day_drop_pct",3,price=1000,day="2026-10-16",change=-3.4)
    ps.run(dry_run=False);pipeline.deliver()
    pipeline.now+=timedelta(hours=49)
    assert ps.run(dry_run=False)["price_alerts"]==[]
    pipeline.quote.last_updated="2026-10-19"
    assert ps.run(dry_run=False)["price_alerts"]==["GC=F:day_drop_pct"]
    pipeline.deliver()
    assert len(pipeline.calls)==4


def test_stale_user_quote_does_not_fire_and_changed_rule_gets_new_identity(pipeline):
    _user_line(pipeline,"price_below",900)
    ps.run(dry_run=False);pipeline.deliver()
    pipeline.lines[0]["alerts"]["price_below"]=950
    assert ps.run(dry_run=False)["price_alerts"]==["GC=F:price_below"]
    pipeline.quote.is_stale=True
    assert ps.run(dry_run=False)["price_alerts"]==[]


def test_user_line_failure_cannot_stop_market_risk_scan(pipeline,monkeypatch):
    monkeypatch.setattr(ps,"_check_user_alerts",lambda *a,**kw:(_ for _ in ()).throw(RuntimeError("line evaluator down")))
    result=ps.run(dry_run=False)
    assert result["notifications_queued"]==1
    assert result["coverage"]["user_alert_lines"]["status"]=="evaluation_failed"


def test_fetch_frames_uses_only_prior_sessions_for_reference_and_keeps_first_bar(monkeypatch):
    import pandas as pd
    import yfinance as yf
    prices=[100+(i%3)*.1 for i in range(300)]+[80.0]
    days=pd.date_range("2026-01-01",periods=len(prices),tz="UTC")
    daily=pd.DataFrame({"Close":prices,"High":[p+1 for p in prices],"Low":[p-1 for p in prices]},index=days)
    intraday=pd.DataFrame({"Close":[80.0,float("nan")]},index=pd.date_range(days[-1],periods=2,freq="5min"))
    monkeypatch.setattr(yf,"Ticker",lambda symbol:SimpleNamespace(history=lambda **kw:intraday if kw["interval"]=="5m" else daily))
    frame=ps._fetch_frames("SYNTHETIC")
    assert frame["price"]==80.0 and len(frame["closes"])==1
    day=frame["risk_observations"]["observations"][0]
    assert day["reference_price"]==prices[-2] and day["breached"]
    assert frame["atr_pct"]<3  # The current 20% shock cannot inflate its own historical ATR.
