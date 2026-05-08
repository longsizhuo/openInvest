"""P1-2 per-asset REGIME 阈值测试

验证 ASSET_OVERRIDES 让不同资产用不同 threshold：
- GC=F (黄金): crash_atr_pct_min 从默认 5.0 降到 3.5；trend_ma_spread_pct 5.0
- NDQ.AX: trend_ma_spread_pct 4.0
- BTC-USD: trend_ma_spread_pct 8.0, crash_atr_pct_min 8.0
- 其他 symbol: 用默认值
"""
from __future__ import annotations

from core.regime import (
    ASSET_OVERRIDES,
    THRESHOLDS,
    _per_asset_thresholds,
    classify_regime,
    format_regime_brief,
)


# ---------- _per_asset_thresholds ----------

def test_no_symbol_returns_default():
    t = _per_asset_thresholds(None)
    assert t == THRESHOLDS


def test_unknown_symbol_returns_default():
    t = _per_asset_thresholds("AAPL")
    assert t == THRESHOLDS


def test_gold_override_merges_only_named_keys():
    t = _per_asset_thresholds("GC=F")
    # Override 字段
    assert t["trend_ma_spread_pct"] == 5.0
    assert t["crash_atr_pct_min"] == 3.5
    # 未 override 字段 = 默认
    assert t["low_quantile_threshold"] == THRESHOLDS["low_quantile_threshold"]
    assert t["high_quantile_threshold"] == THRESHOLDS["high_quantile_threshold"]


def test_btc_override():
    t = _per_asset_thresholds("BTC-USD")
    assert t["trend_ma_spread_pct"] == 8.0
    assert t["crash_atr_pct_min"] == 8.0


# ---------- classify_regime with symbol ----------

def _metrics(ma20, ma120, atr_pct, quantile=0.5):
    return {
        "ma20": ma20,
        "ma120": ma120,
        "atr_pct": atr_pct,
        "price_quantile_2y": quantile,
    }


def test_gold_atr_4pct_is_crash_under_override_but_not_default():
    """ATR=4% 对黄金来说算 crash（阈值 3.5），对默认来说不算（阈值 5.0）"""
    m = _metrics(ma20=800, ma120=800, atr_pct=4.0)

    # 默认阈值 → range_bound（4 < 5）
    r_default = classify_regime(m)
    assert r_default["regime"] == "range_bound", r_default

    # 黄金 → crash（4 ≥ 3.5）
    r_gold = classify_regime(m, symbol="GC=F")
    assert r_gold["regime"] == "crash", r_gold


def test_gold_ma_spread_4pct_not_uptrend_under_override():
    """MA spread 4% 默认是 uptrend（≥3），黄金不是（<5）"""
    # ma20 4% 高于 ma120
    m = _metrics(ma20=832, ma120=800, atr_pct=1.0)

    r_default = classify_regime(m)
    assert r_default["regime"] == "uptrend"

    r_gold = classify_regime(m, symbol="GC=F")
    assert r_gold["regime"] == "range_bound", r_gold


def test_btc_atr_6pct_not_crash():
    """加密 ATR=6% 不应触发 crash（阈值 8）"""
    m = _metrics(ma20=50000, ma120=50000, atr_pct=6.0)
    r = classify_regime(m, symbol="BTC-USD")
    # 6% < 8% 不 crash；MA spread 0% < 8% 不 trend → range_bound
    assert r["regime"] == "range_bound", r


def test_btc_atr_10pct_is_crash():
    m = _metrics(ma20=50000, ma120=50000, atr_pct=10.0)
    r = classify_regime(m, symbol="BTC-USD")
    assert r["regime"] == "crash"


def test_thresholds_used_returned_in_classification():
    """thresholds_used 字段必须出现在返回里，给 audit 用"""
    m = _metrics(ma20=800, ma120=800, atr_pct=1.0)
    r = classify_regime(m, symbol="GC=F")
    assert "thresholds_used" in r
    assert r["thresholds_used"]["crash_atr_pct_min"] == 3.5


# ---------- format_regime_brief output ----------

def test_brief_marks_per_asset_threshold():
    """brief 字符串里要标 per-asset 标记，让 LLM 看到"""
    m = _metrics(ma20=800, ma120=800, atr_pct=1.0, quantile=0.1)
    brief = format_regime_brief(m, symbol="GC=F")
    assert "per-asset GC=F" in brief, f"per-asset 标记缺失:\n{brief}"
    # 阈值数字也要出现
    assert "5.00" in brief or "3.50" in brief


def test_brief_no_marker_for_default_assets():
    m = _metrics(ma20=800, ma120=800, atr_pct=1.0, quantile=0.1)
    brief = format_regime_brief(m, symbol="AAPL")  # AAPL 不在 overrides
    assert "per-asset" not in brief, f"非 override 资产不该有 marker:\n{brief}"


def test_default_thresholds_unchanged_for_legacy_callers():
    """没 symbol 参数的旧 caller 行为完全不变（向后兼容）"""
    m = _metrics(ma20=104, ma120=100, atr_pct=1.0)
    r_legacy = classify_regime(m)  # 不传 symbol
    # 4% spread > 默认 3% → uptrend
    assert r_legacy["regime"] == "uptrend"


# ---------- ASSET_OVERRIDES schema 健康 ----------

def test_all_overrides_use_known_keys():
    """所有 override 字段必须是 THRESHOLDS 已有的 key"""
    valid_keys = set(THRESHOLDS.keys())
    for symbol, override in ASSET_OVERRIDES.items():
        for k in override:
            assert k in valid_keys, (
                f"ASSET_OVERRIDES[{symbol}] 含未知 key '{k}'。"
                f"合法 key: {valid_keys}"
            )
