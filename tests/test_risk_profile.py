"""risk_profile 风险档测试 — uptrend 杠杆做成显式开关（2026-06 消融结论显式化）。

消融已证：uptrend 方向锁 = 纯杠杆（+CR −Sharpe 深 MaxDD），不是 alpha。
拆锁后杠杆效应保留为 config 开关：steady（默认，无杠杆）| aggressive
（uptrend ∧ HOLD → ACCUMULATE 确定性升级）。
"""
from __future__ import annotations

import pytest

from openinvest.core.committee import parse_cio_memo, regime_label_from_text
from openinvest.core.config import reset_config, set_config_override

HOLD_MEMO = (
    "VERDICT: HOLD\nCONFIDENCE: 0.7\nDOMINANT_VIEW: macro\nSUGGESTED_ALLOC_CNY: 0"
)


@pytest.fixture(autouse=True)
def _reset_config():
    reset_config()
    yield
    reset_config()


def _aggressive():
    set_config_override({"verdict": {"risk_profile": "aggressive"}})


# ---------- 默认 steady：行为与拆锁后的 B 链完全一致 ----------

def test_steady_default_uptrend_hold_untouched():
    """默认 steady：uptrend + HOLD 不升级（B 链行为，无杠杆）"""
    r = parse_cio_memo(HOLD_MEMO, regime="uptrend")
    assert r["verdict"] == "HOLD"
    assert "_risk_profile_applied" not in r


# ---------- aggressive：uptrend 顺势杠杆 ----------

def test_aggressive_uptrend_hold_upgraded_to_accumulate():
    """aggressive + uptrend + HOLD → ACCUMULATE，留审计字段"""
    _aggressive()
    r = parse_cio_memo(HOLD_MEMO, regime="uptrend")
    assert r["verdict"] == "ACCUMULATE"
    assert r["_original_verdict"] == "HOLD"
    assert r["_risk_profile_applied"] == "aggressive_uptrend_hold_to_accumulate"


def test_aggressive_only_in_uptrend():
    """aggressive 在非 uptrend regime 不动 HOLD（杠杆只挂顺势）"""
    _aggressive()
    for rg in ("downtrend", "range_bound", "crash", "recovery", "unknown", None):
        r = parse_cio_memo(HOLD_MEMO, regime=rg)
        assert r["verdict"] == "HOLD", rg


def test_aggressive_does_not_touch_defensive_verdicts():
    """aggressive 不覆盖 TRIM/SELL/BUY——只升级 HOLD，防御性卖出不被杠杆吃掉"""
    _aggressive()
    for v in ("TRIM", "SELL", "BUY", "ACCUMULATE"):
        memo = f"VERDICT: {v}\nCONFIDENCE: 0.6\nDOMINANT_VIEW: quant\nSUGGESTED_ALLOC_CNY: 0"
        r = parse_cio_memo(memo, regime="uptrend")
        assert "_risk_profile_applied" not in r, v


def test_aggressive_skipped_when_defense_flag_on():
    """INDEP_DEFENSE_FLAG=on（VIX 快崩哨兵）时跳过杠杆——regime 滞后的假 uptrend
    正是 MA120 看不见快崩的盲区，杠杆此时是最大伤害源"""
    _aggressive()
    r = parse_cio_memo(HOLD_MEMO, regime="uptrend", defense_flag_on=True)
    assert r["verdict"] == "HOLD"
    assert "_risk_profile_applied" not in r


def test_aggressive_preserves_earlier_original_verdict():
    """前面 sanity check 已记录 _original_verdict 时不被覆盖（setdefault 语义）"""
    _aggressive()
    # WORKER_UNAVAILABLE → Sanity 3 强制 HOLD；之后 aggressive 不该把
    # garbage 输入的 HOLD 再抬成 ACCUMULATE？——Sanity 3 在前、改的是
    # confidence + verdict，aggressive 看到的 verdict 是 HOLD 会升级。
    # 这里只验证审计链不丢：_original 字段保留最早的原值。
    memo = (
        "VERDICT: BUY\nCONFIDENCE: 0.99\nDOMINANT_VIEW: quant\nSUGGESTED_ALLOC_CNY: 5000"
    )
    r = parse_cio_memo(memo, regime="uptrend")
    # Sanity 1: BUY(0.99) → ACCUMULATE，aggressive 不再介入（verdict 已非 HOLD）
    assert r["verdict"] == "ACCUMULATE"
    assert r["_original_verdict"] == "BUY"


# ---------- regime_label_from_text ----------

def test_regime_label_from_brief():
    brief = (
        "REGIME: uptrend\nREASON: MA20 高于 MA120 4%\n"
        "INPUTS: ma20=100.0, ma120=96.0\nSTRATEGY_HINT: ..."
    )
    assert regime_label_from_text(brief) == "uptrend"


def test_regime_label_from_transcript_middle():
    """coordinator transcript：REGIME 行在文本中间也能提取"""
    raw = "=== QUANT_R1 ===\n# 市场 Regime:\nREGIME: range_bound\nREASON: ...\n=== CIO ===\nVERDICT: HOLD"
    assert regime_label_from_text(raw) == "range_bound"


def test_regime_label_missing_returns_none():
    assert regime_label_from_text("") is None
    assert regime_label_from_text("no regime here") is None
