"""P1-2 per-asset REGIME 阈值测试

验证 ASSET_OVERRIDES 让不同资产用不同 threshold：
- GC=F (黄金): crash_atr_pct_min 从默认 5.0 降到 3.5；trend_ma_spread_pct 5.0
- NDQ.AX: trend_ma_spread_pct 4.0
- BTC-USD: trend_ma_spread_pct 8.0, crash_atr_pct_min 8.0
- 其他 symbol: 用默认值

Step 2: _per_asset_thresholds() 从 config 读取，set_config_override() 实时生效。
"""
from __future__ import annotations

import pytest

from openinvest.core.config import reset_config, set_config_override
from openinvest.core.regime import (
    ASSET_OVERRIDES,
    THRESHOLDS,
    _per_asset_thresholds,
    classify_regime,
    format_regime_brief,
)


@pytest.fixture(autouse=True)
def _reset_config():
    """每个 test 重置 config 隔离状态。"""
    reset_config()
    yield
    reset_config()


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

def _metrics(ma20, ma120, atr_pct, quantile=0.5, return_30d=None, rebound=None):
    return {
        "ma20": ma20,
        "ma120": ma120,
        "atr_pct": atr_pct,
        "price_quantile_2y": quantile,
        "return_30d": return_30d,
        "rebound_off_30d_low": rebound,
    }


def test_gold_atr_4pct_is_crash_under_override_but_not_default():
    """双条件 crash：ATR=4% + 30 日跌 25%。
    黄金 ATR 阈值 3.5 → 波动腿满足 → crash；默认阈值 5.0 → 波动腿不满足 → 非 crash。"""
    # 跌幅腿满足（-25% ≤ -20%），差异只在波动腿的 per-asset 阈值
    m = _metrics(ma20=800, ma120=800, atr_pct=4.0, return_30d=-0.25)

    # 默认阈值 → 波动腿 4 < 5 不满足 → 非 crash → range_bound
    r_default = classify_regime(m)
    assert r_default["regime"] == "range_bound", r_default

    # 黄金 → 波动腿 4 ≥ 3.5 + 跌幅腿 -25% → crash
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
    """加密 ATR=6% 不应触发 crash（波动腿阈值 8 不满足，跌 25% 也没到深跌 30%）"""
    m = _metrics(ma20=50000, ma120=50000, atr_pct=6.0, return_30d=-0.25)
    r = classify_regime(m, symbol="BTC-USD")
    # 6% < 8% 波动腿不满足 + 25% < 30% 非深跌 → range_bound
    assert r["regime"] == "range_bound", r


def test_btc_atr_10pct_is_crash():
    """加密急跌路径：波动腿 10≥8 + 跌幅腿 -25%（≥20% 但 <30%）→ crash"""
    m = _metrics(ma20=50000, ma120=50000, atr_pct=10.0, return_30d=-0.25)
    r = classify_regime(m, symbol="BTC-USD")
    assert r["regime"] == "crash"


# ---------- crash 双条件 ----------

def test_crash_needs_both_legs_vol_only_is_not_crash():
    """只有波动腿（高 ATR）但跌幅腿不满足（仅跌 5%）→ 不是 crash（避免光震荡误触发）"""
    m = _metrics(ma20=100, ma120=100, atr_pct=12.0, return_30d=-0.05)
    r = classify_regime(m)  # 默认 atr 阈值 5
    assert r["regime"] == "range_bound", r


def test_crash_deep_drawdown_low_vol_is_crash():
    """双触发器路径二（深跌）：30 日跌 30% + 低波动 ATR 2% → 判 crash（慢阴跌）。

    这是从旧 AND 逻辑修正过来的：深跌本身就是 crash，无需高 ATR 佐证。"""
    m = _metrics(ma20=90, ma120=100, atr_pct=2.0, return_30d=-0.30)
    r = classify_regime(m)
    assert r["regime"] == "crash", r
    assert "深跌" in r["reason"]


def test_crash_25pct_low_vol_is_not_crash():
    """跌 25% + 低波动：没到深跌门槛(30%)、也没急跌(低 ATR) → 不判 crash"""
    m = _metrics(ma20=90, ma120=100, atr_pct=2.0, return_30d=-0.25)
    r = classify_regime(m)
    assert r["regime"] != "crash", r


def test_crash_fast_path_both_legs():
    """双触发器路径一（急跌）：高 ATR 7% + 30 日跌 25%（≥20% 但 <30%）→ crash"""
    m = _metrics(ma20=90, ma120=100, atr_pct=7.0, return_30d=-0.25)
    r = classify_regime(m)
    assert r["regime"] == "crash", r
    assert "急跌" in r["reason"]


def test_crash_missing_return_30d_does_not_crash():
    """return_30d 缺失（数据不足）时跌幅腿视为不满足 → 保守不判 crash"""
    m = _metrics(ma20=100, ma120=100, atr_pct=12.0, return_30d=None)
    r = classify_regime(m)
    assert r["regime"] != "crash", r


