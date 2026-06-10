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


# ---------- 买回点估计 get_reentry_estimate ----------

def _write_reviews(tmp_path, recs):
    import json
    p = tmp_path / "verdict_review.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    return p


def test_get_reentry_estimate_basic(tmp_path):
    from core.regime_probability import get_reentry_estimate
    recs = [
        {"asset": "GC=F", "regime_at_decision": "range_bound",
         "actual_returns": {"30d": r / 100}}
        for r in (-12, -8, -5, -3, -1, 0, 2, 4, 6, 10, -7, -2)
    ]
    p = _write_reviews(tmp_path, recs)
    est = get_reentry_estimate("GC=F", "range_bound", 1000.0,
                               window="30d", source="verdict_review", jsonl_path=p)
    assert est is not None
    assert est.n == 12
    assert est.has_downside is True          # 低分位为负
    assert est.downside_price < 1000.0
    assert 0.0 < est.p_below_current <= 1.0


def test_get_reentry_estimate_unavailable_window_returns_none(tmp_path):
    """90d 窗口无样本 → None（unavailable）"""
    from core.regime_probability import get_reentry_estimate
    recs = [{"asset": "GC=F", "regime_at_decision": "range_bound",
             "actual_returns": {"30d": -0.05}}]
    p = _write_reviews(tmp_path, recs)
    assert get_reentry_estimate("GC=F", "range_bound", 1000.0,
                                window="90d", source="verdict_review",
                                jsonl_path=p) is None


def test_get_reentry_estimate_no_price_returns_none(tmp_path):
    from core.regime_probability import get_reentry_estimate
    p = _write_reviews(tmp_path, [{"asset": "GC=F",
                                   "regime_at_decision": "range_bound",
                                   "actual_returns": {"30d": -0.05}}])
    assert get_reentry_estimate("GC=F", "range_bound", None,
                                source="verdict_review", jsonl_path=p) is None


def test_build_reentry_reference_text_marks_unavailable(tmp_path):
    from core.regime_probability import build_reentry_reference_text
    recs = [
        {"asset": "GC=F", "regime_at_decision": "range_bound",
         "actual_returns": {"30d": r / 100}}
        for r in (-10, -5, -2, 0, 3, 8)
    ]
    p = _write_reviews(tmp_path, recs)
    txt = build_reentry_reference_text("GC=F", "range_bound", 1000.0,
                                       source="verdict_review", jsonl_path=p)
    assert "30d" in txt
    assert "90d: 历史样本不足 / unavailable" in txt


# ---------- OHLC 直算源（无 DB 依赖，纯函数）----------

def test_compute_regime_return_frame_synthetic():
    """compute_regime_return_frame 在合成 OHLC 上产出 regime + forward return"""
    import numpy as np
    import pandas as pd
    from core.regime_probability import compute_regime_return_frame

    # 300 天稳步上行 + 噪声 → 应出现 uptrend，且 forward return 多为正
    idx = pd.date_range("2015-01-01", periods=300, freq="D")
    base = np.linspace(100, 200, 300)
    close = base + np.sin(np.arange(300) / 5) * 2
    df = pd.DataFrame({"Close": close, "High": close * 1.01, "Low": close * 0.99},
                      index=idx)
    frame = compute_regime_return_frame(df, "TEST", windows=("30d",))
    assert not frame.empty
    assert "regime" in frame.columns and "fwd_30d" in frame.columns
    # warmup 后应能分出非 unknown 的 regime
    assert (frame["regime"] != "unknown").sum() > 100
    # 上行序列：uptrend 应占可观比例
    assert (frame["regime"] == "uptrend").sum() > 50


def test_compute_regime_return_frame_empty():
    import pandas as pd
    from core.regime_probability import compute_regime_return_frame
    assert compute_regime_return_frame(pd.DataFrame(), "TEST").empty


