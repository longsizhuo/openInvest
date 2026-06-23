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

    def test_concentration_trim_blocked_records_lens_off(self):
        """集中度减仓被拦 → rule=config_concentration_lens_off（移除 solvency 后唯一路径）；
        delta_exposure = 负（原 alloc 是负数卖出）"""
        rec = _intervention_record(
            "GC=F", "downtrend", 4100.0,
            _v(verdict="HOLD", alloc_cny=0,
               _original_verdict="TRIM", _original_alloc=-20000,
               _original_trim_reason="concentration"),
            atr_defense_on=False,
        )
        assert rec["rule"] == "config_concentration_lens_off"
        assert rec["delta_exposure_cny"] == -20000.0

    def test_config_concentration_lens_off_labeled_distinctly(self):
        """用户 config 关 lens 拦 TRIM → 标 config_concentration_lens_off"""
        rec = _intervention_record(
            "GC=F", "downtrend", 4100.0,
            _v(verdict="HOLD", alloc_cny=0,
               _original_verdict="TRIM", _original_alloc=-20000,
               _original_trim_reason="concentration",
               _concentration_lens="disabled"),
            atr_defense_on=False,
        )
        assert rec["rule"] == "config_concentration_lens_off"
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

    def test_dca_tranche_record(self):
        """黄金分批放行：rule=defense_gold_dca_tranche，delta=意图的 2/3（踏空账本）"""
        rec = _intervention_record(
            "GC=F", "uptrend", 4100.0,
            _v(verdict="ACCUMULATE", alloc_cny=3000,
               _original_verdict="BUY", _original_alloc=9000,
               _defense_dca="tranche", _defense_dca_tranche_idx=1),
            atr_defense_on=False,
        )
        assert rec["rule"] == "defense_gold_dca_tranche"
        assert rec["rule_family"] == "buy_defense"
        assert rec["defense_dca_tranche_idx"] == 1
        assert rec["delta_exposure_cny"] == 6000.0   # 放行 1/3，踏空 2/3

    def test_dca_blocked_record(self):
        """黄金分批暂拦：rule=defense_gold_dca_blocked_<reason>，delta=全意图"""
        rec = _intervention_record(
            "GC=F", "uptrend", 4100.0,
            _v(verdict="HOLD", alloc_cny=0,
               _original_verdict="ACCUMULATE", _original_alloc=9000,
               _defense_dca="blocked_spacing", _defense_dca_tranche_idx=2),
            atr_defense_on=False,
        )
        assert rec["rule"] == "defense_gold_dca_blocked_spacing"
        assert rec["rule_family"] == "buy_defense"
        assert rec["delta_exposure_cny"] == 9000.0

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

    def test_preview_fields(self, monkeypatch):
        """未结算预览：决策日→最新收盘的浮动反事实"""
        import jobs.intervention_review as ir
        monkeypatch.setattr(ir, "fwd_return", lambda sym, d, w: None)  # 全未结算
        monkeypatch.setattr(ir, "latest_return",
                            lambda sym, d: {"ret": 0.02, "days": 5})
        scored = ir.score([{"date": "2026-06-11", "asset": "GC=F",
                            "rule": "reconstructed_trim_blocked",
                            "delta_exposure_cny": -67000.0}])
        assert scored[0]["preview_days"] == 5
        # 被拦减仓遇反弹 +2% → 拦对：-67000×0.02 = -1340（负=避免损失）
        assert scored[0]["preview_pnl_cny"] == -1340.0
        assert scored[0]["counterfactual_pnl_30d_cny"] is None

    def test_summarize_aggregates(self, monkeypatch):
        import jobs.intervention_review as ir
        monkeypatch.setattr(ir, "fwd_return", lambda sym, d, w: 0.10)
        rows = [{"date": "2026-01-01", "asset": "GC=F",
                 "rule": "defense_accumulate_to_hold", "delta_exposure_cny": 1000.0}] * 3
        summ = ir.summarize(ir.score(rows))
        a = summ["defense_accumulate_to_hold"]
        assert a["n"] == 3 and a["settled_30d"] == 3
        assert a["sum_pnl_30d"] == 300.0


# ---------- rule_family 并桶（2026-06-13 漂移审计）----------

class TestRuleFamily:
    def test_live_and_reconstructed_map_to_same_family(self):
        from core.committee_runner import rule_family
        # 拦减仓家族
        assert rule_family("sanity4_solvency_concentration") == "trim_blocked"
        assert rule_family("sanity5_reentry_missing") == "trim_blocked"
        assert rule_family("reconstructed_trim_blocked") == "trim_blocked"
        assert rule_family("config_concentration_lens_off") == "trim_blocked"
        # 拦买入家族
        assert rule_family("defense_accumulate_to_hold") == "buy_defense"
        assert rule_family("defense_buy_to_accumulate") == "buy_defense"
        assert rule_family("reconstructed_defense_downgrade") == "buy_defense"
        # 黄金分批 DCA（放行批 + 暂拦批）都归 buy_defense，与旧全拦并桶
        assert rule_family("defense_gold_dca_tranche") == "buy_defense"
        assert rule_family("defense_gold_dca_blocked_spacing") == "buy_defense"
        assert rule_family("defense_gold_dca_blocked_quota") == "buy_defense"
        assert rule_family("other") == "other"

    def test_record_carries_rule_family(self):
        rec = _intervention_record(
            "GC=F", "downtrend", 4100.0,
            _v(verdict="HOLD", alloc_cny=0,
               _original_verdict="TRIM", _original_alloc=-20000,
               _original_trim_reason="concentration"),
            atr_defense_on=False,
        )
        assert rec["rule"] == "config_concentration_lens_off"
        assert rec["rule_family"] == "trim_blocked"

    def test_summarize_by_family_merges_live_and_reconstructed(self, monkeypatch):
        import jobs.intervention_review as ir
        monkeypatch.setattr(ir, "fwd_return", lambda sym, d, w: 0.05)
        monkeypatch.setattr(ir, "latest_return", lambda sym, d: None)
        rows = [
            {"date": "2026-01-01", "asset": "GC=F", "rule": "sanity4_solvency_concentration",
             "rule_family": "trim_blocked", "delta_exposure_cny": -10000.0},
            {"date": "2026-01-01", "asset": "GC=F", "rule": "reconstructed_trim_blocked",
             "delta_exposure_cny": -10000.0},  # 旧行无 rule_family，靠 mapper 回退
        ]
        summ = ir.summarize(ir.score(rows), key="rule_family")
        # 两行并入同一 trim_blocked 桶
        assert set(summ) == {"trim_blocked"}
        assert summ["trim_blocked"]["n"] == 2