# ---------- recovery ----------

def test_recovery_basic():
    """从低点反弹 15% + 分位 30%（<50%）→ recovery，且优先于 downtrend"""
    # ma 偏离是 downtrend（-10%），但 recovery 在趋势判断之前 → recovery 胜出
    m = _metrics(ma20=90, ma120=100, atr_pct=2.0, quantile=0.30,
                 return_30d=-0.05, rebound=0.15)
    r = classify_regime(m)
    assert r["regime"] == "recovery", r


def test_recovery_needs_low_quantile():
    """反弹够（15%）但分位高（70% ≥ 50%）→ 不是 recovery，回落到趋势判断（downtrend）"""
    m = _metrics(ma20=90, ma120=100, atr_pct=2.0, quantile=0.70,
                 return_30d=-0.05, rebound=0.15)
    r = classify_regime(m)
    assert r["regime"] == "downtrend", r


def test_recovery_needs_enough_rebound():
    """反弹不够（5% < 10%）→ 不是 recovery"""
    m = _metrics(ma20=90, ma120=100, atr_pct=2.0, quantile=0.30,
                 return_30d=-0.05, rebound=0.05)
    r = classify_regime(m)
    assert r["regime"] != "recovery", r


def test_crash_preempts_recovery():
    """crash 双条件满足时，即便反弹+低分位也判 crash（crash 优先级最高）"""
    m = _metrics(ma20=90, ma120=100, atr_pct=8.0, quantile=0.30,
                 return_30d=-0.25, rebound=0.15)
    r = classify_regime(m)
    assert r["regime"] == "crash", r


def test_recovery_in_strategy_hint():
    """拆方向锁后：recovery 的提示是中性数据口径，不含方向预设措辞"""
    from openinvest.core.regime import regime_strategy_hint
    hint = regime_strategy_hint("recovery", 0.3)
    # 不再有人写方向预设（谨慎看多/顺势/不抄底…），改成引用概率口径 + 当前分位
    assert "不预设方向" in hint
    assert "30%" in hint  # 当前分位仍要出现
    for banned in ("谨慎看多", "顺势", "不抄底", "高抛低吸"):
        assert banned not in hint


def test_strategy_hint_with_prob_hint_numbers():
    """传入 prob_hint 时，提示里出现概率口径数字（中位/跌破概率/样本数）"""
    from openinvest.core.regime import regime_strategy_hint
    hint = regime_strategy_hint(
        "uptrend", 0.5,
        prob_hint={"median_pct": 1.4, "p_below": 0.42, "n": 1423, "effective_n": 47},
    )
    assert "+1.4%" in hint
    assert "42%" in hint
    assert "n=1423" in hint
    assert "47" in hint  # 重叠窗口独立样本提示


def test_crash_and_unknown_hints_kept():
    """crash（可执行性）/ unknown（保守默认）两条非方向约束保留"""
    from openinvest.core.regime import regime_strategy_hint
    assert "离场观望" in regime_strategy_hint("crash", 0.3)
    assert "维持原计划" in regime_strategy_hint("unknown", None)


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


# ---------- config override 实时生效 ----------

def test_config_override_affects_per_asset_thresholds():
    """set_config_override() 注入后 _per_asset_thresholds() 实时读到新值"""
    # 默认 trend_ma_spread_pct = 3.0
    t_default = _per_asset_thresholds(None)
    assert t_default["trend_ma_spread_pct"] == 3.0

    # 注入 override
    set_config_override({"regime": {"trend_ma_spread_pct": 6.0}})

    # _per_asset_thresholds 应该读到新值
    t_new = _per_asset_thresholds(None)
    assert t_new["trend_ma_spread_pct"] == 6.0
    # 其他字段不变
    assert t_new["crash_atr_pct_min"] == 5.0


def test_config_override_affects_classify_regime():
    """set_config_override() 改变 classify_regime() 行为"""
    m = _metrics(ma20=832, ma120=800, atr_pct=1.0)  # 4% spread

    # 默认阈值 3% → uptrend
    r1 = classify_regime(m)
    assert r1["regime"] == "uptrend"

    # 把阈值拉高到 5% → range_bound
    set_config_override({"regime": {"trend_ma_spread_pct": 5.0}})
    r2 = classify_regime(m)
    assert r2["regime"] == "range_bound"


def test_config_override_per_asset():
    """set_config_override() 可以注入 per-asset 覆盖"""
    set_config_override({
        "regime_per_asset": {
            "TEST.SYM": {"trend_ma_spread_pct": 10.0},
        },
    })
    t = _per_asset_thresholds("TEST.SYM")
    assert t["trend_ma_spread_pct"] == 10.0
    # 其他字段用默认
    assert t["crash_atr_pct_min"] == 5.0
