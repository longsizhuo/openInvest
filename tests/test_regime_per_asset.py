"""regime 尺度无关契约（#113 重写，原文件测 per-asset 覆盖机制——该机制已删）。

新契约：
1. 尺度无关性——同样的"归一化强度"，不同绝对波动的资产分类一致
2. 黄金 crash 修复——spike ratio 腿让高波动事件能触发（旧绝对阈值 3.5% 在 p99 之上从不触发）
3. graceful——spike/median 缺失（<120d 历史）时保守处理
"""
from __future__ import annotations

import pytest

from openinvest.core.regime import THRESHOLDS, classify_regime, get_thresholds


def _m(**kw):
    base = dict(ma20=100.0, ma120=100.0, atr_pct=1.0, atr_spike_ratio=1.0,
                atr_pct_median_1y=1.0, price_quantile_2y=0.5,
                return_30d=0.0, rebound_off_30d_low=0.02)
    base.update(kw)
    return base


# ---------- 尺度无关性 ----------

@pytest.mark.parametrize("atr_med", [0.4, 0.9, 3.0, 8.0])
def test_same_normalized_strength_same_regime(atr_med):
    """趋势强度按自身波动折算后，任何波动水平的资产用同一把尺。"""
    R = get_thresholds()["trend_spread_atr_ratio"]
    spread_pct = (R + 0.5) * atr_med  # 归一化后恰好过线
    up = classify_regime(_m(ma20=100 + spread_pct, ma120=100.0, atr_pct_median_1y=atr_med))
    assert up["regime"] == "uptrend", up["reason"]
    down = classify_regime(_m(ma20=100 - spread_pct, ma120=100.0, atr_pct_median_1y=atr_med))
    assert down["regime"] == "downtrend"
    weak = classify_regime(_m(ma20=100 + spread_pct * 0.5, ma120=100.0, atr_pct_median_1y=atr_med))
    assert weak["regime"] == "range_bound"


def test_absolute_spread_alone_does_not_trend():
    """高波动资产的大 spread 若盖不过自身噪声地板 → range_bound（旧绝对阈值会误判 uptrend）。"""
    # +6% spread 对日波 3% 的资产只有 2 个典型日波，不足 3.6
    r = classify_regime(_m(ma20=106.0, ma120=100.0, atr_pct_median_1y=3.0))
    assert r["regime"] == "range_bound"
    assert r["inputs_used"]["trend_spread_norm"] == 2.0


# ---------- crash 波动腿（黄金修复） ----------

def test_crash_spike_leg_triggers():
    """波动较自身常态翻倍 + 30 日跌 20% → crash（尺度无关，黄金也能触发了）。"""
    r = classify_regime(_m(atr_spike_ratio=2.3, return_30d=-0.22, price_quantile_2y=0.3,
                           rebound_off_30d_low=0.0))
    assert r["regime"] == "crash"
    assert "波动突变比" in r["reason"]


def test_crash_spike_missing_is_conservative():
    """spike ratio None（<120d 历史）→ 波动腿不满足；深跌腿仍独立可触发。"""
    not_crash = classify_regime(_m(atr_spike_ratio=None, return_30d=-0.22,
                                   price_quantile_2y=0.3, rebound_off_30d_low=0.0))
    assert not_crash["regime"] != "crash"
    deep = classify_regime(_m(atr_spike_ratio=None, return_30d=-0.35,
                              price_quantile_2y=0.3, rebound_off_30d_low=0.0))
    assert deep["regime"] == "crash"  # 深跌腿不看波动


def test_absolute_atr_no_longer_triggers_crash():
    """绝对 ATR 高但相对自身常态正常（如 BTC 日常）→ 不再误判 crash。"""
    r = classify_regime(_m(atr_pct=8.0, atr_spike_ratio=1.1, atr_pct_median_1y=7.5,
                           return_30d=-0.22, price_quantile_2y=0.3, rebound_off_30d_low=0.0))
    assert r["regime"] != "crash"


# ---------- graceful ----------

def test_missing_normalizer_returns_unknown():
    """MA 可算但归一化因子缺失（数据形态异常）→ unknown 保守处理。"""
    r = classify_regime(_m(ma20=110.0, ma120=100.0, atr_pct_median_1y=None))
    assert r["regime"] == "unknown"


def test_thresholds_schema():
    """THRESHOLDS 精确键集合——加/删阈值必须有意识改这里。"""
    assert set(THRESHOLDS) == {
        "trend_spread_atr_ratio", "crash_atr_spike_ratio_min",
        "crash_drawdown_30d_pct", "crash_deep_drawdown_30d_pct",
        "recovery_rebound_pct", "recovery_quantile_max",
        "low_quantile_threshold", "high_quantile_threshold",
    }


def test_symbol_param_is_audit_only():
    """symbol 参数保留（audit 用）但不再改变阈值——任何 symbol 同结果。"""
    a = classify_regime(_m(ma20=108.0, ma120=100.0, atr_pct_median_1y=1.0), symbol="GC=F")
    b = classify_regime(_m(ma20=108.0, ma120=100.0, atr_pct_median_1y=1.0), symbol="WHATEVER")
    assert a["regime"] == b["regime"] == "uptrend"
    assert a["thresholds_used"] == b["thresholds_used"]
