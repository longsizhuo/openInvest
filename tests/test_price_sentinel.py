"""价格异动哨兵（jobs/price_sentinel.py，ADR-025）单测

守的核心行为：
1. 检测：10 分钟涨跌 vs 日 ATR% 归一化阈值；ATR 缺失退绝对兜底
2. 冷却：同 symbol 同方向静默期内不重复报；反方向不受限
3. **时序契约：报警邮件先于委员会触发**（用户需求"先报给我，再跑 committee"）
4. 委员会触发失败不影响已发出的报警（不抛、不回滚）
5. 数据停滞（闭市）跳过；disabled 短路
"""
from datetime import datetime, timedelta, timezone
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
        assert ps._detect_move([100.0, 100.6, 101.2], atr_pct=None, mult=0.8) is not None
        assert ps._detect_move([100.0, 100.2, 100.5], atr_pct=None, mult=0.8) is None

    def test_atr_missing_ratio_is_none_not_zero(self):
        """code review 发现：ratio 曾经用 0.0 当"ATR 缺失"哨兵值，和真实算出 0 倍
        混淆，且会让 run() 里的 severity 判断把巨幅异动错判成最低档。改用 None。"""
        hit = ps._detect_move([100.0, 100.6, 101.2], atr_pct=None, mult=0.8)
        assert hit["ratio"] is None

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


# ---------- run() 集成（mock 外设，真 EventStore） ----------

@pytest.fixture
def sentinel_env(tmp_path, monkeypatch):
    """隔离 EventStore / MemoryStore 到 tmp，mock 行情与外设。"""
    monkeypatch.setattr("openinvest.db.event_store.DB_PATH", str(tmp_path / "events.sqlite"))
    from openinvest.core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")

    # run() 内是 `from jobs.event_watch import _load_user_context`（调用时解析），打源模块
    monkeypatch.setattr(
        "openinvest.jobs.event_watch._load_user_context",
        lambda: {"holdings": ["GC=F"], "watching": ["GC=F"], "macro_tags": [], "queries": []},
    )
    now = datetime.now(timezone.utc)
    # 垂直线场景：10 分钟 +1.77%，ATR 1.16%
    monkeypatch.setattr(ps, "_fetch_frames", lambda sym: {
        "closes": [100.0, 100.5, 101.77],
        "last_bar_utc": now - timedelta(minutes=10),
        "price": 101.77,
        "atr_pct": 1.16,
    })
    monkeypatch.setattr(ps, "_latest_verdict", lambda sym, committee_root=None: "最近委员会 verdict HOLD（2026-07-02）")

    # 用同一个 manager 录制调用顺序（时序契约的关键断言点）
    manager = MagicMock()
    monkeypatch.setattr("openinvest.services.event_notifier.send_event_alert", manager.alert)
    monkeypatch.setattr("openinvest.services.event_trigger._trigger_committee", manager.trigger)
    manager.trigger.return_value = "task-123"
    monkeypatch.setattr("openinvest.services.event_trigger._holdings_snapshot", lambda syms: {})
    return manager


