"""Regime 概率表测试"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from core.regime_probability import (
    RegimeProbability,
    build_probability_table,
    get_regime_probability,
)


def _make_jsonl(records: list[dict], path: Path) -> Path:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _review(date: str, asset: str, regime: str, verdict: str, ret_30d: float) -> dict:
    return {
        "date": date,
        "asset": asset,
        "regime_at_decision": regime,
        "verdict": verdict,
        "confidence": 0.65,
        "actual_returns": {"1d": 0.0, "7d": 0.01, "30d": ret_30d},
        "hits": {"1d": True, "7d": True, "30d": ret_30d > 0},
        "source": "backtest",
    }


def test_basic_aggregation():
    """10 条 NDQ.AX uptrend，6 条涨>5%，4 条跌>5%"""
    with tempfile.TemporaryDirectory() as td:
        records = [_review(f"2025-01-{i:02d}", "NDQ.AX", "uptrend", "ACCUMULATE",
                          0.08 if i <= 6 else -0.08) for i in range(1, 11)]
        path = _make_jsonl(records, Path(td) / "vr.jsonl")
        table = build_probability_table(path)

        assert ("NDQ.AX", "uptrend") in table
        prob = table[("NDQ.AX", "uptrend")]
        assert prob.n == 10
        assert abs(prob.p_up - 0.6) < 0.01  # 6/10
        assert abs(prob.p_down - 0.4) < 0.01  # 4/10
        assert abs(prob.p_flat - 0.0) < 0.01
        assert not prob.low_confidence  # n=10 >= MIN_CONFIDENT_N


def test_low_confidence_flag():
    """n<10 的组标记 low_confidence"""
    with tempfile.TemporaryDirectory() as td:
        records = [_review(f"2025-01-{i:02d}", "GC=F", "crash", "HOLD", 0.01)
                   for i in range(1, 4)]  # n=3
        path = _make_jsonl(records, Path(td) / "vr.jsonl")
        table = build_probability_table(path)

        prob = table[("GC=F", "crash")]
        assert prob.n == 3
        assert prob.low_confidence


def test_threshold_parameter():
    """阈值参数生效"""
    with tempfile.TemporaryDirectory() as td:
        # 10 条，return 都是 +3%：用 5% 阈值全 flat，用 2% 阈值全 up
        records = [_review(f"2025-01-{i:02d}", "X", "uptrend", "BUY", 0.03)
                   for i in range(1, 11)]
        path = _make_jsonl(records, Path(td) / "vr.jsonl")

        table_5 = build_probability_table(path, threshold_pct=5.0)
        assert table_5[("X", "uptrend")].p_up == 0.0
        assert table_5[("X", "uptrend")].p_flat == 1.0

        table_2 = build_probability_table(path, threshold_pct=2.0)
        assert table_2[("X", "uptrend")].p_up == 1.0
        assert table_2[("X", "uptrend")].p_flat == 0.0


def test_get_regime_probability_query():
    """查询接口返回正确结果"""
    with tempfile.TemporaryDirectory() as td:
        records = [_review(f"2025-01-{i:02d}", "NDQ.AX", "downtrend", "HOLD", 0.07)
                   for i in range(1, 12)]  # n=11
        path = _make_jsonl(records, Path(td) / "vr.jsonl")
        table = build_probability_table(path)

        prob = get_regime_probability("NDQ.AX", "downtrend", table=table)
        assert prob is not None
        assert prob.n == 11
        assert prob.p_up == 1.0  # all +7% > 5%

        # 不存在的组合返 None
        assert get_regime_probability("NDQ.AX", "crash", table=table) is None


def test_empty_jsonl():
    """空文件返空表"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "empty.jsonl"
        path.touch()
        table = build_probability_table(path)
        assert table == {}


def test_missing_jsonl():
    """文件不存在返空表"""
    table = build_probability_table(Path("/nonexistent/vr.jsonl"))
    assert table == {}


def test_summary_line_format():
    """summary_line 格式正确"""
    prob = RegimeProbability(
        asset="NDQ.AX", regime="uptrend", n=49,
        p_up=0.04, p_down=0.51, p_flat=0.45,
        median_return=-5.42, mean_return=-4.72,
        threshold_pct=5.0, low_confidence=False,
    )
    line = prob.summary_line()
    assert "NDQ.AX uptrend" in line
    assert "n=49" in line
    assert "涨>5% 4%" in line
    assert "跌>5% 51%" in line
    assert "中位 -5.4%" in line
    assert "样本不足" not in line


def test_summary_line_low_confidence():
    prob = RegimeProbability(
        asset="X", regime="crash", n=3,
        p_up=0.0, p_down=1.0, p_flat=0.0,
        median_return=-10.0, mean_return=-10.0,
        threshold_pct=5.0, low_confidence=True,
    )
    assert "⚠样本不足" in prob.summary_line()


def test_multiple_assets_and_regimes():
    """多资产多 regime 正确分组"""
    with tempfile.TemporaryDirectory() as td:
        records = (
            [_review(f"2025-01-{i:02d}", "NDQ.AX", "uptrend", "ACC", 0.06) for i in range(1, 6)]
            + [_review(f"2025-01-{i:02d}", "NDQ.AX", "downtrend", "HOLD", -0.02) for i in range(1, 6)]
            + [_review(f"2025-01-{i:02d}", "GC=F", "uptrend", "ACC", 0.01) for i in range(1, 6)]
        )
        path = _make_jsonl(records, Path(td) / "vr.jsonl")
        table = build_probability_table(path)

        assert len(table) == 3
        assert table[("NDQ.AX", "uptrend")].n == 5
        assert table[("NDQ.AX", "downtrend")].n == 5
        assert table[("GC=F", "uptrend")].n == 5