# ---------- 黄金防御分批 DCA 闸（2026-06-13 裁决，wiki18 §5）----------

class TestGoldDcaGate:
    """_gold_defense_dca_gate：读已放行分批，按交易日算 spacing/quota，决定放/拦。"""

    # 固定交易日日历（业务日），最新 = 2026-06-12(周五)
    import pandas as _pd
    CAL = _pd.bdate_range("2026-05-01", "2026-06-12")

    @staticmethod
    def _seed(tmp_path, monkeypatch, records):
        from core import memory_store as ms
        monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path / "memory")
        from core.committee_runner import _log_intervention
        for r in records:
            _log_intervention(r)

    def _gate(self, **kw):
        from core.committee_runner import _gold_defense_dca_gate
        base = dict(n_tranches=3, fraction=0.3333, min_spacing_days=5, window_days=20)
        base.update(kw)
        return _gold_defense_dca_gate("GC=F", self.CAL, **base)

    def test_first_tranche_when_no_history(self, tmp_path, monkeypatch):
        self._seed(tmp_path, monkeypatch, [])
        g = self._gate()
        assert g["allow"] is True
        assert g["reason"] == "first" and g["tranche_idx"] == 1
        assert g["fraction"] == 0.3333

    def test_blocks_on_spacing(self, tmp_path, monkeypatch):
        # 上一批 3 个交易日前（06-09）→ 距今 3 < 5 → spacing 暂拦
        self._seed(tmp_path, monkeypatch, [
            {"asset": "GC=F", "date": "2026-06-09", "rule": "defense_gold_dca_tranche"},
        ])
        g = self._gate()
        assert g["allow"] is False and g["reason"] == "spacing"
        assert g["tranche_idx"] == 2   # 第 2 批被拦

    def test_allows_after_spacing(self, tmp_path, monkeypatch):
        # 上一批 6 个交易日前（06-04）→ 距今 6 ≥ 5 且配额未满 → 放行第 2 批
        self._seed(tmp_path, monkeypatch, [
            {"asset": "GC=F", "date": "2026-06-04", "rule": "defense_gold_dca_tranche"},
        ])
        g = self._gate()
        assert g["allow"] is True and g["reason"] == "ok"
        assert g["tranche_idx"] == 2

    def test_blocks_on_quota(self, tmp_path, monkeypatch):
        # 窗内（最近 20 个交易日 ≈ 4 周）已 3 批（=n_tranches）→ quota 暂拦，
        # 即便 spacing 满也拦（quota 先于 spacing 判）
        self._seed(tmp_path, monkeypatch, [
            {"asset": "GC=F", "date": "2026-05-26", "rule": "defense_gold_dca_tranche"},
            {"asset": "GC=F", "date": "2026-06-03", "rule": "defense_gold_dca_tranche"},
            {"asset": "GC=F", "date": "2026-06-10", "rule": "defense_gold_dca_tranche"},
        ])
        g = self._gate()
        assert g["allow"] is False and g["reason"] == "quota"

    def test_blocked_records_dont_consume_quota(self, tmp_path, monkeypatch):
        # blocked 批 + 其它资产的 tranche 都不计配额；本资产仅 1 放行批且 spacing 满 → 放行
        self._seed(tmp_path, monkeypatch, [
            {"asset": "GC=F", "date": "2026-06-04", "rule": "defense_gold_dca_tranche"},
            {"asset": "GC=F", "date": "2026-06-11", "rule": "defense_gold_dca_blocked_spacing"},
            {"asset": "NDQ.AX", "date": "2026-06-10", "rule": "defense_gold_dca_tranche"},
            {"asset": "GC=F", "date": "2026-06-10", "rule": "defense_accumulate_to_hold"},
        ])
        g = self._gate()
        assert g["allow"] is True and g["reason"] == "ok"
        assert g["tranche_idx"] == 2   # 只 1 个 GC=F 放行批被计入

    def test_same_day_rerun_blocks_second_tranche(self, tmp_path, monkeypatch):
        # 同日已放一批（06-12=最新交易日）→ 再跑距今 0 < 5 → spacing 拦（防同日双批）
        self._seed(tmp_path, monkeypatch, [
            {"asset": "GC=F", "date": "2026-06-12", "rule": "defense_gold_dca_tranche"},
        ])
        g = self._gate()
        assert g["allow"] is False and g["reason"] == "spacing"