class TestRun:
    def test_alert_fires_before_committee(self, sentinel_env):
        """时序契约：先报警邮件，后触发委员会。"""
        out = ps.run(dry_run=False)
        assert out["alerted"] == 1 and out["symbols"] == ["GC=F"]
        names = [c[0] for c in sentinel_env.mock_calls]
        assert names.index("alert") < names.index("trigger")

    def test_holdings_only_symbol_alerts_without_committee(self, sentinel_env, monkeypatch):
        """只在持仓、不在 target_assets：照样报警，但不触发委员会（session 会拒绝它）。"""
        monkeypatch.setattr(
            "openinvest.jobs.event_watch._load_user_context",
            lambda: {"holdings": ["GC=F"], "watching": [], "macro_tags": [], "queries": []},
        )
        out = ps.run(dry_run=False)
        assert out["alerted"] == 1
        sentinel_env.alert.assert_called_once()
        sentinel_env.trigger.assert_not_called()

    def test_committee_failure_does_not_kill_alert(self, sentinel_env):
        """委员会路径炸了：报警已发出，run 不抛。"""
        sentinel_env.trigger.side_effect = RuntimeError("web api down")
        out = ps.run(dry_run=False)
        assert out["alerted"] == 1
        sentinel_env.alert.assert_called_once()

    def test_cooldown_blocks_second_run(self, sentinel_env):
        ps.run(dry_run=False)
        out2 = ps.run(dry_run=False)
        assert out2["alerted"] == 0
        assert sentinel_env.alert.call_count == 1

    def test_late_waking_abandoned_instance_does_not_double_alert(self, sentinel_env, monkeypatch):
        """runner 超时会放弃卡住的实例、让新实例进来。旧实例醒来时手里的冷却快照是过期的：
        发报警前必须对最新状态再核一次，别把新实例刚报过的异动再报一遍。"""
        from openinvest.core.memory_store import MemoryStore
        real_fetch = ps._fetch_frames

        def fetch_then_other_instance_alerts(sym):
            # run() 已读完 state 快照；此刻"新实例"报警并落了冷却
            MemoryStore().state_set(ps._STATE_NAME, {"GC=F:up": datetime.now(timezone.utc).isoformat()})
            return real_fetch(sym)

        monkeypatch.setattr(ps, "_fetch_frames", fetch_then_other_instance_alerts)
        out = ps.run(dry_run=False)
        assert out["alerted"] == 0
        sentinel_env.alert.assert_not_called()
        sentinel_env.trigger.assert_not_called()

    def test_cooldown_stamp_merges_instead_of_clobbering_other_keys(self, sentinel_env):
        """落冷却只合并本 key：另一实例在本实例报警期间写的其它 symbol 冷却不能被整份回写覆盖。"""
        from openinvest.core.memory_store import MemoryStore
        other = datetime.now(timezone.utc).isoformat()
        sentinel_env.alert.side_effect = lambda *a, **k: MemoryStore().state_set(
            ps._STATE_NAME, {"NDQ.AX:down": other})
        ps.run(dry_run=False)
        state = MemoryStore().state_get(ps._STATE_NAME)
        assert state["NDQ.AX:down"] == other and "GC=F:up" in state

    def test_stale_bars_skip(self, sentinel_env, monkeypatch):
        """最后一根 bar 太旧（闭市）→ 不拿昨天尾巴当异动。"""
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        monkeypatch.setattr(ps, "_fetch_frames", lambda sym: {
            "closes": [100.0, 100.5, 101.77], "last_bar_utc": old,
            "price": 101.77, "atr_pct": 1.16,
        })
        out = ps.run(dry_run=False)
        assert out["alerted"] == 0
        sentinel_env.alert.assert_not_called()

    def test_dry_run_no_side_effects(self, sentinel_env):
        out = ps.run(dry_run=True)
        assert out["alerted"] == 1 and out["dry_run"] is True
        sentinel_env.alert.assert_not_called()
        sentinel_env.trigger.assert_not_called()

    def test_disabled_short_circuits(self, sentinel_env, monkeypatch):
        fake_cfg = SimpleNamespace(event=SimpleNamespace(
            sentinel_enabled=False, sentinel_atr_mult=0.8, sentinel_cooldown_min=120))
        monkeypatch.setattr("openinvest.core.config.load_config", lambda *a, **k: fake_cfg)
        out = ps.run(dry_run=False)
        assert out["status"] == "disabled"
        sentinel_env.alert.assert_not_called()

    def test_atr_missing_large_move_gets_high_severity(self, sentinel_env, monkeypatch):
        """code review 发现：ATR 缺失时旧版 ratio 哨兵值 0.0 会让 severity 永远判 mid，
        哪怕涨跌幅巨大（如新上市标的闪崩 15%）。改用 None 后 severity 该按绝对涨跌幅判。"""
        monkeypatch.setattr(ps, "_fetch_frames", lambda sym: {
            "closes": [100.0, 105.0, 115.0],  # 10 分钟 +15%，ATR 缺失
            "last_bar_utc": datetime.now(timezone.utc) - timedelta(minutes=10),
            "price": 115.0, "atr_pct": None,
        })
        captured = {}
        from openinvest.db.event_store import EventStore
        real_upsert = EventStore.upsert_event

        def spy_upsert(self, event, embedding=None):
            captured["event"] = event
            return real_upsert(self, event, embedding=embedding)

        monkeypatch.setattr(EventStore, "upsert_event", spy_upsert)
        ps.run(dry_run=False)
        assert captured["event"]["severity"] == "high"

    def test_upsert_failure_on_later_symbol_does_not_lose_earlier_cooldown(self, monkeypatch, tmp_path):
        """code review 发现：旧版把 ms.state_set 攒到整个 for 循环跑完才调一次；
        如果后面某个 symbol 的 EventStore.upsert_event 抛异常，前面已经成功报警的
        symbol 的冷却状态因为从未持久化而丢失，下个 tick 会被重复报警。"""
        monkeypatch.setattr("openinvest.db.event_store.DB_PATH", str(tmp_path / "events.sqlite"))
        from openinvest.core import memory_store as ms_mod
        monkeypatch.setattr(ms_mod, "MEMORY_ROOT", tmp_path / "memory")
        monkeypatch.setattr(
            "openinvest.jobs.event_watch._load_user_context",
            lambda: {"holdings": ["AAA", "BBB"], "watching": [], "macro_tags": [], "queries": []},
        )
        now = datetime.now(timezone.utc)
        frames = {
            "closes": [100.0, 100.5, 101.77], "last_bar_utc": now - timedelta(minutes=10),
            "price": 101.77, "atr_pct": 1.16,
        }
        monkeypatch.setattr(ps, "_fetch_frames", lambda sym: frames)
        monkeypatch.setattr(ps, "_latest_verdict", lambda sym, committee_root=None: "近期无委员会 verdict")
        monkeypatch.setattr("openinvest.services.event_trigger._trigger_committee", lambda *a, **k: "task-1")
        monkeypatch.setattr("openinvest.services.event_trigger._holdings_snapshot", lambda syms: {})
        monkeypatch.setattr("openinvest.services.event_notifier.send_event_alert", lambda *a, **k: None)

        from openinvest.db.event_store import EventStore
        real_upsert = EventStore.upsert_event

        def flaky_upsert(self, event, embedding=None):
            if event["affected_symbols"] == ["BBB"]:
                raise RuntimeError("simulated DB failure on BBB")
            return real_upsert(self, event, embedding=embedding)

        monkeypatch.setattr(EventStore, "upsert_event", flaky_upsert)

        out = ps.run(dry_run=False)
        assert out["alerted"] == 1 and out["symbols"] == ["AAA"]  # BBB 失败被跳过

        # 第二轮：AAA 应该仍在冷却期内（不重复报），即便上一轮 BBB 抛了异常
        monkeypatch.setattr(EventStore, "upsert_event", real_upsert)
        out2 = ps.run(dry_run=False)
        assert "AAA" not in out2["symbols"]