def test_compute_regime_return_frame_forward_return_exact():
    """forward return 数值正确性：日历日对齐 + 尾部 lookahead 不足 → NaN。

    守 `searchsorted(side="left")` 的对齐逻辑（本 PR 最易错的一块，原来只有 smoke 测试）。
    close[i]=100+i、freq=D → date[i]+30 天恰好 = date[i+30]，
    所以 fwd_30d[i] = close[i+30]/close[i]-1 = 30/(100+i)。
    """
    import numpy as np
    import pandas as pd
    from core.regime_probability import compute_regime_return_frame

    n = 120
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    close = 100.0 + np.arange(n)
    df = pd.DataFrame({"Close": close}, index=idx)  # 仅 Close（走 ATR fallback，regime 无关）

    frame = compute_regime_return_frame(df, "TEST", windows=("30d",))
    fwd = frame["fwd_30d"]

    # 前 n-30 行有"30 日历日后"的样本，尾部 30 行 lookahead 不足 → NaN
    assert fwd.notna().sum() == n - 30
    assert fwd.iloc[n - 30:].isna().all()
    # 精确值
    assert abs(fwd.iloc[0] - 30 / 100) < 1e-9    # (130/100)-1
    assert abs(fwd.iloc[50] - 30 / 150) < 1e-9   # close[50]=150, close[80]=180


def test_make_regime_probability_aggregation():
    """p_up/p_down/p_flat/median/mean 聚合正确性（verdict_review 与 OHLC 共用）。"""
    from core.regime_probability import _make_regime_probability

    # 阈值 5%：>5 只有 10 → p_up=1/5；<-5 只有 -10 → p_down=1/5；其余 flat
    rp = _make_regime_probability("X", "uptrend", [10.0, -10.0, 1.0, -1.0, 5.0], 5.0)
    assert rp.n == 5
    assert rp.p_up == 0.2
    assert rp.p_down == 0.2
    assert rp.p_flat == 0.6
    assert rp.median_return == 1.0          # sorted [-10,-1,1,5,10] → 1
    assert rp.mean_return == 1.0            # (10-10+1-1+5)/5
    assert rp.effective_n == 5              # window_days=1 默认（非重叠）→ effective_n = n
    assert rp.low_confidence is True        # effective_n=5 < MIN_CONFIDENT_N(10)


def test_effective_n_overlapping_window_downweights_confidence():
    """重叠窗口：effective_n = n//window_days，low_confidence 用 effective_n 判定。

    守住 CR 🔴#1：原始 n=200 的重叠日度样本会被误判为高置信；按 30d 窗口折算后
    独立样本仅 6 → 应标 low_confidence。
    """
    from core.regime_probability import _make_regime_probability

    rp_hi = _make_regime_probability("X", "uptrend", [1.0] * 300, 5.0, window_days=30)
    assert rp_hi.n == 300
    assert rp_hi.effective_n == 10          # 300 // 30
    assert rp_hi.low_confidence is False     # 10 not < 10

    rp_lo = _make_regime_probability("X", "uptrend", [1.0] * 200, 5.0, window_days=30)
    assert rp_lo.n == 200
    assert rp_lo.effective_n == 6           # 200 // 30
    assert rp_lo.low_confidence is True      # 6 < 10 —— 原始 n=200 不会标，effective_n 会
    # 文案标注重叠窗口独立样本
    assert "重叠窗口独立≈6" in rp_lo.summary_line()


