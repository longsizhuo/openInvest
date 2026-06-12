"""反事实记账（interventions.jsonl）单元测试

覆盖：
- _intervention_record() 各类干预标记的提取 + 非干预的过滤
- _log_intervention() 落盘
- jobs/intervention_review 的反事实损益算术（monkeypatch 行情）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.committee_runner import _intervention_record  # noqa: E402


def _v(**kw):
    base = {"verdict": "HOLD", "confidence": 0.5, "alloc_cny": 0}
    base.update(kw)
    return base


class TestInterventionRecord:
    def test_defense_downgrade_buy_side(self):
        """快崩防御 ACCUMULATE→HOLD：delta_exposure = +原 alloc"""
        rec = _intervention_record(
            "GC=F", "downtrend", 4100.0,
            _v(verdict="HOLD", alloc_cny=0,
               _original_verdict="ACCUMULATE", _original_alloc=3000,
               _defense_downgrade="accumulate_to_hold"),
            atr_defense_on=False,
        )
        assert rec is not None
        assert rec["rule"] == "defense_accumulate_to_hold"
        assert rec["delta_exposure_cny"] == 3000.0
        assert rec["asset"] == "GC=F" and rec["price"] == 4100.0

    def test_sanity4_blocked_trim(self):
        """SOLVENCY 拦 TRIM：delta_exposure = 负（原 alloc 是负数卖出）"""
        rec = _intervention_record(
            "GC=F", "downtrend", 4100.0,
            _v(verdict="HOLD", alloc_cny=0,
               _original_verdict="TRIM", _original_alloc=-20000,
               _original_trim_reason="concentration"),
            atr_defense_on=False,
        )
        assert rec["rule"] == "sanity4_solvency_concentration"
        assert rec["delta_exposure_cny"] == -20000.0

    def test_sanity5_reentry_missing(self):
        rec = _intervention_record(
            "NDQ.AX", "uptrend", 60.0,
            _v(verdict="HOLD", alloc_cny=0,
               _original_verdict="TRIM", _original_alloc=-5000,
               _sanity5_reason="reentry_missing"),
            atr_defense_on=True,
        )
        assert rec["rule"] == "sanity5_reentry_missing"
        assert rec["atr_defense_on"] is True

    def test_no_marker_returns_none(self):
        assert _intervention_record("GC=F", "range", 4100.0, _v(), False) is None

    def test_confidence_only_change_not_logged(self):
        """只动 confidence（如 overdrive 压帽）不算干预"""
        rec = _intervention_record(
            "GC=F", "uptrend", 4100.0,
            _v(verdict="BUY", alloc_cny=5000,
               _original_verdict="BUY", _original_alloc=5000,
               _original_confidence=0.97),
            atr_defense_on=False,
        )
        assert rec is None

    def test_empty_verdict_graceful(self):
        assert _intervention_record("GC=F", "", None, None, False) is None


class TestLogIntervention:
    def test_appends_jsonl(self, tmp_path, monkeypatch):
        from core import memory_store as ms
        monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")
        from core.committee_runner import _log_intervention
        _log_intervention({"schema": 1, "date": "2026-06-12", "asset": "GC=F",
                           "rule": "defense_accumulate_to_hold"})
        _log_intervention({"schema": 1, "date": "2026-06-13", "asset": "GC=F",
                           "rule": "sanity4_solvency_concentration"})
        p = tmp_path / "memory" / ".dreams" / "interventions.jsonl"
        lines = [json.loads(l) for l in p.read_text().splitlines()]
        assert len(lines) == 2
        assert lines[1]["rule"] == "sanity4_solvency_concentration"


class TestReviewArithmetic:
    def test_counterfactual_pnl_signs(self, monkeypatch):
        """被拦买入遇涨=正（拦错踏空）；被拦减仓遇跌=正（拦错多亏）"""
        import jobs.intervention_review as ir
        # 30d +10%，60d -5%，90d 未到期
        monkeypatch.setattr(ir, "fwd_return",
                            lambda sym, d, w: {30: 0.10, 60: -0.05, 90: None}[w])
        rows = [
            {"date": "2026-01-01", "asset": "GC=F", "rule": "defense_accumulate_to_hold",
             "delta_exposure_cny": 3000.0},
            {"date": "2026-01-01", "asset": "GC=F", "rule": "sanity4_solvency_concentration",
             "delta_exposure_cny": -20000.0},
        ]
        scored = ir.score(rows)
        # 被拦买入：涨 10% → 拦截踏空 +300；跌 5% → 拦对 -150
        assert scored[0]["counterfactual_pnl_30d_cny"] == 300.0
        assert scored[0]["counterfactual_pnl_60d_cny"] == -150.0
        assert scored[0]["counterfactual_pnl_90d_cny"] is None
        # 被拦减仓：涨 10% → 拦对（没卖飞）-2000；跌 5% → 拦错 +1000
        assert scored[1]["counterfactual_pnl_30d_cny"] == -2000.0
        assert scored[1]["counterfactual_pnl_60d_cny"] == 1000.0

    def test_summarize_aggregates(self, monkeypatch):
        import jobs.intervention_review as ir
        monkeypatch.setattr(ir, "fwd_return", lambda sym, d, w: 0.10)
        rows = [{"date": "2026-01-01", "asset": "GC=F",
                 "rule": "defense_accumulate_to_hold", "delta_exposure_cny": 1000.0}] * 3
        summ = ir.summarize(ir.score(rows))
        a = summ["defense_accumulate_to_hold"]
        assert a["n"] == 3 and a["settled_30d"] == 3
        assert a["sum_pnl_30d"] == 300.0
