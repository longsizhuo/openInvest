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

from jobs import price_sentinel as ps


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
    monkeypatch.setattr("db.event_store.DB_PATH", str(tmp_path / "events.sqlite"))
    from core import memory_store as ms
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")

    # run() 内是 `from jobs.event_watch import _load_user_context`（调用时解析），打源模块
    monkeypatch.setattr(
        "jobs.event_watch._load_user_context",
        lambda: {"holdings": ["GC=F"], "watching": [], "macro_tags": [], "queries": []},
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
    monkeypatch.setattr("services.event_notifier.send_event_alert", manager.alert)
    monkeypatch.setattr("jobs.event_watch._trigger_committee", manager.trigger)
    manager.trigger.return_value = "task-123"
    monkeypatch.setattr("jobs.event_watch._holdings_snapshot", lambda syms: {})
    return manager


class TestRun:
    def test_alert_fires_before_committee(self, sentinel_env):
        """时序契约：先报警邮件，后触发委员会。"""
        out = ps.run(dry_run=False)
        assert out["alerted"] == 1 and out["symbols"] == ["GC=F"]
        names = [c[0] for c in sentinel_env.mock_calls]
        assert names.index("alert") < names.index("trigger")

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
        monkeypatch.setattr("core.config.load_config", lambda *a, **k: fake_cfg)
        out = ps.run(dry_run=False)
        assert out["status"] == "disabled"
        sentinel_env.alert.assert_not_called()