# ---------- 用户自设报警线（C8） ----------

class TestUserAlerts:
    """target_assets[].alerts 每个 tick 判一次：命中 → 24h 原子冷却 → 入库(price_alert) → 同通道报警。
    只入库不触发委员会；INVEST_EVENT_ALERT 总开关关着时只入库不发。"""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        from openinvest.utils.quotes import QuoteSnapshot

        monkeypatch.setattr("openinvest.db.event_store.DB_PATH", str(tmp_path / "events.sqlite"))
        from openinvest.core import memory_store as ms_mod
        monkeypatch.setattr(ms_mod, "MEMORY_ROOT", tmp_path / "memory")
        # ATR 路径本测试不关心：没有标的、没有 5m 数据
        monkeypatch.setattr("openinvest.jobs.event_watch._load_user_context",
                            lambda: {"holdings": ["GC=F"], "watching": ["GC=F"], "macro_tags": [], "queries": []})
        monkeypatch.setattr(ps, "_fetch_frames", lambda sym: None)
        monkeypatch.setattr(ps, "_latest_verdict", lambda sym, committee_root=None: "近期无委员会 verdict")

        e = SimpleNamespace(
            targets=[{"symbol": "GC=F", "alerts": {"price_below": 900.0}, "holding": None}],
            quote=QuoteSnapshot(symbol="GC=F", price=880.0, currency="CNY", unit="克", extra={}),
            alert=MagicMock(), trigger=MagicMock(), snapshot=MagicMock(return_value={}))
        monkeypatch.setattr(ps, "_alert_targets", lambda: e.targets)
        monkeypatch.setattr("openinvest.services.price_alerts.fetch_quote", lambda sym, holding=None: e.quote)
        monkeypatch.setattr("openinvest.services.event_notifier.send_event_alert", e.alert)
        monkeypatch.setattr("openinvest.services.event_trigger._trigger_committee", e.trigger)
        monkeypatch.setattr("openinvest.services.event_trigger._holdings_snapshot", lambda syms: e.snapshot(syms))
        return e

    @staticmethod
    def _events():
        from openinvest.db.event_store import EventStore
        rows = EventStore().conn.execute(
            "SELECT event_type, severity, stance, affected_symbols_json, committee_task_id, one_line_claim FROM events").fetchall()
        return [tuple(r) for r in rows]

    def test_price_below_fires_records_event_and_alerts_without_committee(self, env):
        out = ps.run(dry_run=False)
        assert out["price_alerts"] == ["GC=F:price_below"] and out["alerted"] == 0  # alerted 仍只计 ATR
        env.alert.assert_called_once()
        evs = env.alert.call_args.args[0]
        assert evs[0]["event_type"] == "price_alert" and evs[0]["affected_symbols"] == ["GC=F"]
        assert "已触及 price_below 900" in evs[0]["one_line_claim"] and "880.00 CNY" in evs[0]["one_line_claim"]
        (et, sev, stance, syms, task, _), = self._events()
        assert (et, sev, stance, task) == ("price_alert", 1, "risk", None)
        env.trigger.assert_not_called()  # 只入库不触发委员会

    def test_price_above_crossing_up(self, env):
        env.targets[0]["alerts"] = {"price_above": 1000.0}
        env.quote.price = 1010.0
        assert ps.run(dry_run=False)["price_alerts"] == ["GC=F:price_above"]
        assert self._events()[0][2] == "opportunity"

    def test_day_drop_uses_quote_day_change(self, env):
        env.targets[0]["alerts"] = {"day_drop_pct": 3.0}
        env.quote.price, env.quote.extra = 1000.0, {"day_change_pct": -3.4}
        assert ps.run(dry_run=False)["price_alerts"] == ["GC=F:day_drop_pct"]
        env.quote.extra = {"day_change_pct": -1.0}  # 没到线
        env.alert.reset_mock()
        assert ps.run(dry_run=False)["price_alerts"] == []
        env.alert.assert_not_called()

    def test_drawdown_from_cost_needs_a_held_position(self, env):
        env.targets[0]["alerts"] = {"drawdown_from_cost_pct": 15.0}
        env.quote.price = 80.0
        env.targets[0]["holding"] = {"symbol": "GC=F", "units": 10.0, "avg_cost": 100.0}  # -20% < -15%
        assert ps.run(dry_run=False)["price_alerts"] == ["GC=F:drawdown_from_cost_pct"]
        # 同一价格但没有持仓（只跟踪）→ 无成本可比，不报
        env.targets[0]["holding"] = None
        assert ps.run(dry_run=False)["price_alerts"] == []

    def test_cooldown_24h_is_atomic_and_expires(self, env):
        from openinvest.core.memory_store import MemoryStore
        ms = MemoryStore()
        now = datetime.now(timezone.utc)
        # 同一刻两个实例抢同一条：只有一个抢到
        assert ps._claim_alert(ms, "GC=F", "price_below", now) is not None
        assert ps._claim_alert(ms, "GC=F", "price_below", now) is None
        assert ps._claim_alert(ms, "GC=F", "day_drop_pct", now) is not None  # 另一条规则互不影响
        # 端到端：已在冷却内 → run 不再入库不再报
        out = ps.run(dry_run=False)
        assert out["price_alerts"] == [] and self._events() == []
        env.alert.assert_not_called()
        # 25h 前盖的章 → 过期，可再报；再跑一次又在冷却内
        ms.state_set(ps._ALERT_STATE_NAME, {"GC=F:price_below": (now - timedelta(hours=25)).isoformat()})
        assert ps.run(dry_run=False)["price_alerts"] == ["GC=F:price_below"]
        assert ps.run(dry_run=False)["price_alerts"] == []
        assert env.alert.call_count == 1 and len(self._events()) == 1

    def test_alert_master_switch_off_stores_event_but_sends_nothing(self, env, monkeypatch):
        """INVEST_EVENT_ALERT 默认关：走真实 send_event_alert，Discord / 邮件出口一个都不能被调，
        连持仓快照（要拉行情）都不算；事件照常入库、冷却照常盖章。"""
        discord, email = MagicMock(), MagicMock()
        monkeypatch.setattr("openinvest.services.event_notifier.send_event_alert", _REAL_SEND)
        monkeypatch.setattr("openinvest.services.event_notifier.send_discord_alert", discord)
        monkeypatch.setattr("openinvest.services.event_notifier.send_email_html", email)
        out = ps.run(dry_run=False)
        assert out["price_alerts"] == ["GC=F:price_below"]
        assert len(self._events()) == 1
        discord.assert_not_called()
        email.assert_not_called()
        env.snapshot.assert_not_called()
        assert ps.run(dry_run=False)["price_alerts"] == []  # 冷却已盖章：开关关着也不会每 5 分钟入一条

    def test_alert_master_switch_on_sends_via_same_channel(self, env, monkeypatch):
        discord, email = MagicMock(), MagicMock()
        monkeypatch.setattr("openinvest.services.event_notifier.send_event_alert", _REAL_SEND)
        monkeypatch.setattr("openinvest.services.event_notifier.send_discord_alert", discord)
        monkeypatch.setattr("openinvest.services.event_notifier.send_email_html", email)
        monkeypatch.setenv("INVEST_EVENT_ALERT", "1")
        ps.run(dry_run=False)
        discord.assert_called_once()
        assert "price_below" in discord.call_args.args[0]
        email.assert_called_once()
        env.snapshot.assert_called_once_with(["GC=F"])

    def test_stale_quote_never_alerts(self, env):
        env.quote.is_stale = True
        assert ps.run(dry_run=False)["price_alerts"] == []
        assert self._events() == []

    def test_dry_run_has_no_side_effects(self, env):
        out = ps.run(dry_run=True)
        assert out["price_alerts"] == ["GC=F:price_below"]
        assert self._events() == []
        env.alert.assert_not_called()
        from openinvest.core.memory_store import MemoryStore
        assert MemoryStore().state_get(ps._ALERT_STATE_NAME) is None

    def test_event_store_failure_releases_cooldown_for_retry(self, env, monkeypatch):
        from openinvest.db.event_store import EventStore
        real = EventStore.upsert_event
        monkeypatch.setattr(EventStore, "upsert_event", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        assert ps.run(dry_run=False)["price_alerts"] == []
        env.alert.assert_not_called()
        monkeypatch.setattr(EventStore, "upsert_event", real)
        assert ps.run(dry_run=False)["price_alerts"] == ["GC=F:price_below"]  # 没被冷却吃掉

    def test_alert_pass_failure_does_not_block_atr_sentinel(self, sentinel_env, monkeypatch):
        monkeypatch.setattr(ps, "_check_user_alerts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        out = ps.run(dry_run=False)
        assert out["alerted"] == 1 and out["price_alerts"] == []

    def test_price_alert_event_never_passes_the_committee_gate(self, env):
        """severity=low：事件触发闸 / RAG 召回 / 日报资产事件的最低门槛都是 mid，用户的线进不了委员会论据。"""
        from openinvest.services.event_trigger import is_triggerable
        ps.run(dry_run=False)
        ev = env.alert.call_args.args[0][0]
        assert not is_triggerable(ev, ["gc=f"], "mid")
        from openinvest.db.event_store import EventStore
        store = EventStore()
        assert store.recall("GC=F", time_window_days=7, min_severity="mid", top_k=10) == []
        assert len(store.recall("GC=F", time_window_days=7, min_severity="low", top_k=10)) == 1  # 召回本身看得见它


def test_fetch_frames_ignores_trailing_nan_bar(monkeypatch):
    """5 分钟线末根 Close=NaN 时取最后一根有效价，不让涨跌幅变 NaN 静默漏报"""
    import pandas as pd
    import yfinance as yf

    n = ps._WINDOW_BARS + 2
    idx = pd.date_range("2026-10-15 01:30", periods=n + 1, freq="5min", tz="UTC")
    intraday = pd.DataFrame({"Close": [100.0] * n + [float("nan")]}, index=idx)
    intraday.iloc[n - 1, 0] = 103.0
    daily = pd.DataFrame({"Close": [100.0] * 30 + [float("nan")], "High": [101.0] * 31, "Low": [99.0] * 31},
                         index=pd.date_range("2026-08-01", periods=31, tz="UTC"))
    monkeypatch.setattr(yf, "Ticker", lambda sym: SimpleNamespace(
        history=lambda period, interval: intraday if interval == "5m" else daily))
    frames = ps._fetch_frames("GC=F")
    assert frames["price"] == 103.0
    assert all(c == c for c in frames["closes"])  # 无 NaN
    assert frames["last_bar_utc"] == idx[n - 1].to_pydatetime()
    assert frames["atr_pct"] is not None  # 日线末根 NaN 也不让 ATR 归一化失效
