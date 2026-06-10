"""独立快崩防御测试 — VIX 哨兵 / ATR 飙升 → 确定性买侧降级。

背景（2026-06 消融结论）：MA120 regime 看不见快速崩盘（COVID 全程 uptrend），
原 crash 锁因双条件（ATR + 30d 回撤确认）永不触发。防御必须独立于 regime、
只取快腿，且确定性后处理（CIO SKILL 的降级规则从 prompt 搬进代码强制）。
只拦买侧（BUY/ACCUMULATE），不强制卖出——卖出判断留给委员会。
"""
from __future__ import annotations

import pytest

from core.committee import atr_defense_from_text, parse_cio_memo
from core.config import reset_config


@pytest.fixture(autouse=True)
def _reset_config():
    reset_config()
    yield
    reset_config()


def _memo(verdict: str, alloc: int = 5000) -> str:
    return (
        f"VERDICT: {verdict}\nCONFIDENCE: 0.6\nDOMINANT_VIEW: quant\n"
        f"SUGGESTED_ALLOC_CNY: {alloc}"
    )


# ---------- parse_cio_memo 确定性降级 ----------

def test_defense_downgrades_buy_to_accumulate():
    r = parse_cio_memo(_memo("BUY"), defense_flag_on=True)
    assert r["verdict"] == "ACCUMULATE"
    assert r["_original_verdict"] == "BUY"
    assert r["_defense_downgrade"] == "buy_to_accumulate"
    # BUY→ACCUMULATE 只降一级，alloc 保留（Sanity 2 已 clamp）
    assert r["alloc_cny"] == 5000


def test_defense_downgrades_accumulate_to_hold_and_zeroes_alloc():
    r = parse_cio_memo(_memo("ACCUMULATE"), defense_flag_on=True)
    assert r["verdict"] == "HOLD"
    assert r["_defense_downgrade"] == "accumulate_to_hold"
    assert r["alloc_cny"] == 0
    assert r["_original_alloc"] == 5000


def test_defense_leaves_hold_trim_sell_untouched():
    """防御只拦加仓，不强制卖出（VIX 飙升常在恐慌底部，强制卖=高买低卖）"""
    for v in ("HOLD", "TRIM", "SELL"):
        r = parse_cio_memo(_memo(v, alloc=0), defense_flag_on=True)
        assert r["verdict"] == v, v
        assert "_defense_downgrade" not in r, v


def test_no_defense_no_downgrade():
    r = parse_cio_memo(_memo("ACCUMULATE"), defense_flag_on=False)
    assert r["verdict"] == "ACCUMULATE"
    assert "_defense_downgrade" not in r


def test_defense_stacks_with_sanity1_overconfident_buy():
    """纵深：BUY(conf 0.99) 先被 Sanity 1 降 ACCUMULATE，防御再降 HOLD"""
    memo = "VERDICT: BUY\nCONFIDENCE: 0.99\nDOMINANT_VIEW: quant\nSUGGESTED_ALLOC_CNY: 5000"
    r = parse_cio_memo(memo, defense_flag_on=True)
    assert r["verdict"] == "HOLD"
    assert r["_original_verdict"] == "BUY"  # 审计链保留最早原值
    assert r["_defense_downgrade"] == "accumulate_to_hold"
    assert r["alloc_cny"] == 0


# ---------- atr_defense_from_text（coordinator transcript 路径） ----------
# 阈值与 direct 路径同源：core.regime.defense_atr_threshold(symbol)
# （regime.defense_atr_pct_min，per-asset，与 crash 分类解耦）

BRIEF_TRIGGERED = (
    "REGIME: uptrend\n"
    "REASON: MA20 高于 MA120 4%（快崩中 MA 滞后仍显示 uptrend）\n"
    "INPUTS: ma20=100.0000, ma120=96.0000, atr_pct=6.2000, price_quantile_2y=0.9000\n"
    "THRESHOLDS: trend_ma_spread_pct=3.00, crash_atr_pct_min=5.00\n"
    "STRATEGY_HINT: ..."
)


def test_atr_defense_from_text_triggers():
    """默认防御线 5.0：atr 6.2% → 触发"""
    assert atr_defense_from_text(BRIEF_TRIGGERED) is True


def test_atr_defense_from_text_below_threshold():
    calm = BRIEF_TRIGGERED.replace("atr_pct=6.2000", "atr_pct=1.2000")
    assert atr_defense_from_text(calm) is False


def test_atr_defense_from_text_per_asset_threshold():
    """per-asset 防御线（GC=F defense_atr_pct_min=3.5）：4.0% 对黄金触发、对默认资产不触发"""
    gold = "INPUTS: ma20=4600.0, ma120=4500.0, atr_pct=4.0000"
    assert atr_defense_from_text(gold, "GC=F") is True
    assert atr_defense_from_text(gold, "NDQ.AX") is False  # NDQ 防御线 5.0


def test_atr_defense_from_text_missing_graceful():
    assert atr_defense_from_text("") is False
    assert atr_defense_from_text("no regime data") is False


# ---------- 防御线与 crash 分类解耦 ----------

def test_defense_atr_threshold_decoupled_from_crash_classification():
    """调 defense_atr_pct_min 不影响 classify_regime（分类只读 crash_atr_pct_min）"""
    from core.config import set_config_override
    from core.regime import classify_regime, defense_atr_threshold

    metrics = {
        "ma20": 100.0, "ma120": 96.0, "atr_pct": 2.0,
        "price_quantile_2y": 0.5, "return_30d": -0.05,
        "rebound_off_30d_low": None,
    }
    before = classify_regime(metrics, symbol="NDQ.AX")["regime"]

    # 防御线压到 0.5（atr 2.0 会触发防御），crash 分类必须不动
    set_config_override({"regime_per_asset": {"NDQ.AX": {"defense_atr_pct_min": 0.5}}})
    assert defense_atr_threshold("NDQ.AX") == 0.5
    after = classify_regime(metrics, symbol="NDQ.AX")["regime"]
    assert after == before, "防御线居然影响了 regime 分类 — 解耦被破坏"
    assert after != "crash"
