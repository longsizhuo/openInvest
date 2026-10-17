"""反事实账本（2026-10 评审 Sprint 2，Track A3）

每条裁决配两个反事实：同窗口组合基准（标的 − 基准 = 超额，verdict 无关）与"照 CIO 金额做并在
窗口末平仓、扣往返成本、对比留现金"的净损益（只有有金额的裁决）。HOLD 的对错看超额，不看金额。
"""
from __future__ import annotations

import pytest

from openinvest.jobs import review_calc as rc
from openinvest.jobs import verdict_review as vr


def test_roundtrip_cost_rate_by_market():
    assert rc.roundtrip_cost_rate("510300.SS") == pytest.approx(0.0016)
    assert rc.roundtrip_cost_rate("1024.HK") == pytest.approx(0.0050)
    assert rc.roundtrip_cost_rate("NDQ.AX") == pytest.approx(0.0030)
    assert rc.roundtrip_cost_rate("GC=F") == pytest.approx(0.0050)
    assert rc.roundtrip_cost_rate("BTC-USD") == pytest.approx(0.0040)
    assert rc.roundtrip_cost_rate("AAPL") == pytest.approx(0.0005)
    assert rc.roundtrip_cost_rate("") == pytest.approx(0.0005)


def test_counterfactual_pnl_side_from_verdict_not_sign():
    # 买 10,000，涨 5%，成本 0.16% → +484
    assert rc.counterfactual_pnl("ACCUMULATE", 10_000, 0.05, 0.0016) == pytest.approx(484.0)
    # 买 10,000，跌 3% → −316
    assert rc.counterfactual_pnl("BUY", 10_000, -0.03, 0.0016) == pytest.approx(-316.0)
    # 卖 10,000（alloc −10,000），随后跌 3% → 躲过 300，扣成本 16 → +284
    assert rc.counterfactual_pnl("TRIM", -10_000, -0.03, 0.0016) == pytest.approx(284.0)
    # 卖后涨 5% → 错过 500 + 成本 → −516
    assert rc.counterfactual_pnl("SELL", -10_000, 0.05, 0.0016) == pytest.approx(-516.0)
    # 老存档符号错（regex 时代 TRIM 负号 bug）：TRIM 配正数金额仍按卖侧算；ACCUMULATE 配负数仍按买侧算
    assert rc.counterfactual_pnl("TRIM", 10_000, -0.03, 0.0016) == pytest.approx(284.0)
    assert rc.counterfactual_pnl("ACCUMULATE", -10_000, 0.05, 0.0016) == pytest.approx(484.0)
    # HOLD / UNCLEAR / 无金额 / 未成熟 → None
    assert rc.counterfactual_pnl("HOLD", 10_000, 0.05, 0.0016) is None
    assert rc.counterfactual_pnl("UNCLEAR", 10_000, 0.05, 0.0016) is None
    assert rc.counterfactual_pnl("ACCUMULATE", 0, 0.05, 0.0016) is None
    assert rc.counterfactual_pnl("ACCUMULATE", 10_000, None, 0.0016) is None