def test_build_probability_table_from_ohlc_stubbed(monkeypatch):
    """生产默认路径 build_probability_table_from_ohlc（原零覆盖）：stub MarketStore。"""
    import numpy as np
    import pandas as pd
    import db.market_store as ms
    from core.regime_probability import build_probability_table_from_ohlc, RegimeProbability

    idx = pd.date_range("2015-01-01", periods=400, freq="D")
    close = np.linspace(100, 260, 400)  # 稳步上行
    df = pd.DataFrame({"Close": close, "High": close * 1.01, "Low": close * 0.99}, index=idx)

    class _StubStore:
        def get_history_df(self, symbol, days=730):
            return df

    monkeypatch.setattr(ms, "MarketStore", _StubStore)

    table = build_probability_table_from_ohlc(["TEST"], window="30d")
    assert table  # 非空
    # key = (TEST, regime)，regime 非 unknown，value 是合法 RegimeProbability
    assert all(s == "TEST" and r != "unknown" for (s, r) in table)
    for rp in table.values():
        assert isinstance(rp, RegimeProbability)
        assert rp.n > 0
        assert 0.0 <= rp.p_up <= 1.0 and 0.0 <= rp.p_down <= 1.0
        # 重叠窗口折算：effective_n = n // 30，且 ≤ n
        assert rp.effective_n == max(1, rp.n // 30)
        assert rp.effective_n <= rp.n
    # 稳步上行 → 应分出 uptrend 桶
    assert any(r == "uptrend" for (_, r) in table)
    # 至少一个桶的独立样本明显小于原始 n（重叠折算确实生效）
    assert any(rp.effective_n < rp.n for rp in table.values())


def test_ohlc_forward_returns_values_exact(monkeypatch):
    """_ohlc_forward_returns：stub MarketStore，返回值必须等于手算 forward return(%)。

    守 CR 🔴#2 + searchsorted 日期对齐：forward return 不能错位一天，regime 过滤后
    不丢样本/不重复，symbol 接线走 .upper()。
    """
    import numpy as np
    import pandas as pd
    import db.market_store as ms
    from core.regime_probability import _ohlc_forward_returns, compute_regime_return_frame

    # n 要够长：ma120 需 120 行 warmup，非 unknown 的行还得有 30d lookahead，故用 300
    n = 300
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    close = 100.0 + np.arange(n)
    df = pd.DataFrame({"Close": close}, index=idx)

    captured = {}

    class _StubStore:
        def get_history_df(self, symbol, days=730):
            captured["symbol"] = symbol
            return df

    monkeypatch.setattr(ms, "MarketStore", _StubStore)

    # 手算：fwd_30d[i] = close[i+30]/close[i]-1，×100 转百分比（freq=D → date+30 = 第 i+30 行）
    expected = {round((close[i + 30] / close[i] - 1) * 100, 9) for i in range(n - 30)}

    frame = compute_regime_return_frame(df, "TEST", windows=("30d",))
    regimes = [r for r in frame["regime"].unique() if r != "unknown"]
    assert regimes
    got = []
    for rg in regimes:
        got += _ohlc_forward_returns("test", rg, "30d")  # 传小写验证 .upper() 接线
    assert captured["symbol"] == "TEST"
    assert got
    # 每个返回值都精确等于手算 forward return（无错位一天）
    assert all(round(v, 9) in expected for v in got)
    # 覆盖完整：取到的样本数 = 非 unknown 且 fwd 非 NaN 的行数（无丢/无重复）
    valid = frame.dropna(subset=["fwd_30d"])
    valid = valid[valid["regime"] != "unknown"]
    assert len(got) == len(valid)


# ---------- 概率表路径化（2026-06）：多窗 + 路径形状 ----------

def test_compute_regime_return_frame_path_columns_exact():
    """min_/tmin_/atr_pct 列数值正确：严格递增序列 → 窗口内最低点=次日，tmin=1"""
    import numpy as np
    import pandas as pd
    from core.regime_probability import compute_regime_return_frame

    n = 200
    idx = pd.date_range("2020-01-01", periods=n, freq="D")
    close = 100.0 + np.arange(n)
    df = pd.DataFrame({"Close": close}, index=idx)

    frame = compute_regime_return_frame(df, "TEST", windows=("30d",))
    for col in ("min_30d", "tmin_30d", "atr_pct"):
        assert col in frame.columns, col

    i = 150  # warmup 后、lookahead 充足的行
    assert abs(frame["min_30d"].iat[i] - (close[i + 1] / close[i] - 1)) < 1e-12
    assert frame["tmin_30d"].iat[i] == 1
    # 尾部 lookahead 不足 → NaN（与 fwd 同口径）
    assert pd.isna(frame["min_30d"].iat[n - 1])


def test_get_path_profile_multi_window_and_shape(monkeypatch):
    """get_path_profile：多窗分布齐全 + 形状占比构成完备分布（和=1）"""
    import numpy as np
    import pandas as pd
    import db.market_store as ms
    from core.regime_probability import get_path_profile

    n = 600
    idx = pd.date_range("2015-01-01", periods=n, freq="D")
    rng = np.random.default_rng(11)
    close = 100 * np.cumprod(1 + rng.normal(0.001, 0.01, n))  # 缓涨 + 噪声
    df = pd.DataFrame({"Close": close, "High": close * 1.01, "Low": close * 0.99},
                      index=idx)

    class _StubStore:
        def get_history_df(self, symbol, days=730):
            return df

    monkeypatch.setattr(ms, "MarketStore", _StubStore)

    # 找一个有样本的 regime
    from core.regime_probability import compute_regime_return_frame
    frame = compute_regime_return_frame(df, "TEST", windows=("90d",))
    regime = frame.loc[frame["regime"] != "unknown", "regime"].mode().iat[0]

    p = get_path_profile("TEST", regime)
    assert p is not None
    for w in ("30d", "60d", "90d"):
        st = p["windows"][w]
        assert st["n"] > 0
        assert 0.0 <= st["p_below"] <= 1.0
        assert st["p10_pct"] <= st["median_pct"] <= st["p90_pct"]
        assert st["effective_n"] == max(1, st["n"] // int(w.rstrip("d")))
    shape = p["shape"]
    assert shape is not None and shape["window"] == "90d"
    total = shape["pct_dip_then_up"] + shape["pct_up_no_dip"] + shape["pct_down"]
    # 各占比独立四位小数舍入 → 和的容差放到 1e-3
    assert abs(total - 1.0) < 1e-3, "三种路径形状必须构成完备分布"
    assert shape["dip_p25_pct"] <= shape["dip_median_pct"]  # 深四分位更悲观
    assert shape["days_to_trough_median"] >= 1


def test_get_path_profile_straight_up_no_dip(monkeypatch):
    """严格单边上行（无任何回踩）→ 直接涨=100%，先跌后涨=0，收跌=0"""
    import numpy as np
    import pandas as pd
    import db.market_store as ms
    from core.regime_probability import get_path_profile

    n = 400
    idx = pd.date_range("2018-01-01", periods=n, freq="D")
    close = 100.0 * (1.002 ** np.arange(n))  # 每天 +0.2%，永不回头
    df = pd.DataFrame({"Close": close}, index=idx)

    class _StubStore:
        def get_history_df(self, symbol, days=730):
            return df

    monkeypatch.setattr(ms, "MarketStore", _StubStore)

    p = get_path_profile("TEST", "uptrend")
    assert p is not None and p["shape"] is not None
    assert p["shape"]["pct_up_no_dip"] == 1.0
    assert p["shape"]["pct_dip_then_up"] == 0.0
    assert p["shape"]["pct_down"] == 0.0


def test_build_reentry_reference_text_ohlc_multi_window_with_shape(monkeypatch):
    """OHLC 源路径参考：30/60/90 三窗都出 + 路径形状/回踩深度/见底时点行"""
    import numpy as np
    import pandas as pd
    import db.market_store as ms
    from core.regime_probability import build_reentry_reference_text

    n = 600
    idx = pd.date_range("2015-01-01", periods=n, freq="D")
    rng = np.random.default_rng(3)
    close = 100 * np.cumprod(1 + rng.normal(0.0008, 0.012, n))
    df = pd.DataFrame({"Close": close}, index=idx)

    class _StubStore:
        def get_history_df(self, symbol, days=730):
            return df

    monkeypatch.setattr(ms, "MarketStore", _StubStore)

    from core.regime_probability import compute_regime_return_frame
    frame = compute_regime_return_frame(df, "TEST", windows=("90d",))
    regime = frame.loc[frame["regime"] != "unknown", "regime"].mode().iat[0]

    txt = build_reentry_reference_text("TEST", regime, 1000.0)
    assert "30d" in txt and "60d" in txt and "90d" in txt
    assert "路径形状" in txt
    assert "先跌后涨" in txt
    assert "见谷底" in txt
    assert "¥1,000.00" in txt  # 现价行
