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

BRIEF_TRIGGERED = (
    "REGIME: uptrend\n"
    "REASON: MA20 高于 MA120 4%（快崩中 MA 滞后仍显示 uptrend）\n"
    "INPUTS: ma20=100.0000, ma120=96.0000, atr_pct=6.2000, price_quantile_2y=0.9000\n"
    "THRESHOLDS: trend_ma_spread_pct=3.00, crash_atr_pct_min=5.00\n"
    "STRATEGY_HINT: ..."
)


def test_atr_defense_from_text_triggers():
    assert atr_defense_from_text(BRIEF_TRIGGERED) is True


def test_atr_defense_from_text_below_threshold():
    calm = BRIEF_TRIGGERED.replace("atr_pct=6.2000", "atr_pct=1.2000")
    assert atr_defense_from_text(calm) is False


def test_atr_defense_from_text_per_asset_threshold():
    """per-asset 阈值（GC=F crash_atr_pct_min=3.50）：4.0% 对黄金已触发"""
    gold = (
        "INPUTS: ma20=4600.0, ma120=4500.0, atr_pct=4.0000\n"
        "THRESHOLDS (per-asset GC=F): trend_ma_spread_pct=5.00, crash_atr_pct_min=3.50"
    )
    assert atr_defense_from_text(gold) is True


def test_atr_defense_from_text_missing_graceful():
    assert atr_defense_from_text("") is False
    assert atr_defense_from_text("no regime data") is False
    assert atr_defense_from_text("INPUTS: atr_pct=9.0") is False  # 缺阈值行 → False