def test_review_one_fills_benchmark_excess_and_followed_pnl(tmp_path, monkeypatch):
    date_dir = tmp_path / "2026-06-01"
    date_dir.mkdir()
    (date_dir / "X_SS.md").write_text(
        "**Symbol**: X.SS\n**Verdict**: ACCUMULATE (confidence 0.70)\n"
        "**Suggested allocation CNY**: 10000\n", encoding="utf-8")
    rets = {"X.SS": 0.05, "H.SS": -0.01}
    monkeypatch.setattr(vr, "_window_return", lambda sym, holding, d, w: rets[sym])
    monkeypatch.setattr(vr, "_benchmark_return", lambda sym, d, w: 0.02 if sym else None)
    monkeypatch.setattr(vr, "_benchmark_symbol", lambda: "BENCH.SS")
    monkeypatch.setattr(vr, "_detect_macro_shock", lambda *a, **k: {"detected": False, "drivers": []})
    monkeypatch.setattr(vr, "_decision_regime", lambda *a, **k: None)
    monkeypatch.setattr(vr, "_atr_pct_cached", lambda *a, **k: 2.0)
    rv = vr.review_one(date_dir, "X_SS", {})
    assert rv.alloc_cny == 10000 and rv.alloc_currency == "CNY" and rv.benchmark_symbol == "BENCH.SS"
    assert rv.cost_rate == pytest.approx(0.0016)
    for w in ("1d", "7d", "30d"):
        assert rv.benchmark_returns[w] == pytest.approx(0.02)
        assert rv.excess_returns[w] == pytest.approx(0.03)
        assert rv.followed_pnl[w] == pytest.approx(10000 * (0.05 - 0.0016))

    # 未配置基准 → 没有 benchmark/excess，但照做损益照算
    monkeypatch.setattr(vr, "_benchmark_symbol", lambda: "")
    rv2 = vr.review_one(date_dir, "X_SS", {})
    assert rv2.excess_returns == {} and rv2.followed_pnl["30d"] == pytest.approx(10000 * (0.05 - 0.0016))

    # HOLD：有超额、无金额
    (date_dir / "H_SS.md").write_text(
        "**Symbol**: H.SS\n**Verdict**: HOLD (confidence 0.60)\n**Suggested allocation CNY**: 0\n",
        encoding="utf-8")
    monkeypatch.setattr(vr, "_benchmark_symbol", lambda: "BENCH.SS")
    rv3 = vr.review_one(date_dir, "H_SS", {})
    assert rv3.excess_returns["30d"] == pytest.approx(-0.03) and rv3.followed_pnl == {}

    # 币种口径：金额币种 ≠ 当前基准币种 → 不进金额账本（超额照算）
    (date_dir / "A_SS.md").write_text(
        "**Symbol**: A.SS\n**Verdict**: ACCUMULATE (confidence 0.70)\n**Suggested allocation AUD**: 2000\n",
        encoding="utf-8")
    rets["A.SS"] = 0.05
    rv4 = vr.review_one(date_dir, "A_SS", {}, ctx={"benchmark_symbol": "BENCH.SS", "base_ccy": "CNY"})
    assert rv4.alloc_currency == "AUD" and rv4.followed_pnl == {} and rv4.excess_returns["30d"] == pytest.approx(0.03)
    # ctx 传入时不再读 config
    monkeypatch.setattr(vr, "_benchmark_symbol", lambda: (_ for _ in ()).throw(AssertionError("must not read config")))
    rv5 = vr.review_one(date_dir, "X_SS", {}, ctx={"benchmark_symbol": "BENCH.SS", "base_ccy": "CNY"})
    assert rv5.followed_pnl["30d"] == pytest.approx(10000 * (0.05 - 0.0016))


def _rv(verdict, alloc, actual, bench, conf=0.7):
    r = rc.VerdictReview(
        date="2025-06-02", asset="X.SS", verdict=verdict, confidence=conf,
        expected_direction=rc.EXPECTED_DIRECTION.get(verdict, rc.UNCLEAR_DIRECTION),
        macro_at_decision={}, actual_returns={"30d": actual},
        directions={"30d": "up" if actual > 0.08 else ("down" if actual < -0.08 else "flat")},
        benchmark_symbol="B", benchmark_returns={"30d": bench},
        excess_returns={"30d": round(actual - bench, 4)}, alloc_cny=alloc, cost_rate=0.0016,
    )
    if verdict in rc.EXPECTED_DIRECTION:
        r.hits = {"30d": rc._is_hit(verdict, actual, 0.08)}
    pnl = rc.counterfactual_pnl(verdict, alloc, actual, 0.0016)
    if pnl is not None:
        r.followed_pnl = {"30d": pnl}
    return r


def test_summarize_counterfactual_fields_and_report():
    rows = [_rv("ACCUMULATE", 10_000, 0.05, 0.02) for _ in range(20)]   # 照做 +484 each
    rows += [_rv("ACCUMULATE", 10_000, -0.03, 0.01) for _ in range(10)]  # 照做 −316 each
    rows += [_rv("HOLD", 0, 0.01, 0.02) for _ in range(5)]              # 超额 −1pp，无金额
    b = rc.summarize_verdict_reviews(rows)["holdout"]
    acc = b["by_verdict"]["ACCUMULATE"]
    assert acc["n_benchmark_30d"] == 30 and acc["n_followed_30d"] == 30
    assert acc["excess_mean_30d"] == pytest.approx((20 * 0.03 + 10 * -0.04) / 30, abs=1e-4)
    assert acc["beat_benchmark_rate_30d"] == pytest.approx(20 / 30, abs=1e-3)
    assert acc["followed_pnl_sum_30d"] == pytest.approx(20 * 484 - 10 * 316, abs=0.5)
    assert acc["followed_net_return_30d"] == pytest.approx((20 * 484 - 10 * 316) / 300_000, abs=1e-4)
    assert acc["followed_positive_rate_30d"] == pytest.approx(20 / 30, abs=1e-3)
    hold = b["by_verdict"]["HOLD"]
    assert hold["excess_mean_30d"] == pytest.approx(-0.01, abs=1e-4) and "n_followed_30d" not in hold
    assert b["followed_pnl_total_30d"] == pytest.approx(20 * 484 - 10 * 316, abs=0.5)
    assert b["followed_n_30d"] == 30 and b["benchmark_symbols"] == ["B"]
    assert b["benchmark_missing_count"] == {"1d": 0, "7d": 0, "30d": 0}
    assert b["followed_net_return_30d"] == pytest.approx((20 * 484 - 10 * 316) / 300_000, abs=1e-4)
    text = "\n".join(rc._bucket_lines("x", b))
    assert "反事实（同窗口基准 B" in text and "全部有金额的裁决照做" in text and "+6,520" in text

    # n<30 抑制：不出任何反事实数字，只有计数
    small = rc.summarize_verdict_reviews(rows[:5])["holdout"]
    assert small["rates_suppressed_sub30"] and "followed_pnl_total_30d" not in small
    assert "excess_mean_30d" not in small["by_verdict"]["ACCUMULATE"]


def test_export_excess_fields_and_suppression():
    from scripts.export_accuracy import _aggregate, _suppress_small_samples, HIT_HORIZON
    rows = [{"expected_direction": "up", "hits": {HIT_HORIZON: True}, "directions": {HIT_HORIZON: "flat"},
             "actual_returns": {HIT_HORIZON: 0.02}, "excess_returns": {HIT_HORIZON: 0.01}} for _ in range(31)]
    rows += [{"expected_direction": "down", "hits": {HIT_HORIZON: False}, "directions": {HIT_HORIZON: "flat"},
              "actual_returns": {HIT_HORIZON: 0.02}, "excess_returns": {HIT_HORIZON: -0.03}} for _ in range(3)]
    agg = _aggregate(rows)
    bull, bear = agg["by_direction"]["bullish"], agg["by_direction"]["bearish"]
    assert bull["excess_mean"] == pytest.approx(0.01) and bull["beat_benchmark_rate"] == 1.0 and bull["n_benchmark"] == 31
    assert bear["excess_mean"] == pytest.approx(-0.03) and bear["n_benchmark"] == 3
    supp = _suppress_small_samples(agg)
    assert supp["by_direction"]["bullish"]["excess_mean"] == pytest.approx(0.01)
    assert supp["by_direction"]["bearish"]["excess_mean"] is None and supp["by_direction"]["bearish"]["beat_benchmark_rate"] is None
    assert supp["by_direction"]["bearish"]["n_benchmark"] == 3
    # 公开 JSON 不含金额类键
    assert not any("pnl" in k for k in bull)

    # 基准覆盖不全：total ≥ 30 但 n_benchmark < 30 → 两个比率照样抹掉（分母是 n_benchmark）
    rows2 = [{"expected_direction": "up", "hits": {HIT_HORIZON: True}, "directions": {HIT_HORIZON: "flat"},
              "actual_returns": {HIT_HORIZON: 0.02}} for _ in range(28)]
    rows2 += [{"expected_direction": "up", "hits": {HIT_HORIZON: True}, "directions": {HIT_HORIZON: "flat"},
               "actual_returns": {HIT_HORIZON: 0.02}, "excess_returns": {HIT_HORIZON: 0.01}} for _ in range(3)]
    s2 = _suppress_small_samples(_aggregate(rows2))["by_direction"]["bullish"]
    assert s2["total"] == 31 and s2["rate"] is not None
    assert s2["n_benchmark"] == 3 and s2["excess_mean"] is None and s2["beat_benchmark_rate"] is None
